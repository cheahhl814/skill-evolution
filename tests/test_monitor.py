"""Offline tests for the monitor (telemetry aggregation) and triage ranking.

The telemetry writer lives in the TypeScript extension; these tests pin the
*reader* contract and the ranking logic, so a schema drift shows up as a
failing test rather than as a silently empty triage table.
"""

from __future__ import annotations

import json
import time

from evolution.monitor.telemetry import aggregate, read_events
from evolution.monitor.triage import SkillFacts, TriageRow, suggest, triage

NOW = 1_760_000_000.0  # fixed clock for deterministic decay


def _ts(offset_days: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - offset_days * 86400))


def _ev(event: str, skill: str | None = None, days: float = 0.0, session: str = "s1", **extra):
    obj = {"ts": _ts(days), "session": session, "event": event, "source": "test"}
    if skill:
        obj["skill"] = skill
    obj.update(extra)
    return obj


# ── telemetry ───────────────────────────────────────────────────────────────


def test_read_events_skips_malformed_and_missing_file(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"event":"skill_loaded","skill":"a"}\nnot json\n\n{"no_event":1}\n', encoding="utf-8")
    events = read_events(p)
    assert len(events) == 1
    assert events[0]["skill"] == "a"
    assert read_events(tmp_path / "missing.jsonl") == []


def test_aggregate_counts_per_skill():
    events = [
        _ev("skill_loaded", "alpha"),
        _ev("skill_loaded", "alpha"),
        _ev("skill_read", "alpha"),
        _ev("skill_managed", "alpha", op="patch"),
        _ev("skill_failed", "alpha"),
        _ev("skill_loaded", "beta"),
        _ev("learning_tool", tool="wiki_retro"),
    ]
    usage = aggregate(events, now=NOW)
    a = usage["alpha"]
    assert (a.loads, a.reads, a.edits, a.failures) == (2, 1, 1, 1)
    assert a.used is True
    assert usage["beta"].loads == 1
    # learning_tool is global -> synthetic bucket, not a skill
    assert usage["__learning__"].learning_calls == 1
    assert "__learning__" not in {k for k in usage if k == "beta"}


def test_aggregate_ignores_unknown_events():
    usage = aggregate([_ev("brand_new_event", "alpha")], now=NOW)
    assert "alpha" not in usage


def test_recency_decay_demotes_stale_usage():
    fresh = aggregate([_ev("skill_loaded", "fresh", days=0)], now=NOW)
    stale = aggregate([_ev("skill_loaded", "stale", days=60)], now=NOW)
    assert fresh["fresh"].score > stale["stale"].score
    # 60 days with a 14-day half-life -> 2^-4.28 of the fresh score
    assert stale["stale"].score < fresh["fresh"].score * 0.1


# ── triage ──────────────────────────────────────────────────────────────────


def _facts(name: str, size: int = 1000, golden: bool = False) -> SkillFacts:
    return SkillFacts(
        name=name,
        path=f"/tmp/{name}/SKILL.md",
        size_bytes=size,
        has_golden=golden,
        golden_path="/tmp/golden.jsonl" if golden else None,
    )


def test_triage_actions():
    usage = aggregate(
        [
            _ev("skill_loaded", "unmeasured"),
            _ev("skill_loaded", "measured"),
            _ev("skill_loaded", "measured"),
            _ev("skill_loaded", "big"),
            _ev("skill_loaded", "big"),
            _ev("skill_loaded", "churny"),
            _ev("skill_managed", "churny", op="patch"),
            _ev("skill_managed", "churny", op="patch"),
            _ev("skill_managed", "churny", op="patch"),
        ],
        now=NOW,
    )
    facts = {
        "unmeasured": _facts("unmeasured"),
        "measured": _facts("measured", golden=True),
        "big": _facts("big", size=20000, golden=True),
        "churny": _facts("churny", golden=True),
        "unused": _facts("unused"),
    }
    rows = {r.skill: r for r in triage(usage=usage, facts=facts)}
    assert rows["unmeasured"].action == "measure"
    assert rows["measured"].action == "evolve"
    assert rows["big"].action == "shrink"
    assert rows["churny"].action == "stabilize"
    assert rows["unused"].action == "review"
    # "measure" must outrank "evolve" at equal usage
    assert rows["unmeasured"].potential > rows["measured"].potential


def test_triage_ranks_by_priority_desc():
    usage = aggregate(
        [_ev("skill_loaded", "hot"), _ev("skill_loaded", "hot"), _ev("skill_loaded", "cold")],
        now=NOW,
    )
    facts = {"hot": _facts("hot"), "cold": _facts("cold")}
    rows = triage(usage=usage, facts=facts)
    assert rows[0].skill == "hot"
    assert rows[0].priority >= rows[1].priority
    assert isinstance(rows[0], TriageRow)


def test_triage_row_serialises():
    usage = aggregate([_ev("skill_loaded", "alpha")], now=NOW)
    row = triage(usage=usage, facts={"alpha": _facts("alpha")})[0]
    d = row.to_dict()
    assert d["skill"] == "alpha"
    assert d["action"] == "measure"
    assert d["loads"] == 1
    assert json.dumps(d)  # JSON-safe


# ── suggestion (what the extension injects into the prompt) ─────────────────


def test_suggest_picks_top_actionable_target():
    # The suggestion is evidence-gated (evolution/monitor/evidence.py): a skill
    # is only named when the accumulated-experience bar is cleared — used across
    # several sessions AND showing a deficiency signal.
    usage = aggregate(
        [
            _ev("skill_loaded", "measured", session="s1"),
            _ev("skill_loaded", "measured", session="s2"),
            _ev("skill_failed", "measured", session="s2"),
            _ev("skill_loaded", "ncal", session="s1"),
        ],
        now=NOW,
    )
    facts = {"measured": _facts("measured", golden=True), "ncal": _facts("ncal")}
    s = suggest(triage(usage=usage, facts=facts))
    assert s is not None
    assert s["skill"] == "measured"
    assert s["action"] == "evolve"
    assert s["priority"] > 0
    assert "reason" in s
    assert "evidence" in s


def test_suggest_is_none_when_nothing_is_actionable():
    # never used -> action "review" (a human's call, not a skill run)
    usage = aggregate([], now=NOW)
    facts = {"idle": _facts("idle")}
    assert suggest(triage(usage=usage, facts=facts)) is None


def test_suggest_requires_real_usage():
    # used but with no golden set -> "measure" IS actionable — but only once
    # the accumulated-experience bar is cleared.
    usage = aggregate(
        [
            _ev("skill_loaded", "unmeasured", session="s1"),
            _ev("skill_loaded", "unmeasured", session="s2"),
            _ev("skill_failed", "unmeasured", session="s2"),
        ],
        now=NOW,
    )
    s = suggest(triage(usage=usage, facts={"unmeasured": _facts("unmeasured")}))
    assert s is not None and s["action"] == "measure"
    # but a row with usage 0 must never be suggested
    rows = triage(usage=aggregate([], now=NOW), facts={"unmeasured": _facts("unmeasured")})
    assert all(r.usage == 0 for r in rows)
    assert suggest(rows) is None


def test_suggest_skips_skills_below_the_evidence_bar():
    """A skill used in one session must not be named by the suggestion."""
    usage = aggregate([_ev("skill_loaded", "unmeasured", session="s1")], now=NOW)
    assert suggest(triage(usage=usage, facts={"unmeasured": _facts("unmeasured")})) is None
