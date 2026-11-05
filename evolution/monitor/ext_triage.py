"""Extension triage — rank Pi extensions by worthiness of attention.

Counterpart of `triage.py` for the extension blind spot. Actions table
(kind-aware; hook extensions fire every session, so "dust" is meaningless
for them):

| Facts                                              | potential | action    |
| -------------------------------------------------- | --------- | --------- |
| tool-extension with errors in ≥2 sessions          | 0.95      | `fix`     |
| tool-extension, 1 error or errors in 1 session     | 0.55      | `watch`   |
| tool-extension used, error-free                    | 0.40      | `healthy` |
| tool-extension, zero attributed calls in window    | 0.35      | `dusty`   |
| hook/UI extension (no registered tools)            | 0.00      | `inherent` |

Only `fix` is actionable in `suggest()` — a patch to the extension's source
repo (the symlink target), deployed as a local branch for human review.
`dusty` is a human's call (prune or re-scope), like `review` for skills.
`watch`/`healthy`/`inherent` are deliberately not nudged: single errors can
be environmental noise (network timeout, transient 429), and the video's
paper-3 lesson applies — do not confuse randomness with causality. Two
errors inside the window is the floor for concluding a real defect.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .ext_registry import DEFAULT_EXTENSIONS_ROOT, ExtensionFacts, extension_roots, scan_extensions
from .ext_usage import ExtensionUsage, mine_sessions

ACTIONABLE_ACTIONS = frozenset({"fix"})


@dataclass
class ExtTriageRow:
	extension: str
	kind: str
	usage: float
	potential: float
	priority: float
	action: str
	reason: str
	calls: int
	errors: int
	sessions: int
	repo_root: Optional[str] = None

	def to_dict(self) -> dict:
		return {
			"extension": self.extension,
			"kind": self.kind,
			"usage": round(self.usage, 3),
			"potential": round(self.potential, 3),
			"priority": round(self.priority, 3),
			"action": self.action,
			"reason": self.reason,
			"calls": self.calls,
			"errors": self.errors,
			"sessions": self.sessions,
			"repo_root": self.repo_root,
		}


def _potential(facts: ExtensionFacts, usage: ExtensionUsage, window_days: float) -> tuple[float, str, str]:
	if facts.kind != "tool":
		return (0.0, "inherent", "hook/UI-only — fires every session, usage is inherent")
	if usage.calls == 0:
		if facts.commands and not facts.tools:
			return (0.35, "dusty", "slash-command-only: toolResult attribution cannot see command use (v1 limitation) — judge dustiness by hand")
		return (0.35, "dusty", "no attributed tool calls in the last %.0fd — prune it or fix its triggers" % window_days)
	# M4 (review): `fix` requires errors in ≥2 DISTINCT sessions — an agent's
	# own gate refusals ("not approved", "no UI", validation errors) come back
	# as isError toolResults and can cluster in a single session; that is the
	# gate working, not a defect. One flaky error is never `fix` either
	# (randomness ≠ causality, the open-world paper's lesson).
	if usage.errors >= 2 and len(usage.error_sessions) >= 2:
		return (
			0.95,
			"fix",
			"%d failed tool calls across %d sessions out of %d in %.0fd — patch the source repo (branch + human review)"
			% (usage.errors, len(usage.error_sessions), usage.calls, window_days),
		)
	if usage.errors == 1 or (usage.errors >= 2 and len(usage.error_sessions) == 1):
		return (
			0.55,
			"watch",
			"%d failed call(s) in %.0fd (error sessions: %d) — could be environmental noise or clustered gate refusals; check whether it repeats"
			% (usage.errors, window_days, len(usage.error_sessions)),
		)
	return (0.40, "healthy", "%d calls in %.0fd, no errors" % (usage.calls, window_days))


def triage_ext(
	facts: dict[str, ExtensionFacts] | None = None,
	usage: dict[str, ExtensionUsage] | None = None,
	window_days: float = 30.0,
	half_life_days: float = 14.0,
	sessions_root: Optional[Path] = None,
) -> list[ExtTriageRow]:
	"""Rank every registered tool-providing extension + list hook extensions."""
	if facts is None:
		facts = scan_extensions(extension_roots())
	rows: list[ExtTriageRow] = []
	for name, f in sorted(facts.items()):
		u = (usage or {}).get(name) or ExtensionUsage(name=name)
		potential, action, reason = _potential(f, u, window_days)
		rows.append(
			ExtTriageRow(
				extension=name,
				kind=f.kind,
				usage=u.score,
				potential=potential,
				priority=u.score * potential if f.kind == "tool" else 0.0,
				action=action,
				reason=reason,
				calls=u.calls,
				errors=u.errors,
				sessions=len(u.sessions),
				repo_root=f.repo_root,
			)
		)
	rows.sort(key=lambda r: (-r.priority, r.extension))
	return rows


def suggest(rows: list[ExtTriageRow]) -> dict | None:
	"""Highest-priority `fix` target, or None. Dusty/inherent rows are never suggested."""
	for r in rows:
		if r.action in ACTIONABLE_ACTIONS:
			return {
				"extension": r.extension,
				"action": r.action,
				"priority": round(r.priority, 3),
				"repo_root": r.repo_root,
				"reason": r.reason,
			}
	return None


def render_markdown(rows: list[ExtTriageRow], limit: int = 20) -> str:
	out = [
		"Extensions are monitored post-hoc (no runtime hooks): the registry maps",
		"tool names to deployments and session JSONL toolResults are attributed.",
		"",
		"| # | Extension | Kind | Calls | Errors | Sessions | Action | Why |",
		"| --- | --- | --- | --- | --- | --- | --- | --- |",
	]
	for i, r in enumerate(rows[:limit], start=1):
		out.append(
			"| %d | `%s` | %s | %d | %d | %d | **%s** | %s |"
			% (i, r.extension, r.kind, r.calls, r.errors, r.sessions, r.action, r.reason)
		)
	return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
	import argparse

	ap = argparse.ArgumentParser(
		prog="ext-triage",
		description="Rank Pi extensions (usage x error signals) from session history. Read-only; zero model cost.",
	)
	ap.add_argument("--window", type=float, default=30.0, help="window in days (default 30)")
	ap.add_argument("--half-life", type=float, default=14.0, help="recency half-life in days")
	ap.add_argument("--sessions-root", default=None, help="override the Pi sessions root")
	ap.add_argument("--root", action="append", default=[], help="extra extension root (repeatable)")
	ap.add_argument("--json", action="store_true", help="emit JSON instead of a markdown table")
	ap.add_argument("--md", action="store_true", help="markdown table (default; explicit form for the digest)")
	ap.add_argument("--suggest", action="store_true", help="print one actionable fix target as JSON or null")
	ap.add_argument("--limit", type=int, default=20)
	args = ap.parse_args([a for a in (argv if argv is not None else sys.argv[1:]) if a != "--extensions"])

	from .ext_registry import scan_extensions

	facts = scan_extensions(extension_roots(args.root) if args.root else None)
	usage = mine_sessions(
		window_days=args.window,
		half_life_days=args.half_life,
		sessions_root=Path(args.sessions_root) if args.sessions_root else None,
		facts=facts,
	)
	rows = triage_ext(facts=facts, usage=usage, window_days=args.window)

	if args.suggest:
		print(json.dumps(suggest(rows)))
		return 0
	if args.json:
		print(json.dumps([r.to_dict() for r in rows], indent=2))
	else:
		print(render_markdown(rows, limit=args.limit))
	return 0


if __name__ == "__main__":
	sys.exit(main())
