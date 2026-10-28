"""Triage — rank skills by "what is worth evolving" before spending tokens.

This is the module Hermes upstream never built: `evolution/monitor/` in
NousResearch/hermes-agent-self-evolution is a one-line placeholder
("Phase placeholder: monitor evolution."). Hermes' PLAN.md Phase 5 describes
the intended logic:

    * performance monitor — per-skill success rates, tool-selection accuracy,
      benchmark scores over time
    * auto-triage — "rank by (potential improvement x usage frequency)"
    * scheduled optimization — cron job, threshold-triggered GEPA, PR, human merge

This port implements the *triage* half against the signals Pi actually has:
per-skill usage telemetry (Layer 1) + structural facts about each skill
(size, churn, whether it has a golden eval set). It deliberately does NOT
auto-run GEPA: the optimizer's own cost gate and the human merge stay manual.

Scoring
-------
    priority = usage_score x potential

`usage_score` is the recency-decayed telemetry score (see telemetry.py).
`potential` (0..1) is the estimated payoff of optimizing that skill, derived
from structural signals:

| Condition                                   | potential | action    |
| ------------------------------------------- | --------- | --------- |
| used, no golden dataset                     | 0.90      | `measure` |
| used, golden dataset, over the size cap     | 0.75      | `shrink`  |
| used, >= 3 authoring ops (churn)            | 0.70      | `stabilize` |
| used, golden dataset                        | 0.60      | `evolve`  |
| never used but installed                    | 0.35      | `review`  |

`measure` outranks `evolve` on purpose: a skill that is used but has no eval
set cannot be evolved responsibly yet — you need the golden JSONL first.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .telemetry import SkillUsage, load_usage

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASETS_ROOT = REPO_ROOT / "datasets" / "skills"
DEFAULT_SIZE_CAP = 15360
DEFAULT_PRIMARY_ROOT = Path.home() / ".agents" / "skills"


@dataclass
class SkillFacts:
    """Structural facts about an installed skill."""

    name: str
    path: str
    size_bytes: int
    has_golden: bool
    golden_path: str | None = None

    @property
    def over_cap(self) -> bool:
        return self.size_bytes > DEFAULT_SIZE_CAP


@dataclass
class TriageRow:
    """One ranked triage result."""

    skill: str
    usage: float
    potential: float
    priority: float
    action: str
    reason: str
    facts: SkillFacts | None = None
    usage_detail: SkillUsage | None = None

    def to_dict(self) -> dict:
        return {
            "skill": self.skill,
            "usage": round(self.usage, 3),
            "potential": round(self.potential, 3),
            "priority": round(self.priority, 3),
            "action": self.action,
            "reason": self.reason,
            "size_bytes": self.facts.size_bytes if self.facts else None,
            "over_cap": self.facts.over_cap if self.facts else None,
            "has_golden": self.facts.has_golden if self.facts else None,
            "loads": self.usage_detail.loads if self.usage_detail else 0,
            "reads": self.usage_detail.reads if self.usage_detail else 0,
            "edits": self.usage_detail.edits if self.usage_detail else 0,
            "failures": self.usage_detail.failures if self.usage_detail else 0,
            "sessions": len(self.usage_detail.sessions) if self.usage_detail else 0,
        }


def skill_roots(extra: list[str] | None = None) -> list[Path]:
    """Skill roots: the native root plus PI_SKILL_MANAGE_ROOTS (same rule as Layer 1)."""
    roots = [DEFAULT_PRIMARY_ROOT]
    raw = extra if extra is not None else (os.environ.get("PI_SKILL_MANAGE_ROOTS") or "").split(":")
    for r in raw:
        if r:
            roots.append(Path(r).expanduser())
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        try:
            rp = r.resolve()
        except OSError:
            continue
        if rp not in seen and rp.is_dir():
            seen.add(rp)
            out.append(rp)
    return out


def inventory(roots: list[Path] | None = None, datasets_root: Path | None = None) -> dict[str, SkillFacts]:
    """Collect structural facts for every installed skill (SKILL.md present)."""
    ds_root = datasets_root or DATASETS_ROOT
    facts: dict[str, SkillFacts] = {}
    for root in roots or skill_roots():
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            skill_md = entry / "SKILL.md"
            try:
                if not skill_md.is_file():
                    continue
                size = skill_md.stat().st_size
            except OSError:
                continue
            name = entry.name
            golden = ds_root / name / "golden.jsonl"
            has_golden = golden.is_file()
            facts[name] = SkillFacts(
                name=name,
                path=str(skill_md),
                size_bytes=size,
                has_golden=has_golden,
                golden_path=str(golden) if has_golden else None,
            )
    return facts


def _potential(facts: SkillFacts | None, usage: SkillUsage | None) -> tuple[float, str, str]:
    """Return (potential, action, reason) for one skill."""
    used = usage.used if usage else False
    churn = usage.churn if usage else 0
    failures = usage.failures if usage else 0
    has_golden = facts.has_golden if facts else False
    over_cap = facts.over_cap if facts else False

    if used and not has_golden:
        return (
            0.90,
            "measure",
            "used but has no golden eval set — build datasets/skills/%s/golden.jsonl before evolving" % (facts.name if facts else "?"),
        )
    if used and over_cap:
        return (
            0.75,
            "shrink",
            "used and %d bytes (over the %d cap) — evolve to shrink, or raise PI_EVOLUTION_MAX_SKILL"
            % (facts.size_bytes if facts else 0, DEFAULT_SIZE_CAP),
        )
    if used and churn >= 3:
        return (
            0.70,
            "stabilize",
            "used but rewritten %d times — the body is unstable; rewrite before optimizing" % churn,
        )
    if used and has_golden:
        return (0.60, "evolve", "used and measured — GEPA candidate (human merge required)")
    if used and failures:
        return (
            0.55,
            "debug",
            "used but %d failed authoring ops — fix the failure signature before optimizing" % failures,
        )
    if not used:
        return (
            0.35,
            "review",
            "installed but never used — fix the description trigger or prune it",
        )
    return (0.0, "none", "")


def triage(
    usage: dict[str, SkillUsage] | None = None,
    facts: dict[str, SkillFacts] | None = None,
    telemetry_path: Path | str | None = None,
    half_life_days: float = 14.0,
    now: float | None = None,
) -> list[TriageRow]:
    """Rank every known skill (used or merely installed) by evolve-worthiness."""
    if usage is None:
        usage = load_usage(telemetry_path, now=now, half_life_days=half_life_days)
    if facts is None:
        facts = inventory()

    names = set(usage.keys()) | set(facts.keys())
    names.discard("__learning__")

    rows: list[TriageRow] = []
    for name in names:
        u = usage.get(name)
        f = facts.get(name)
        potential, action, reason = _potential(f, u)
        usage_score = u.score if u else 0.0
        rows.append(
            TriageRow(
                skill=name,
                usage=usage_score,
                potential=potential,
                priority=usage_score * potential,
                action=action,
                reason=reason,
                facts=f,
                usage_detail=u,
            )
        )
    rows.sort(key=lambda r: (-r.priority, r.skill))
    return rows


#: Actions that mean "there is work to do here" — a skill with any of these is
#: worth suggesting to the agent. `review` is deliberately excluded: it means
#: "fix the trigger or prune it", which is a human's call, not a skill run.
ACTIONABLE_ACTIONS: frozenset[str] = frozenset({"measure", "evolve", "shrink", "stabilize", "debug"})


def suggest(rows: list[TriageRow]) -> dict | None:
    """Pick the single highest-priority actionable target, or None.

    This is what the Layer-1 extension injects into the prompt so the agent
    knows which skill to work on next. It is a *suggestion*: the agent still
    needs the user's request and the ask-user cost gate before any spend.

    A row must also clear the accumulated-experience bar (see
    `evolution/monitor/evidence.py`) to be suggested at all: a skill used in
    one session is not a conclusion, and a well-used skill needs no work. The
    bar is what keeps the runtime from nudging unrelated sessions into
    "improving" a skill that nobody found deficient.
    """
    from .evidence import KIND_REVISE, evaluate

    for r in rows:
        if r.action not in ACTIONABLE_ACTIONS or r.usage <= 0:
            continue
        ev = evaluate(r.skill, r.usage_detail, kind=KIND_REVISE)
        if not ev.eligible:
            continue
        return {
            "skill": r.skill,
            "action": r.action,
            "priority": round(r.priority, 3),
            "usage": round(r.usage, 3),
            "reason": r.reason,
            "evidence": ev.reason,
        }
    return None


def render_markdown(rows: list[TriageRow], limit: int = 20, learning_calls: int = 0) -> str:
    """Human/agent-readable triage table."""
    if not rows:
        return "No skills found — is PI_SKILL_MANAGE_ROOTS set and ~/.agents/skills populated?"
    out: list[str] = []
    if all(r.usage == 0 for r in rows):
        out = [
            "_No telemetry yet — every skill shows usage 0. The Layer-1 extension writes telemetry on",
            "`skill_search(action=load)` and on `read` of a `SKILL.md`; check the JSONL at",
            "`PI_SKILL_TELEMETRY_PATH` (default `~/.local/share/skill-evolution/skill-usage.jsonl`) and",
            "make sure the extension was reloaded after the telemetry change._",
            "",
        ]
    out = out + ["| # | Skill | Usage | Potential | Priority | Action | Why |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(rows[:limit], start=1):
        out.append(
            "| %d | `%s` | %.2f | %.2f | %.2f | **%s** | %s |"
            % (i, r.skill, r.usage, r.potential, r.priority, r.action, r.reason)
        )
    if learning_calls:
        out.append("")
        out.append("_Learning-tool calls (global, all skills): %d_" % learning_calls)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="triage",
        description="Rank skills by evolve-worthiness (usage x potential) from Layer-1 telemetry.",
    )
    ap.add_argument("--telemetry", default=None, help="path to skill-usage.jsonl")
    ap.add_argument("--root", action="append", default=[], help="extra skill root (repeatable)")
    ap.add_argument("--half-life", type=float, default=14.0, help="recency half-life in days")
    ap.add_argument("--limit", type=int, default=20, help="rows to print")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--only-action", default=None, help="filter rows by action (measure/evolve/shrink/...)")
    ap.add_argument(
        "--suggest",
        action="store_true",
        help="print ONE actionable target as JSON (the extension's prompt suggestion), or null",
    )
    args = ap.parse_args(argv)

    usage = load_usage(args.telemetry, half_life_days=args.half_life)
    facts = inventory(skill_roots(args.root) if args.root else None)
    rows = triage(usage=usage, facts=facts)
    if args.only_action:
        rows = [r for r in rows if r.action == args.only_action]

    if args.suggest:
        print(json.dumps(suggest(rows)))
        return 0
    if args.json:
        print(json.dumps([r.to_dict() for r in rows], indent=2))
    else:
        learning = usage.get("__learning__")
        print(render_markdown(rows, limit=args.limit, learning_calls=learning.learning_calls if learning else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
