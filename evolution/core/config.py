"""Core config: Pi skill roots, models, budgets.

Env:
  PI_EVOLUTION_SKILLS_ROOTS  ':'-separated extra skill roots to search
  PI_EVOLUTION_OPTIMIZER     optimizer/reflection model (DSPy LM string)
  PI_EVOLUTION_EVAL          eval/judge model (DSPy LM string)
  PI_EVOLUTION_MAX_SKILL     max SKILL.md bytes (default 15360)
  PI_EVOLUTION_MAX_GROWTH    max relative growth vs baseline (default 1.20)
  SKILL_FORGE_LINT           path to skill-lint.py (optional structural gate)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()


def _settings_skill_roots() -> list[str]:
    """Read additional skill roots from ~/.pi/agent/settings.json (skills.* dirs)."""
    settings = HOME / ".pi" / "agent" / "settings.json"
    roots: list[str] = []
    try:
        data = json.loads(settings.read_text())
        custom = data.get("skills") or {}
        if isinstance(custom, dict):
            for key in ("customDirectories", "customRoots", "dirs"):
                v = custom.get(key)
                if isinstance(v, list):
                    roots.extend(str(p) for p in v)
    except Exception:
        pass
    return roots


def default_skill_roots() -> list[Path]:
    env = os.environ.get("PI_EVOLUTION_SKILLS_ROOTS")
    roots = [HOME / ".agents" / "skills", HOME / ".pi" / "agent" / "skills"]
    if env:
        roots.extend(Path(p) for p in env.split(":") if p)
    else:
        roots.extend(Path(p) for p in _settings_skill_roots())
    seen, out = set(), []
    for r in roots:
        rp = r.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def default_skillforge_lint() -> Path | None:
    env = os.environ.get("SKILL_FORGE_LINT")
    if env:
        return Path(env)
    candidate = HOME / ".agents" / "skills" / "skill-forge" / "bin" / "skill-lint.py"
    if candidate.exists():
        return candidate
    return None


@dataclass
class EvolutionConfig:
    skills_roots: list[Path] = field(default_factory=default_skill_roots)
    optimizer_model: str = field(
        default_factory=lambda: os.environ.get("PI_EVOLUTION_OPTIMIZER", "openai/gpt-4.1")
    )
    eval_model: str = field(
        default_factory=lambda: os.environ.get("PI_EVOLUTION_EVAL", "openai/gpt-4.1-mini")
    )
    judge_model: str = field(
        default_factory=lambda: os.environ.get("PI_EVOLUTION_EVAL", "openai/gpt-4.1-mini")
    )
    max_skill_size: int = field(
        default_factory=lambda: int(os.environ.get("PI_EVOLUTION_MAX_SKILL", "15360"))
    )
    max_skill_growth: float = field(
        default_factory=lambda: float(os.environ.get("PI_EVOLUTION_MAX_GROWTH", "1.20"))
    )
    max_tool_desc_size: int = 500
    max_param_desc_size: int = 200
    skillforge_lint: Path | None = field(default_factory=default_skillforge_lint)


def resolve_skill(name: str, config: EvolutionConfig) -> Path | None:
    """Find <root>/<name>/SKILL.md across roots; returns the SKILL.md path
    (through deployment symlinks, which is how Pi itself resolves skills)."""
    for root in config.skills_roots:
        base = root / name
        skill_md = base / "SKILL.md"
        if skill_md.is_file():
            return skill_md.resolve()  # follow symlink: edits land in the source tree
        # frontmatter-name fallback (one level deep)
        if base.is_dir():
            for child in base.rglob("SKILL.md"):
                raw = child.read_text()[:500]
                if f"name: {name}" in raw:
                    return child.resolve()
    return None


def find_git_repo_root(path: Path) -> Path | None:
    """Nearest enclosing .git for a resolved path (deployment is via git branch)."""
    cur = path
    for _ in range(50):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None