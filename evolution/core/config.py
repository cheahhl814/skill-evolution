"""Core config: Pi skill roots, models, budgets.

Env:
  PI_EVOLUTION_SKILLS_ROOTS  ':'-separated extra skill roots to search
  PI_EVOLUTION_OPTIMIZER     optimizer/reflection model (DSPy LM string)
  PI_EVOLUTION_EVAL          eval model (DSPy LM string)
  PI_EVOLUTION_JUDGE         judge model (DSPy LM string; falls back to PI_EVOLUTION_EVAL)
  PI_EVOLUTION_VERIFIER      verifier model (DSPy LM string; gate off when unset)
  PI_EVOLUTION_MAX_SKILL     max SKILL.md bytes (default 15360)
  PI_EVOLUTION_MAX_GROWTH    max relative growth vs baseline (default 1.20)
  SKILL_FORGE_LINT           path to skill-lint.py (optional structural gate)
"""

from __future__ import annotations

import json
import os
import re
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


# Canonical family patterns — ordered, first match wins.  Lower-cased model
# string is tested against each regex.  Ollama-cloud gpt-* is gpt-oss →
# OpenAI family (same training lineage for diversity purposes).
FAMILY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"gpt-?oss"), "openai"),
    (re.compile(r"\bgpt[-_]?4"), "openai"),
    (re.compile(r"\bgpt[-_]?3"), "openai"),
    (re.compile(r"\bopenai"), "openai"),
    (re.compile(r"claude"), "anthropic"),
    (re.compile(r"anthropic"), "anthropic"),
    (re.compile(r"gemma"), "gemma"),
    (re.compile(r"\bglm"), "glm"),
    (re.compile(r"kimi"), "kimi"),
    (re.compile(r"minimax"), "minimax"),
    (re.compile(r"mistral"), "mistral"),
    (re.compile(r"nemotron"), "nemotron"),
    (re.compile(r"deepseek"), "deepseek"),
    (re.compile(r"\bmimo"), "mimo"),
    (re.compile(r"qwen"), "qwen"),
    (re.compile(r"llama"), "meta"),
]


def _resolve_judge_model() -> str:
    """PI_EVOLUTION_JUDGE when set, otherwise fall back to PI_EVOLUTION_EVAL
    for backwards compatibility (pre-split single eval/judge var)."""
    return os.environ.get(
        "PI_EVOLUTION_JUDGE",
        os.environ.get("PI_EVOLUTION_EVAL", "openai/gpt-4.1-mini"),
    )


def family(model: str | None) -> str | None:
    """Canonical family for a DSPy/LiteLLM model string, or None when
    unknown (fail-closed — caller must refuse to run the gate and require
    an explicit --verifier-family-override)."""
    if not model:
        return None
    s = model.lower().strip()
    # Strip common routing prefixes: openai/, ollama_chat/, ollama/, anthropic/,
    # openrouter/<vendor>/ (two segments).  Normalise to the model id part.
    # We keep the full string too — patterns match anywhere, so prefix
    # stripping is lenient rather than exact.
    for pat, fam in FAMILY_PATTERNS:
        if pat.search(s):
            return fam
    return None


def check_family_diversity(
    config: "EvolutionConfig",
    verifier_family_override: str | None = None,
) -> None:
    """Enforce that the verifier family differs from all three of
    optimizer/eval/judge.  Unknown verifier family is refused unless
    verifier_family_override is given.  Called at startup including
    --dry-run so misconfiguration is caught for free."""
    vm = config.verifier_model
    if not vm:
        return  # gate off
    vfam = verifier_family_override or family(vm)
    if vfam is None:
        raise SystemExit(
            f"unknown verifier family for model '{vm}' — refusing to run the gate; "
            f"set --verifier-family-override <family> or PI_EVOLUTION_VERIFIER to a known model"
        )
    # Build role→family map for the three in-loop roles
    roles = {
        "optimizer": family(config.optimizer_model),
        "eval": family(config.eval_model),
        "judge": family(config.judge_model),
    }
    for role, fam in roles.items():
        if fam is not None and fam == vfam:
            raise SystemExit(
                f"verifier family '{vfam}' collides with {role} family '{fam}' "
                f"(verifier={vm!r}); verifier must differ from all of optimizer/eval/judge"
            )
    # Also refuse weak verifier tier unless overridden
    if not verifier_family_override:
        low = vm.lower()
        if any(tag in low for tag in ("-mini", "-small", "-nano", ":small", "-haiku")):
            raise SystemExit(
                f"weak verifier tier refused: '{vm}' looks like a -mini/small model; "
                f"use a full-size verifier or pass --verifier-family-override to override"
            )


@dataclass
class EvolutionConfig:
    skills_roots: list[Path] = field(default_factory=default_skill_roots)
    optimizer_model: str = field(
        default_factory=lambda: os.environ.get("PI_EVOLUTION_OPTIMIZER", "openai/gpt-4.1")
    )
    eval_model: str = field(
        default_factory=lambda: os.environ.get("PI_EVOLUTION_EVAL", "openai/gpt-4.1-mini")
    )
    judge_model: str = field(default_factory=_resolve_judge_model)
    verifier_model: str | None = field(
        default_factory=lambda: os.environ.get("PI_EVOLUTION_VERIFIER")
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