"""Extension usage mining — attribute recorded session tool calls to extensions.

How it works (post-hoc, zero runtime cost)
------------------------------------------
Pi session JSONLs record every tool result:

    {"type": "message", "message": {"role": "toolResult", "toolCallId": ...,
     "toolName": "bash", "isError": false, "durationMs": 5014, ...}}

`ext_registry` maps extension → tool names; this module inverts that map and
counts, per extension, over a sliding window:

    calls — toolResult records owned by the extension
    errors — toolResults with isError: true
    score  — recency-decayed usage, `sum(0.5 ** (age_days / half_life))`
             capped at 1.0 (mirrors the skill usage_score semantics)

No new events are written: this module only reads sessions. If a session file
is unreadable the entry is skipped, not fatal — mining is best-effort by
design. Tool names that no extension claims (pi built-ins like `bash`) are
ignored: they are the harness itself, not an extension.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .ext_registry import ExtensionFacts, extension_roots, scan_extensions

DEFAULT_SESSIONS_ROOT = Path.home() / ".pi" / "agent" / "sessions"


@dataclass
class ExtensionUsage:
	"""Aggregated attributable usage for one extension over the window."""

	name: str
	calls: int = 0
	errors: int = 0
	sessions: set[str] = field(default_factory=set)
	error_sessions: set[str] = field(default_factory=set)
	#: tool names claimed by >1 extension - attribution is split across owners
	ambiguous_tools: set[str] = field(default_factory=set)
	score: float = 0.0

	@property
	def error_rate(self) -> float:
		return self.errors / self.calls if self.calls else 0.0

	@property
	def used(self) -> bool:
		return self.calls > 0

	def to_dict(self) -> dict:
		return {
			"calls": self.calls,
			"errors": self.errors,
			"sessions": len(self.sessions),
			"score": round(self.score, 3),
		}


def _parse_ts(value: object) -> Optional[float]:
	"""Epoch seconds (float>1e9), epoch ms (>1e12), or ISO string → seconds.
	M3 (review): returns None only for truly unparseable values; the caller
	SKIPS such records instead of counting them as age-0."""
	if isinstance(value, (int, float)):
		v = float(value)
		if v > 1e12:  # epoch ms
			return v / 1000.0
		if v > 1e9:  # epoch seconds
			return v
		return None
	if isinstance(value, str):
		t = value.strip().replace("Z", "+00:00")
		try:
			return datetime.fromisoformat(t).timestamp()
		except ValueError:
			return None
	return None


def tool_ownership(facts: dict[str, ExtensionFacts]) -> dict[str, list[str]]:
	"""Invert the registry: tool (or command) name → deploying extension names."""
	own: dict[str, list[str]] = {}
	for name, f in facts.items():
		for t in list(f.tools) + list(f.commands):
			own.setdefault(t, []).append(name)
	return own


def mine_sessions(
	window_days: float = 30.0,
	sessions_root: Optional[Path] = None,
	now: float | None = None,
	facts: dict[str, ExtensionFacts] | None = None,
	half_life_days: float = 14.0,
) -> dict[str, ExtensionUsage]:
	"""Attribute session toolResult records to extensions via the registry."""
	own = tool_ownership(facts if facts is not None else scan_extensions(extension_roots()))
	usage: dict[str, ExtensionUsage] = {name: ExtensionUsage(name=name) for name in own_names(own)}

	root = sessions_root or Path(os.environ.get("PI_SESSIONS_ROOT", "") or DEFAULT_SESSIONS_ROOT)
	now_ts = now if now is not None else datetime.now(timezone.utc).timestamp()
	cutoff = now_ts - max(0.1, window_days) * 86400.0
	half_life = max(0.5, half_life_days)

	if not root.is_dir() or not own:
		return usage

	for path in sorted(root.glob("*/*.jsonl")):
		session_id = path.stem
		try:
			with path.open() as fh:
				for line in fh:
					line = line.strip()
					if not line:
						continue
					try:
						rec = json.loads(line)
					except (json.JSONDecodeError, ValueError):
						continue
					m = rec.get("message")
					if not isinstance(m, dict) or m.get("role") != "toolResult":
						continue
					tool = str(m.get("toolName") or "")
					owners = own.get(tool)
					if not owners:
						continue
					ts = _parse_ts(rec.get("timestamp"))
					if ts is None:
						# M3 (review): a record with an unparseable/missing timestamp
						# cannot be window-filtered or decayed — skip it instead of
						# pretending it happened now.
						continue
					if ts < cutoff:
						continue
					is_error = bool(m.get("isError"))
					age_days = max(0.0, (now_ts - ts) / 86400.0) if ts is not None else 0.0
					decay = 0.5 ** (age_days / half_life)
					if len(owners) > 1:
						for _own in owners:
							usage.setdefault(_own, ExtensionUsage(name=_own)).ambiguous_tools.add(tool)
					for owner in owners:
						u = usage.get(owner)
						if u is None:
							u = usage[owner] = ExtensionUsage(name=owner)
						u.calls += 1
						u.score = min(1.0, u.score + decay)
						u.sessions.add(session_id)
						if is_error:
							u.errors += 1
							u.error_sessions.add(session_id)
		except OSError:
			continue
	return usage


def own_names(own: dict[str, list[str]]) -> list[str]:
	names: list[str] = []
	for exts in own.values():
		for e in exts:
			if e not in names:
				names.append(e)
	return names
