"""Layer-3 extension monitor tests — registry, attribution, triage ranking.

Runs against synthetic fixtures only (tmp_path); no live extensions are
touched. The live-data smoke is a human step (see SKILL.md procedure).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evolution.monitor import ext_registry as er
from evolution.monitor import ext_triage as et
from evolution.monitor import ext_usage as eu


# ── fixtures ────────────────────────────────────────────────────────────────

def make_ext(root: Path, name: str, body: str, *, symlink: bool = False) -> Path:
    deploy = root / name
    if symlink:
        src = root / f"src-{name}"
        src.mkdir(exist_ok=True)
        (src / "index.ts").write_text(body)
        deploy.symlink_to(src)
        return deploy
    if name.endswith(".ts"):
        deploy.write_text(body)
        return deploy
    deploy.mkdir()
    (deploy / "main.ts").write_text(body)
    return deploy


@pytest.fixture
def ext_root(tmp_path: Path) -> Path:
    root = tmp_path / "exts"
    root.mkdir()
    return root


# ── registry ────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_tool_string_literal(self, ext_root):
        make_ext(ext_root, "a.ts", 'pi.registerTool("my_tool", handler)')
        facts = er.scan_extensions([ext_root])
        assert facts["a.ts"].kind == "tool"
        assert "my_tool" in facts["a.ts"].tools

    def test_tool_object_literal(self, ext_root):
        make_ext(ext_root, "b.ts", 'pi.registerTool({ name: "obj_tool", description: "x" })')
        facts = er.scan_extensions([ext_root])
        assert "obj_tool" in facts["b.ts"].tools

    def test_hook_only_is_inherent_kind(self, ext_root):
        make_ext(ext_root, "c.ts", 'pi.on("session_shutdown", async () => {});')
        facts = er.scan_extensions([ext_root])
        assert facts["c.ts"].kind == "hook"
        assert "session_shutdown" in facts["c.ts"].hooks

    def test_dir_deployment_recurse(self, ext_root):
        make_ext(ext_root, "pkg", 'pi.registerTool({ name: "dir_tool" })')
        facts = er.scan_extensions([ext_root])
        assert "dir_tool" in facts["pkg"].tools

    def test_known_extra_tools_added(self, ext_root, monkeypatch):
        monkeypatch.setitem(er.KNOWN_EXTRA_TOOLS, "d.ts", ["hand_curated"])
        make_ext(ext_root, "d.ts", "console.log(1)")
        facts = er.scan_extensions([ext_root])
        assert "hand_curated" in facts["d.ts"].tools

    def test_symlink_resolves_and_repo_root_found(self, tmp_path, ext_root):
        repo = tmp_path / "some-repo"
        (repo / ".git").mkdir(parents=True)
        body = 'pi.registerTool({ name: "sl_tool" })'
        (repo / "ext.ts").write_text(body)
        deploy = ext_root / "link.ts"
        deploy.symlink_to(repo / "ext.ts")
        facts = er.scan_extensions([ext_root])
        f = facts["link.ts"]
        assert f.is_symlink is True
        assert f.repo_root == str(repo)
        assert "sl_tool" in f.tools

    def test_non_source_files_ignored(self, ext_root):
        (ext_root / "README.md").write_text("registerTool({ name: 'nope' })")
        (ext_root / "notes.txt").write_text("registerTool('nope2'")
        facts = er.scan_extensions([ext_root])
        assert "README.md" not in facts
        assert "notes.txt" not in facts

    def test_nonexistent_root_is_empty_object(self, tmp_path):
        facts = er.scan_extensions([tmp_path / "does-not-exist"])
        assert facts == {}


# ── usage mining ────────────────────────────────────────────────────────────

def write_session(root: Path, name: str, tool_results: list[dict], ts: str):
    recs = [{"type": "session", "id": name}]
    for tr in tool_results:
        recs.append(
            {
                "type": "message",
                "id": tr.get("toolCallId", "x"),
                "timestamp": ts,
                "message": {
                    "role": "toolResult",
                    "toolCallId": tr.get("toolCallId", "x"),
                    "toolName": tr["toolName"],
                    "content": [{"type": "text", "text": "out"}],
                    "isError": tr.get("isError", False),
                    "durationMs": 100,
                },
            }
        )
    d = root / "proj"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")


class TestMineSessions:
    def _facts(self, ext_root):
        make_ext(ext_root, "e.ts", 'pi.registerTool({ name: "alpha" })')
        make_ext(ext_root, "f.ts", 'pi.registerTool({ name: "beta" })')
        return er.scan_extensions([ext_root])

    def test_attribution_and_errors(self, tmp_path, ext_root):
        facts = self._facts(ext_root)
        now = datetime.now(timezone.utc)
        ts = now.isoformat().replace("+00:00", "Z")
        sessions = tmp_path / "sessions"
        write_session(sessions, "s1", [{"toolName": "alpha"}, {"toolName": "alpha", "isError": True}, {"toolName": "bash"}], ts)
        usage = eu.mine_sessions(facts=facts, sessions_root=sessions, now=now.timestamp(), window_days=30)
        assert usage["e.ts"].calls == 2
        assert usage["e.ts"].errors == 1
        assert usage["e.ts"].used
        assert usage["e.ts"].score > 0.9  # fresh event, decay ~1
        assert usage["f.ts"].calls == 0  # seeded but untouched

    def test_window_filters_old_events(self, tmp_path, ext_root):
        facts = self._facts(ext_root)
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=60)).isoformat().replace("+00:00", "Z")
        sessions = tmp_path / "sessions"
        write_session(sessions, "s-old", [{"toolName": "alpha"}], old)
        usage = eu.mine_sessions(facts=facts, sessions_root=sessions, now=now.timestamp(), window_days=30)
        assert usage["e.ts"].calls == 0

    def test_unowned_tools_ignored(self, tmp_path, ext_root):
        facts = self._facts(ext_root)
        now = datetime.now(timezone.utc)
        ts = now.isoformat().replace("+00:00", "Z")
        sessions = tmp_path / "sessions"
        write_session(sessions, "s1", [{"toolName": "bash"}, {"toolName": "read"}], ts)
        usage = eu.mine_sessions(facts=facts, sessions_root=sessions, now=now.timestamp())
        assert all(u.calls == 0 for u in usage.values())

    def test_epoch_ms_timestamp_accepts(self, tmp_path, ext_root):
        make_ext(ext_root, "e.ts", 'pi.registerTool({ name: "alpha" })')
        facts = er.scan_extensions([ext_root])
        now_ms = int(time.time() * 1000)
        (tmp_path / "sessions" / "p").mkdir(parents=True)
        rec = {
            "type": "message",
            "id": "1",
            "timestamp": now_ms * 1000,  # ms precision as epoch int is fine > 1e12
            "message": {"role": "toolResult", "toolCallId": "c1", "toolName": "alpha", "isError": False, "durationMs": 1},
        }
        # top-level timestamp must be ISO; the toolResult inner one is epoch — keep ISO here
        (tmp_path / "sessions" / "p" / "s1.jsonl").write_text(
            json.dumps({"type": "session", "id": "s1"}) + "\n" + json.dumps({**rec, "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}) + "\n"
        )
        usage = eu.mine_sessions(facts=facts, sessions_root=tmp_path / "sessions", now=now_ms / 1000)
        assert usage["e.ts"].calls == 1


# ── triage ranking ──────────────────────────────────────────────────────────

class TestTriage:
    def _facts(self, ext_root):
        make_ext(ext_root, "t.ts", 'pi.registerTool({ name: "alpha" })')
        make_ext(ext_root, "u.ts", 'pi.registerCommand("slashy", fn)')
        make_ext(ext_root, "h.ts", 'pi.on("input", fn)')
        return er.scan_extensions([ext_root])

    def test_ranking_actions(self, tmp_path, ext_root):
        facts = self._facts(ext_root)
        rows = et.triage_ext(facts=facts)
        by_ext = {r.extension: r for r in rows}
        assert by_ext["t.ts"].action == "dusty"
        assert by_ext["u.ts"].action == "dusty"  # command-only caveat
        assert "slash-command-only" in by_ext["u.ts"].reason
        assert by_ext["h.ts"].action == "inherent"
        # hook extension excluded from priority ranking
        assert by_ext["h.ts"].priority == 0.0

    def test_fix_fires_on_two_errors(self, tmp_path, ext_root):
        facts = self._facts(ext_root)
        now = datetime.now(timezone.utc)
        ts = now.isoformat().replace("+00:00", "Z")
        sessions = tmp_path / "sessions"
        write_session(sessions, "s1", [{"toolName": "alpha", "isError": True}] * 2, ts)
        usage = eu.mine_sessions(facts=facts, sessions_root=sessions, now=now.timestamp())
        rows = et.triage_ext(facts=facts, usage=usage)
        row = next(r for r in rows if r.extension == "t.ts")
        assert row.action == "fix"
        assert row.priority > 0
        s = et.suggest(rows)
        assert s and s["extension"] == "t.ts"

    def test_single_error_is_watch_not_fix(self, tmp_path, ext_root):
        facts = self._facts(ext_root)
        now = datetime.now(timezone.utc)
        ts = now.isoformat().replace("+00:00", "Z")
        sessions = tmp_path / "sessions"
        write_session(sessions, "s1", [{"toolName": "alpha", "isError": True}], ts)
        usage = eu.mine_sessions(facts=facts, sessions_root=sessions, now=now.timestamp())
        rows = et.triage_ext(facts=facts, usage=usage)
        row = next(r for r in rows if r.extension == "t.ts")
        assert row.action == "watch"

    def test_healthy_and_suggest_excludes_dusty(self, tmp_path, ext_root):
        facts = self._facts(ext_root)
        now = datetime.now(timezone.utc)
        ts = now.isoformat().replace("+00:00", "Z")
        sessions = tmp_path / "sessions"
        write_session(sessions, "s1", [{"toolName": "alpha"}], ts)
        usage = eu.mine_sessions(facts=facts, sessions_root=sessions, now=now.timestamp())
        rows = et.triage_ext(facts=facts, usage=usage)
        row = next(r for r in rows if r.extension == "t.ts")
        assert row.action == "healthy"
        assert et.suggest(rows) is None

    def test_json_roundtrip(self, tmp_path, ext_root):
        facts = self._facts(ext_root)
        rows = et.triage_ext(facts=facts)
        loaded = json.loads(json.dumps([r.to_dict() for r in rows]))
        assert loaded[0]["extension"]
        assert isinstance(loaded[0]["priority"], float)
