"""Tests for the candidate command smoke gate (`bin/smoke-candidate`).

The gate exists because a candidate can pass every constraint, beat the
baseline score, and still contain a command that fails when it is executed.
These tests pin the three behaviours that matter: a clean skill passes, a
skill with a broken command fails, and a command that would smuggle in
another program is skipped rather than executed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "bin" / "smoke-candidate"


def _run(tmp_path: Path, body: str) -> tuple[int, dict]:
    skill = tmp_path / "SKILL.md"
    skill.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(GATE), str(skill), "--json"], capture_output=True, text=True, timeout=120
    )
    try:
        report = json.loads(proc.stdout or "{}")
    except ValueError:
        report = {}
    return proc.returncode, report


def test_gate_exists_and_is_executable():
    assert GATE.is_file(), "bin/smoke-candidate missing"
    assert GATE.stat().st_mode & 0o111, "bin/smoke-candidate is not executable"


def test_clean_skill_passes(tmp_path):
    body = """# clean
Use `date +%Y-%m-%d` for today and `date -d "2024-03-15" +%A` for a weekday.
Also `ncal 12 2026` for a month grid.
"""
    rc, report = _run(tmp_path, body)
    assert rc == 0, report
    assert report["failed"] == 0
    assert report["total"] >= 3


def test_broken_command_fails(tmp_path):
    body = """# broken
Use `date -d "2024-03-15" +%V / %G` to get the ISO week and year.
"""
    rc, report = _run(tmp_path, body)
    assert rc == 1, "a command that errors must fail the gate"
    assert report["failed"] >= 1
    failed = [r for r in report["results"] if r["status"] == "FAIL"]
    assert any("extra operand" in r["stderr"] or "invalid" in r["stderr"] for r in failed)


def test_shell_metacharacters_are_skipped_not_executed(tmp_path):
    marker = tmp_path / "pwned"
    body = f"""# hostile
Run `date -d "$(touch {marker})"` to check the date.
"""
    rc, report = _run(tmp_path, body)
    assert not marker.exists(), "the gate executed a command it must not run"
    assert report["failed"] == 0
    assert any(r["status"] == "skipped" for r in report["results"])


def test_non_allowlisted_binary_is_skipped(tmp_path):
    body = """# other
Run `curl http://example.com` and `rm -rf /tmp/x` to check things.
"""
    rc, report = _run(tmp_path, body)
    assert rc == 0
    assert all(r["status"] == "skipped" for r in report["results"])


def test_leap_year_probe_error_is_expected_not_a_failure(tmp_path):
    body = """# leap
Use `date -d "2023-02-29" +%Y 2>&1` to test a non-leap year.
"""
    rc, report = _run(tmp_path, body)
    assert rc == 0, "a deliberate leap-year probe must not fail the gate"
    assert any(r["status"] == "expected-error" for r in report["results"])
