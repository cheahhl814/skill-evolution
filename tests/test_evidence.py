"""Offline tests for the accumulated-experience bar.

The bar is the mechanism that keeps a runtime learning loop from revising a
skill after one session's impression: a conclusion needs accumulated
experience of a real deficiency. These tests pin the rule, because a bar
that silently loosens is the same bug in a different shape.
"""

from __future__ import annotations

import json

from evolution.monitor.evidence import (
    KIND_CREATE,
    KIND_REVISE,
    Thresholds,
    eligible_for_revision,
    evaluate,
    evidence_for,
    used_in_session,
)
from evolution.monitor.telemetry import aggregate
from evolution.monitor.triage import SkillFacts, suggest, triage

NOW = 1_760_000_000.0


def _ev(event: str, skill: str | None = None, session: str = "s1", **extra):
    obj = {"ts": "2026-10-28T00:00:00Z", "session": session, "event": event, "source": "test"}
    if skill:
        obj["skill"] = skill
    obj.update(extra)
    return obj


def _facts(name: str, golden: bool = True) -> SkillFacts:
    return SkillFacts(
        name=name,
        path=f"/tmp/{name}/SKILL.md",
        size_bytes=1000,
        has_golden=golden,
        golden_path=f"/tmp/{name}/golden.jsonl" if golden else None,
    )


# ── the revise bar ──────────────────────────────────────────────────────────


def test_one_session_is_not_enough():
    """A skill used once, even thrice, is a one-off — not a conclusion."""
    usage = aggregate(
        [_ev("skill_read", "a", session="s1"), _ev("skill_read", "a", session="s1"), _ev("skill_read", "a", session="s1")],
        now=NOW,
    )
    ev = evaluate("a", usage["a"], kind=KIND_REVISE)
    assert ev.use_sessions == 1
    assert ev.revisits == 3
    assert not ev.eligible
    assert "not enough accumulated experience" in ev.reason


def test_breadth_without_deficiency_is_not_eligible():
    """Used a lot, but nothing says it is deficient — a well-used skill needs no work."""
    usage = aggregate(
        [_ev("skill_loaded", "a", session=f"s{i}") for i in range(5)],
        now=NOW,
    )
    ev = evaluate("a", usage["a"], kind=KIND_REVISE)
    assert ev.use_sessions == 5
    assert not ev.eligible
    assert "no deficiency signal" in ev.reason


def test_revisits_across_sessions_clears_the_bar():
    usage = aggregate(
        [
            _ev("skill_read", "a", session="s1"),
            _ev("skill_read", "a", session="s1"),
            _ev("skill_read", "a", session="s1"),
            _ev("skill_read", "a", session="s2"),
        ],
        now=NOW,
    )
    ev = evaluate("a", usage["a"], kind=KIND_REVISE)
    assert ev.eligible
    assert "revisited" in ev.reason


def test_failed_write_clears_the_bar():
    usage = aggregate(
        [
            _ev("skill_loaded", "a", session="s1"),
            _ev("skill_loaded", "a", session="s2"),
            _ev("skill_failed", "a", session="s2"),
        ],
        now=NOW,
    )
    ev = evaluate("a", usage["a"], kind=KIND_REVISE)
    assert ev.eligible
    assert "failed write" in ev.reason


def test_repeated_proposals_clear_the_bar():
    usage = aggregate(
        [
            _ev("skill_loaded", "a", session="s1"),
            _ev("skill_loaded", "a", session="s2"),
            _ev("skill_proposed", "a", session="s1", op="patch"),
            _ev("skill_proposed", "a", session="s2", op="patch"),
        ],
        now=NOW,
    )
    ev = evaluate("a", usage["a"], kind=KIND_REVISE)
    assert ev.proposal_sessions == 2
    assert ev.eligible


def test_a_single_proposal_does_not_clear_the_bar():
    usage = aggregate(
        [
            _ev("skill_loaded", "a", session="s1"),
            _ev("skill_loaded", "a", session="s2"),
            _ev("skill_proposed", "a", session="s2", op="patch"),
        ],
        now=NOW,
    )
    ev = evaluate("a", usage["a"], kind=KIND_REVISE)
    assert ev.proposal_sessions == 1
    assert not ev.eligible


