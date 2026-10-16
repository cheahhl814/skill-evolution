"""Constraint gates — every evolved variant must pass ALL of them.

1. size limit            2. growth limit vs baseline     3. non-empty
4. structure (frontmatter contract)      5. test suite (optional)
6. skill-forge lint gate (optional, reuses AIx-BIO structural linter)
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from evolution.core.config import EvolutionConfig


@dataclass
class ConstraintResult:
    passed: bool
    constraint_name: str
    message: str
    details: Optional[str] = None


class ConstraintValidator:
    def __init__(self, config: EvolutionConfig):
        self.config = config

    def validate_all(self, text: str, artifact_type: str = "skill", baseline_text: Optional[str] = None) -> list[ConstraintResult]:
        results = [
            self._check_size(text),
            self._check_non_empty(text),
            self._check_skill_structure(text),
        ]
        if baseline_text:
            results.append(self._check_growth(text, baseline_text))
        return results

    def validate_skill_dir(self, skill_dir: Path) -> ConstraintResult:
        """Run skill-forge's domain-agnostic structural linter on the resolved
        skill directory (if available). Exit 0 = no FAIL verdicts."""
        lint = self.config.skillforge_lint
        if not lint or not Path(lint).exists():
            return ConstraintResult(True, "skillforge_lint", "skipped: skill-forge lint not found (set SKILL_FORGE_LINT)")
        try:
            proc = subprocess.run(
                ["python3", str(lint), str(skill_dir), "--json"],
                capture_output=True, text=True, timeout=120,
            )
            passed = proc.returncode == 0
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-8:]
            return ConstraintResult(
                passed,
                "skillforge_lint",
                "no FAIL verdicts" if passed else "skill-forge lint reported FAILs",
                details="\n".join(tail),
            )
        except subprocess.TimeoutExpired:
            return ConstraintResult(False, "skillforge_lint", "lint timed out (120s)")
        except Exception as e:  # gate failure must not crash the loop
            return ConstraintResult(False, "skillforge_lint", f"lint failed to run: {e}")

    def run_test_suite(self, repo: Path, timeout: int = 300) -> ConstraintResult:
        """Optional: run the target repo's pytest as a hard gate."""
        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", "tests/", "-q", "--tb=no"],
                capture_output=True, text=True, timeout=timeout, cwd=str(repo),
            )
            passed = proc.returncode == 0
            tail = (proc.stdout or "").strip().splitlines()[-5:]
            return ConstraintResult(passed, "test_suite", "all tests passed" if passed else "test suite failed", "\n".join(tail))
        except Exception as e:
            return ConstraintResult(False, "test_suite", f"failed to run tests: {e}")

    # ── individual checks ────────────────────────────────────────────────

    def _check_size(self, text: str) -> ConstraintResult:
        size = len(text.encode("utf-8"))
        limit = self.config.max_skill_size
        ok = size <= limit
        return ConstraintResult(ok, "size_limit", f"{'OK' if ok else 'exceeded'}: {size}/{limit} bytes")

    def _check_growth(self, text: str, baseline: str) -> ConstraintResult:
        base = max(1, len(baseline.encode("utf-8")))
        grown = len(text.encode("utf-8")) / base
        ok = grown <= self.config.max_skill_growth
        return ConstraintResult(ok, "growth_limit", f"growth {grown:.2f}x (max {self.config.max_skill_growth:.2f}x)")

    def _check_non_empty(self, text: str) -> ConstraintResult:
        return ConstraintResult(bool(text.strip()), "non_empty", "artifact is non-empty" if text.strip() else "artifact is empty")

    def _check_skill_structure(self, text: str) -> ConstraintResult:
        head = text[:500]
        missing = []
        if not text.strip().startswith("---"):
            missing.append("YAML frontmatter (---)")
        else:
            if "name:" not in head:
                missing.append("name field")
            if "description:" not in head:
                missing.append("description field")
        return ConstraintResult(not missing, "skill_structure", "valid frontmatter (name + description)" if not missing else f"missing: {', '.join(missing)}")