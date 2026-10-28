"""Guard the golden eval datasets.

A golden dataset is the input that makes a skill *measurable* — the thing
the triage's `measure` action asks for. If it drifts (bad JSON, missing keys,
a rubric with no load-bearing keywords) the optimizer would score a candidate
against noise, so these checks are a hard gate, not a nicety.
"""

from __future__ import annotations

import json
from pathlib import Path

from evolution.core.dataset_builder import GoldenDatasetLoader

DATASETS_ROOT = Path(__file__).resolve().parents[1] / "datasets" / "skills"

#: A rubric is a keyword target for the overlap metric, not prose. These
#: bounds keep it dense enough to score and long enough to be a rubric.
#: The upper bound is generous because the fitness metric divides by the
#: rubric's word count: a very long rubric lowers the achievable score.
MIN_RUBRIC_WORDS = 6
MAX_RUBRIC_WORDS = 90
DENSE_MIN_WORDS = 12
DENSE_MAX_WORDS = 40


def _golden_files() -> list[Path]:
    return sorted(DATASETS_ROOT.glob("*/golden.jsonl"))


def test_at_least_one_dataset_exists():
    files = _golden_files()
    assert files, "no golden dataset under datasets/skills/*/golden.jsonl"


def test_every_dataset_is_valid_jsonl_with_required_keys():
    for path in _golden_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)  # raises on malformed JSON — that is the point
            assert set(obj.keys()) == {"task_input", "expected_behavior"}, f"{path.name}:{i} keys {set(obj.keys())}"
            assert obj["task_input"].strip(), f"{path.name}:{i} empty task_input"
            assert obj["expected_behavior"].strip(), f"{path.name}:{i} empty expected_behavior"


def test_rubrics_are_dense_enough_to_score():
    for path in _golden_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            rubric = json.loads(line)["expected_behavior"]
            words = len(rubric.split())
            assert MIN_RUBRIC_WORDS <= words <= MAX_RUBRIC_WORDS, f"{path.name}:{i} rubric has {words} words"


def test_datasets_load_through_the_real_loader_without_loss():
    for path in _golden_files():
        raw = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        ds = GoldenDatasetLoader.load(path)
        assert len(ds.train) + len(ds.val) + len(ds.holdout) == len(raw), path.name
        assert ds.train and ds.holdout, path.name


def test_ncal_dataset_covers_the_skills_surface():
    """The ncal golden set must exercise the behaviours the skill exists for."""
    path = DATASETS_ROOT / "ncal" / "golden.jsonl"
    assert path.is_file(), "ncal golden dataset missing"
    blob = " ".join(l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()).lower()
    for needle in ("%a", "%v", "%g", "%j", "+%s", "ncal", "leap", "epoch", "86400"):
        assert needle in blob, f"ncal dataset does not cover {needle!r}"


def test_ncal_rubrics_are_dense_for_the_overlap_metric():
    """The ncal set is scored by a keyword-overlap proxy, so its rubrics must
    stay in the dense band (12-40 words): long enough to constrain the
    behaviour, short enough that a correct answer can cover the keywords."""
    path = DATASETS_ROOT / "ncal" / "golden.jsonl"
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        rubric = json.loads(line)["expected_behavior"]
        words = len(rubric.split())
        assert DENSE_MIN_WORDS <= words <= DENSE_MAX_WORDS, f"ncal rubric {i} has {words} words"
