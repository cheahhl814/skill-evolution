"""Mine Pi session history (~/.pi/agent/sessions/**) for eval data.

Pi session JSONL format (observed, pi 1.2.0):
  {"type":"session", ...}
  {"type":"message", "message":{"role":"user"|"assistant","content":[{"type":"text","text":...}, ...], ...}}
  ...other event types (model_change, thinking_level_change, tool calls) are skipped.

Returns (meta, user_text, assistant_text) triples for completed exchanges.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HOME = Path.home()


@dataclass
class SessionPair:
    session_id: str
    cwd: str
    timestamp: str
    user_text: str
    assistant_text: str


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
    return "\n".join(parts)


def iter_session_files(sessions_root: Optional[Path] = None):
    root = sessions_root or (HOME / ".pi" / "agent" / "sessions")
    if not root.exists():
        return
    for f in sorted(root.glob("*/*.jsonl")):
        yield f


def extract_pairs(path: Path, limit: int = 0) -> list[SessionPair]:
    pairs: list[SessionPair] = []
    session_id = path.stem
    cwd = ""
    pending_user: Optional[tuple[str, str]] = None  # (text, timestamp)
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = entry.get("type")
                if etype == "session":
                    session_id = str(entry.get("id", session_id))
                    cwd = str(entry.get("cwd", ""))
                elif etype == "message":
                    msg = entry.get("message") or {}
                    role = msg.get("role")
                    text = _content_to_text(msg.get("content")).strip()
                    ts = str(entry.get("timestamp", ""))
                    if role == "user" and text and not text.startswith("["):
                        pending_user = (text, ts)
                    elif role == "assistant" and text and pending_user:
                        pairs.append(
                            SessionPair(
                                session_id=session_id,
                                cwd=cwd,
                                timestamp=pending_user[1],
                                user_text=pending_user[0][:4000],
                                assistant_text=text[:4000],
                            )
                        )
                        pending_user = None
                        if limit and len(pairs) >= limit:
                            break
    except OSError:
        pass
    return pairs


def mine_pi_sessions(skill_name: str, limit: int = 200, sessions_root: Optional[Path] = None) -> list[SessionPair]:
    """Heuristic relevance filter (no API cost): the user message mentions the
    skill name, its kebab/snake/word forms, or asks to 'forge/create/use' it."""
    import re

    tokens = {skill_name}
    tokens.update(re.split(r"[-_]", skill_name))
    # skip ultra-generic single-token noise
    generic = {"use", "the", "skill", "a", "of"}
    tokens = {t.lower() for t in tokens if len(t) > 3 and t.lower() not in generic}

    out: list[SessionPair] = []
    for f in iter_session_files(sessions_root):
        for pair in extract_pairs(f):
            low = " ".join((pair.user_text + " " + pair.assistant_text)).lower()
            if any(t in low for t in tokens):
                out.append(pair)
                if len(out) >= limit:
                    return out
    return out