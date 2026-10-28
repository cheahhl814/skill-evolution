"""Per-skill usage telemetry — read + aggregate the Layer-1 JSONL feed.

The Layer-1 extension (`layer1/extension/skill-memory.ts`) appends one JSON
object per usage event to a JSONL file. This module is the reader half of
that contract (see `layer1/telemetry-schema.md`).

Design notes:

- Malformed lines are skipped, never fatal: a truncated write (crash mid-append)
  must not break triage.
- Unknown `event` values are ignored, so a newer extension can add events
  without breaking an older reader.
- The usage score is recency-weighted with a configurable half-life. A skill
  used every day should outrank one used once a month, even if the latter has
  a higher raw count.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TELEMETRY_PATH = Path.home() / ".local" / "share" / "skill-evolution" / "skill-usage.jsonl"

#: Relative contribution of each event type to the usage score.
EVENT_WEIGHTS: dict[str, float] = {
    "skill_loaded": 1.0,
    "skill_read": 0.6,
    "skill_managed": 0.8,
    "skill_failed": 1.5,
    "learning_tool": 0.05,
    # A proposal is a *conclusion*, not a use: it must not inflate the usage
    # score (it would make a barely-used skill look important), but it is the
    # accumulated evidence behind a create/revise conclusion.
    "skill_proposed": 0.0,
}

#: Days after which an event counts half as much.
DEFAULT_HALF_LIFE_DAYS = 14.0


@dataclass
class SkillUsage:
    """Aggregated usage for a single skill."""

    skill: str
    loads: int = 0
    reads: int = 0
    edits: int = 0
    failures: int = 0
    learning_calls: int = 0
    proposals: int = 0
    sessions: set[str] = field(default_factory=set)
    days: set[str] = field(default_factory=set)
    first_seen: float = 0.0
    last_seen: float = 0.0
    score: float = 0.0
    raw_score: float = 0.0
    #: Sessions in which the skill was actually loaded or read (the "breadth"
    #: signal — a skill used in one session is a one-off, not a pattern).
    use_sessions: set[str] = field(default_factory=set)
    #: Per-session count of loads+reads. A skill consulted several times in one
    #: session is the clearest "the skill did not have the answer" signal.
    session_uses: dict[str, int] = field(default_factory=dict)
    #: Sessions in which a revision/create was proposed for this skill.
    proposal_sessions: set[str] = field(default_factory=set)

    @property
    def used(self) -> bool:
        """True when the skill was loaded, read or authored at least once."""
        return bool(self.loads or self.reads or self.edits)

    @property
    def churn(self) -> int:
        """Number of write-side operations (authoring is a churn signal)."""
        return self.edits

    @property
    def revisits(self) -> int:
        """Highest number of loads+reads inside any single session."""
        return max(self.session_uses.values()) if self.session_uses else 0

    def to_dict(self) -> dict:
        return {
            "skill": self.skill,
            "loads": self.loads,
            "reads": self.reads,
            "edits": self.edits,
            "failures": self.failures,
            "learning_calls": self.learning_calls,
            "proposals": self.proposals,
            "sessions": len(self.sessions),
            "use_sessions": len(self.use_sessions),
            "revisits": self.revisits,
            "proposal_sessions": len(self.proposal_sessions),
            "days": len(self.days),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "score": round(self.score, 3),
            "raw_score": round(self.raw_score, 3),
        }


def read_events(path: Path | str | None = None) -> list[dict]:
    """Read the telemetry JSONL. Missing file -> empty list; bad lines skipped."""
    p = Path(path) if path else Path(os.environ.get("PI_SKILL_TELEMETRY_PATH") or DEFAULT_TELEMETRY_PATH)
    if not p.exists():
        return []
    events: list[dict] = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("event"), str):
                    events.append(obj)
    except OSError:
        return []
    return events


def _parse_ts(value: object) -> float:
    """ISO-8601 -> epoch seconds; 0.0 when unparseable."""
    if not isinstance(value, str) or not value:
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def aggregate(
    events: list[dict],
    now: float | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> dict[str, SkillUsage]:
    """Aggregate raw events into per-skill usage with a recency decay.

    The score is `sum(weight * 0.5 ** (age_days / half_life_days))` over the
    skill's events, so recent usage dominates and stale usage fades out.
    """
    now = now if now is not None else time.time()
    half_life = max(0.5, float(half_life_days))
    usage: dict[str, SkillUsage] = {}

    for ev in events:
        kind = ev.get("event")
        weight = EVENT_WEIGHTS.get(kind)
        if weight is None:
            continue  # unknown / global event: forward-compatible skip
        ts = _parse_ts(ev.get("ts"))
        if ts <= 0:
            ts = now
        age_days = max(0.0, (now - ts) / 86400.0)
        decay = 0.5 ** (age_days / half_life)
        session = str(ev.get("session") or "")
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))

        name = ev.get("skill")
        if kind == "learning_tool" or not isinstance(name, str) or not name:
            # global learning signal: keep it on a synthetic bucket
            name = "__learning__"

        rec = usage.get(name)
        if rec is None:
            rec = usage[name] = SkillUsage(skill=name)
        rec.raw_score += weight
        rec.score += weight * decay
        if session:
            rec.sessions.add(session)
        rec.days.add(day)
        if rec.first_seen == 0.0 or ts < rec.first_seen:
            rec.first_seen = ts
        if ts > rec.last_seen:
            rec.last_seen = ts
        if kind in ("skill_loaded", "skill_read"):
            if session:
                rec.use_sessions.add(session)
                rec.session_uses[session] = rec.session_uses.get(session, 0) + 1
        if kind == "skill_loaded":
            rec.loads += 1
        elif kind == "skill_read":
            rec.reads += 1
        elif kind == "skill_managed":
            rec.edits += 1
        elif kind == "skill_failed":
            rec.failures += 1
        elif kind == "skill_proposed":
            rec.proposals += 1
            if session:
                rec.proposal_sessions.add(session)
        elif kind == "learning_tool":
            rec.learning_calls += 1

    return usage


def load_usage(
    path: Path | str | None = None,
    now: float | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> dict[str, SkillUsage]:
    """Convenience: read + aggregate in one call."""
    return aggregate(read_events(path), now=now, half_life_days=half_life_days)
