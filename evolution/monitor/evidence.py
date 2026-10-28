"""Evidence model — accumulated experience before concluding a skill needs work.

Why this module exists
----------------------
The Layer-1 runtime can write to skills. A runtime that writes on every
session turns unrelated work into skill churn: the agent patches a skill it
happened to load, the patch lands in that session's handoff, and the next
session inherits a revision nobody asked for. The 2026-10-28 OceanDecade
round is the worked example — a proposal-writing session also revised
`design-pack` and `wpmgn-pack-sync`, and both revisions ended up in the
handoff of a session that was about something else entirely.

The fix is not "prompt the agent less" (prompts are suggestions, and the
agent's judgement about *when* a skill is deficient is exactly what
contaminates a session). The fix is a **measurable bar**: a skill becomes a
revision candidate only when the telemetry shows accumulated experience of a
real deficiency. Everything here is computed from the telemetry JSONL, so the
bar is auditable rather than vibes.

The two kinds of conclusion
---------------------------
``revise``
    "this existing skill should be improved". Requires **breadth** (the skill
    was used across several sessions — a one-off is not a pattern) **and** a
    **deficit signal** (the agent kept going back to it without getting the
    answer, a write to it failed, or the agent already concluded twice that
    it needs work).

``create``
    "a new skill is needed". A skill that does not exist yet has no usage
    history, so the only accumulated experience available is the agent's own
    repeated conclusion: the same skill name proposed in several independent
    sessions. The first proposal is not a conclusion.

Thresholds
----------
Defaults are deliberately conservative: the bar should be crossed by a real
pattern, not by an agent having a good idea once.

===============================  =======  ===================================
Knob                            Default  Meaning
===============================  =======  ===================================
``PI_SKILL_EVIDENCE_MIN_SESSIONS``   2   distinct sessions of use (breadth)
``PI_SKILL_EVIDENCE_MIN_REVISITS``   3   uses inside one session (deficit)
``PI_SKILL_EVIDENCE_MIN_FAILURES``   1   failed writes against the skill
``PI_SKILL_EVIDENCE_MIN_PROPOSALS``  2   independent sessions that proposed it
===============================  =======  ===================================

Usage::

    python -m evolution.monitor.evidence --skill ncal --kind revise --json
    python -m evolution.monitor.evidence --eligible --session s-abc --json
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .telemetry import DEFAULT_TELEMETRY_PATH, SkillUsage, read_events

DEFAULT_MIN_SESSIONS = 2
DEFAULT_MIN_REVISITS = 3
DEFAULT_MIN_FAILURES = 1
DEFAULT_MIN_PROPOSALS = 2

KIND_REVISE = "revise"
KIND_CREATE = "create"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


@dataclass
class Thresholds:
    """The accumulated-experience bar. Every field is env-overridable."""

    min_sessions: int = field(default_factory=lambda: _env_int("PI_SKILL_EVIDENCE_MIN_SESSIONS", DEFAULT_MIN_SESSIONS))
    min_revisits: int = field(default_factory=lambda: _env_int("PI_SKILL_EVIDENCE_MIN_REVISITS", DEFAULT_MIN_REVISITS))
    min_failures: int = field(default_factory=lambda: _env_int("PI_SKILL_EVIDENCE_MIN_FAILURES", DEFAULT_MIN_FAILURES))
    min_proposals: int = field(default_factory=lambda: _env_int("PI_SKILL_EVIDENCE_MIN_PROPOSALS", DEFAULT_MIN_PROPOSALS))


@dataclass
class Evidence:
    """The accumulated experience behind one skill, and the verdict on it."""

    skill: str
    kind: str = KIND_REVISE
    use_sessions: int = 0
    revisits: int = 0
    failures: int = 0
    proposal_sessions: int = 0
    proposals: int = 0
    eligible: bool = False
    reason: str = ""
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "skill": self.skill,
            "kind": self.kind,
            "use_sessions": self.use_sessions,
            "revisits": self.revisits,
            "failures": self.failures,
            "proposal_sessions": self.proposal_sessions,
            "proposals": self.proposals,
            "eligible": self.eligible,
            "reason": self.reason,
            "score": round(self.score, 3),
        }


def evaluate(
    skill: str,
    usage: SkillUsage | None,
    kind: str = KIND_REVISE,
    thresholds: Thresholds | None = None,
) -> Evidence:
    """Decide whether the accumulated experience supports a conclusion.

    ``usage`` is the aggregated record for the skill (may be None — a skill
    that was never used has no evidence at all). For ``create`` the skill does
    not exist yet, so only the proposal channel counts.
    """
    th = thresholds or Thresholds()
    ev = Evidence(skill=skill, kind=kind)
    if usage is not None:
        ev.use_sessions = len(usage.use_sessions)
        ev.revisits = max(usage.session_uses.values()) if usage.session_uses else 0
        ev.failures = usage.failures
        ev.proposal_sessions = len(usage.proposal_sessions)
        ev.proposals = usage.proposals

    # Breadth: the skill is part of recurring work, not a one-off.
    breadth_ok = ev.use_sessions >= th.min_sessions
    # Deficiency: something is actually wrong with it.
    revisits_ok = ev.revisits >= th.min_revisits
    failures_ok = ev.failures >= th.min_failures
    proposals_ok = ev.proposal_sessions >= th.min_proposals

    if kind == KIND_CREATE:
        # A skill that does not exist has no usage history: the only
        # accumulated experience is the agent's own repeated conclusion.
        ev.eligible = proposals_ok
        ev.score = float(ev.proposal_sessions)
        if ev.eligible:
            ev.reason = (
                "proposed for creation in %d independent sessions — the need is recurring"
                % ev.proposal_sessions
            )
        else:
            ev.reason = (
                "proposed for creation in %d of %d independent sessions — not enough "
                "accumulated experience to conclude a new skill is needed"
                % (ev.proposal_sessions, th.min_proposals)
            )
        return ev

    deficit_reasons = []
    if revisits_ok:
        deficit_reasons.append("revisited %d times in one session" % ev.revisits)
    if failures_ok:
        deficit_reasons.append("%d failed write(s)" % ev.failures)
    if proposals_ok:
        deficit_reasons.append("proposed for revision in %d independent sessions" % ev.proposal_sessions)

    ev.eligible = breadth_ok and bool(deficit_reasons)
    ev.score = float(ev.use_sessions) + 2.0 * ev.revisits + 2.0 * ev.failures + 2.0 * ev.proposal_sessions
    if not breadth_ok:
        ev.reason = (
            "used in %d of %d independent sessions — not enough accumulated experience"
            % (ev.use_sessions, th.min_sessions)
        )
    elif deficit_reasons:
        ev.reason = "used in %d sessions and %s" % (ev.use_sessions, ", ".join(deficit_reasons))
    else:
        ev.reason = (
            "used in %d sessions with no deficiency signal (revisits %d/%d, failures %d/%d, "
            "revision proposals %d/%d) — a well-used skill needs no work"
            % (
                ev.use_sessions,
                ev.revisits,
                th.min_revisits,
                ev.failures,
                th.min_failures,
                ev.proposal_sessions,
                th.min_proposals,
            )
        )
    return ev


def evidence_for(
    skill: str,
    events: list[dict] | None = None,
    kind: str = KIND_REVISE,
    thresholds: Thresholds | None = None,
    telemetry_path: Path | str | None = None,
) -> Evidence:
    """Convenience: read the telemetry, aggregate, and evaluate one skill."""
    from .telemetry import aggregate

    evs = events if events is not None else read_events(telemetry_path)
    usage = aggregate(evs).get(skill)
    return evaluate(skill, usage, kind=kind, thresholds=thresholds)


def eligible_for_revision(
    events: list[dict] | None = None,
    thresholds: Thresholds | None = None,
    telemetry_path: Path | str | None = None,
) -> list[Evidence]:
    """Every skill whose accumulated experience supports a revision conclusion."""
    from .telemetry import aggregate

    evs = events if events is not None else read_events(telemetry_path)
    usage = aggregate(evs)
    out: list[Evidence] = []
    for name, rec in usage.items():
        if name == "__learning__":
            continue
        ev = evaluate(name, rec, kind=KIND_REVISE, thresholds=thresholds)
        if ev.eligible:
            out.append(ev)
    out.sort(key=lambda e: (-e.score, e.skill))
    return out


def used_in_session(events: list[dict], session: str) -> list[str]:
    """Names of the skills the agent loaded or read inside one session."""
    names: set[str] = set()
    for ev in events:
        if str(ev.get("session") or "") != session:
            continue
        if ev.get("event") not in ("skill_loaded", "skill_read"):
            continue
        name = ev.get("skill")
        if isinstance(name, str) and name:
            names.add(name)
    return sorted(names)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="evidence",
        description="Accumulated-experience bar for skill revision / creation conclusions.",
    )
    ap.add_argument("--skill", default=None, help="evaluate one skill")
    ap.add_argument("--kind", default=KIND_REVISE, choices=(KIND_REVISE, KIND_CREATE))
    ap.add_argument("--eligible", action="store_true", help="list every skill that clears the bar")
    ap.add_argument("--session", default=None, help="restrict --eligible to skills used in this session")
    ap.add_argument("--telemetry", default=None, help="path to skill-usage.jsonl")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    path = args.telemetry or os.environ.get("PI_SKILL_TELEMETRY_PATH") or DEFAULT_TELEMETRY_PATH
    events = read_events(path)

    if args.eligible:
        rows = eligible_for_revision(events, telemetry_path=path)
        if args.session:
            used = set(used_in_session(events, args.session))
            rows = [r for r in rows if r.skill in used]
        if args.json:
            print(json.dumps([r.to_dict() for r in rows], indent=1))
        else:
            if not rows:
                print("no skill clears the accumulated-experience bar")
            for r in rows:
                print("%-28s %-6s score=%.1f  %s" % (r.skill, r.kind, r.score, r.reason))
        return 0

    if not args.skill:
        ap.error("provide --skill <name> or --eligible")
    ev = evidence_for(args.skill, events, kind=args.kind, telemetry_path=path)
    if args.json:
        print(json.dumps(ev.to_dict(), indent=1))
    else:
        print("%s (%s): %s" % (ev.skill, ev.kind, "ELIGIBLE" if ev.eligible else "not eligible"))
        print("  " + ev.reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
