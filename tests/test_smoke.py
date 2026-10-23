"""Offline tests that need no API keys: parser, constraints, session miner."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution.core.config import EvolutionConfig, resolve_skill, find_git_repo_root  # noqa: E402
from evolution.core.constraints import ConstraintValidator  # noqa: E402
from evolution.core.fitness import length_penalty, skill_fitness_metric  # noqa: E402
from evolution.core.pi_sessions import extract_pairs  # noqa: E402


def test_length_penalty():
    assert length_penalty(1000, 15360) == 0.0
    assert 0 < length_penalty(15000, 15360) <= 0.3
    assert length_penalty(16000, 15360) == 0.3


def test_metric():
    class P:
        output = "run the workflow and verify results"

    class E:
        expected_behavior = "verify results and report evidence"

    s = skill_fitness_metric(E(), P())
    assert 0.3 <= s <= 1.0


def test_constraints_contract():
    v = ConstraintValidator(EvolutionConfig())
    good = "---\nname: x\ndescription: Use when testing.\n---\nbody"
    bad = "no frontmatter"
    assert all(r.passed for r in v.validate_all(good))
    assert not v.validate_all(bad)[2].passed
    r = v.validate_all(good + "x" * 20000)[0]
    assert not r.passed  # size cap


def test_session_parser(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join([
        json.dumps({"type": "session", "id": "s1", "cwd": "/tmp"}),
        json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "forge a skill for QC"}]}}),
        json.dumps({"type": "model_change", "provider": "x", "modelId": "y"}),
        json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "Forged."}]}}),
    ]))
    pairs = extract_pairs(f)
    assert len(pairs) == 1 and pairs[0].user_text == "forge a skill for QC"


def test_git_root_detection(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_git_repo_root(sub) == tmp_path
    assert find_git_repo_root(Path("/etc")) is None


def test_resolve_symlinked_skill(tmp_path: Path):
    root = tmp_path / "roots"
    src = tmp_path / "src" / "my-skill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: my-skill\ndescription: Use when resolving.\n---\nbody")
    root.mkdir()
    (root / "my-skill").symlink_to(src)
    cfg = EvolutionConfig(skills_roots=[root])
    assert resolve_skill("my-skill", cfg) == (src / "SKILL.md").resolve()

def test_mine_pi_sessions_finds_token(tmp_path: Path, monkeypatch):
    """Regression: the heuristic miner used \" \".join(<string>), which
    space-separates every character and made every token search miss."""
    import evolution.core.pi_sessions as ps

    d = tmp_path / "s" / "dir"
    d.mkdir(parents=True)
    f = d / "2026-01-01.jsonl"
    f.write_text("\n".join([
        json.dumps({"type": "session", "id": "s1", "cwd": "/tmp"}),
        json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "forge a skill for QC"}]}}),
        json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "Forged."}]}}),
    ]))
    monkeypatch.setattr(ps, "HOME", tmp_path / "s")
    orig = ps.iter_session_files
    monkeypatch.setattr(ps, "iter_session_files", lambda root=None: iter([f]))
    pairs = ps.mine_pi_sessions("skill-forge")
    assert len(pairs) == 1


def test_mined_sessions_dataset_uses_sessionpair(tmp_path, monkeypatch):
    """Regression: mine_pi_sessions returns SessionPair dataclasses; the
    sessions dataset builder used to unpack them as (meta, user, assistant)
    triples after the 3f1b07c redesign and crashed with TypeError."""
    import evolution.core.dataset_builder as db
    from evolution.core.pi_sessions import SessionPair

    pair = SessionPair("s1", "/tmp", "2026-01-01", "forge a skill", "ok")
    monkeypatch.setattr(db, "mine_pi_sessions", lambda name, limit=200: [pair])
    ds = db.build_dataset_from_pi_sessions("skill-forge", "body", judge=False)
    # a single mined pair is duplicated across splits (_default_split), so
    # assert on content rather than count
    assert ds.all_examples, "dataset must retain the mined pair"
    assert all("forge a skill" in e.task_input for e in ds.all_examples)
