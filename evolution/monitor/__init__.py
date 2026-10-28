"""Monitor: the Layer-1 signal feed for Layer-2 triage.

Upstream Hermes has the same module name as an empty placeholder
(`evolution/monitor/__init__.py` in NousResearch/hermes-agent-self-evolution
contains only a "Phase placeholder: monitor evolution." docstring). This
port fills it in: it reads the per-skill usage telemetry that the Layer-1
extension writes and turns it into a ranked "what is worth evolving" list.

See `layer1/telemetry-schema.md` for the wire contract.

Exports are lazy (PEP 562) so that `python -m evolution.monitor.triage` does
not import the submodule twice — the eager version emitted a RuntimeWarning
about `evolution.monitor.triage` already being in sys.modules.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DEFAULT_TELEMETRY_PATH",
    "SkillUsage",
    "TriageRow",
    "aggregate",
    "read_events",
    "render_markdown",
    "triage",
]


def __getattr__(name: str) -> Any:
    if name in {"DEFAULT_TELEMETRY_PATH", "SkillUsage", "aggregate", "read_events"}:
        from . import telemetry

        return getattr(telemetry, name)
    if name in {"TriageRow", "render_markdown", "triage"}:
        from . import triage as _triage

        return getattr(_triage, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