# ── the create bar ──────────────────────────────────────────────────────────


def test_create_needs_repeated_independent_proposals():
    """A new skill has no usage history — the accumulated experience is the
    agent's own repeated conclusion, in independent sessions."""
    once = aggregate([_ev("skill_proposed", "new", session="s1", op="create")], now=NOW)
    assert not evaluate("new", once["new"], kind=KIND_CREATE).eligible

    twice = aggregate(
        [
            _ev("skill_proposed", "new", session="s1", op="create"),
            _ev("skill_proposed", "new", session="s2", op="create"),
        ],
        now=NOW,
    )
    ev = evaluate("new", twice["new"], kind=KIND_CREATE)
    assert ev.eligible
    assert "recurring" in ev.reason


def test_two_proposals_in_one_session_are_not_independent():
    usage = aggregate(
        [
            _ev("skill_proposed", "new", session="s1", op="create"),
            _ev("skill_proposed", "new", session="s1", op="create"),
        ],
        now=NOW,
    )
    ev = evaluate("new", usage["new"], kind=KIND_CREATE)
    assert ev.proposal_sessions == 1
    assert ev.proposals == 2
    assert not ev.eligible


# ── thresholds are overridable ──────────────────────────────────────────────


def test_thresholds_are_env_overridable(monkeypatch):
    monkeypatch.setenv("PI_SKILL_EVIDENCE_MIN_SESSIONS", "1")
    monkeypatch.setenv("PI_SKILL_EVIDENCE_MIN_REVISITS", "1")
    th = Thresholds()
    assert th.min_sessions == 1
    assert th.min_revisits == 1
    usage = aggregate([_ev("skill_read", "a", session="s1")], now=NOW)
    assert evaluate("a", usage["a"], kind=KIND_REVISE, thresholds=th).eligible


# ── wiring ──────────────────────────────────────────────────────────────────


def test_eligible_for_revision_lists_only_qualified_skills():
    events = [
        # qualifies: two sessions + a failed write
        _ev("skill_loaded", "good", session="s1"),
        _ev("skill_loaded", "good", session="s2"),
        _ev("skill_failed", "good", session="s2"),
        # does not qualify: used a lot, no deficiency
        _ev("skill_loaded", "fine", session="s1"),
        _ev("skill_loaded", "fine", session="s2"),
        _ev("skill_loaded", "fine", session="s3"),
    ]
    rows = eligible_for_revision(events)
    assert [r.skill for r in rows] == ["good"]
    assert rows[0].eligible


def test_used_in_session_finds_the_sessions_skills():
    events = [
        _ev("skill_loaded", "a", session="s1"),
        _ev("skill_read", "b", session="s1"),
        _ev("skill_loaded", "c", session="s2"),
    ]
    assert used_in_session(events, "s1") == ["a", "b"]
    assert used_in_session(events, "s2") == ["c"]


def test_evidence_for_reads_the_telemetry_file(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                _ev("skill_loaded", "a", session="s1"),
                _ev("skill_loaded", "a", session="s2"),
                _ev("skill_failed", "a", session="s2"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ev = evidence_for("a", telemetry_path=p)
    assert ev.eligible
    assert ev.use_sessions == 2


# ── the suggestion honours the bar ──────────────────────────────────────────


def test_suggest_skips_a_skill_that_does_not_clear_the_bar():
    """The prompt suggestion must not name a skill the bar would refuse."""
    usage = aggregate(
        [
            _ev("skill_loaded", "hot", session="s1"),
            _ev("skill_loaded", "hot", session="s2"),
            # no deficiency signal -> the bar says "no work needed"
        ],
        now=NOW,
    )
    facts = {"hot": _facts("hot", golden=True)}
    assert suggest(triage(usage=usage, facts=facts)) is None


def test_suggest_names_a_skill_that_clears_the_bar():
    usage = aggregate(
        [
            _ev("skill_loaded", "hot", session="s1"),
            _ev("skill_loaded", "hot", session="s2"),
            _ev("skill_failed", "hot", session="s2"),
        ],
        now=NOW,
    )
    facts = {"hot": _facts("hot", golden=True)}
    s = suggest(triage(usage=usage, facts=facts))
    assert s is not None
    assert s["skill"] == "hot"
    assert "evidence" in s
