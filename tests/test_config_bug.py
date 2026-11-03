"""Regression: evolution/skills/evolve_skill.py must not access config.max_skill.

The field is `max_skill_size`. Two call sites (lines 174, 219) previously used
`config.max_skill` and raised AttributeError on the default --metric judge path
before GEPA started, with no test exercising that branch.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution.core.config import EvolutionConfig


def test_config_has_no_max_skill_alias():
    cfg = EvolutionConfig()
    assert hasattr(cfg, "max_skill_size")
    assert not hasattr(cfg, "max_skill"), "stale alias config.max_skill must not exist"


def test_evolve_skill_never_reads_max_skill():
    src = (Path(__file__).parent.parent / "evolution" / "skills" / "evolve_skill.py").read_text()
    # Exact attribute access — max_skill_growth is allowed, max_skill (with
    # word boundary) is not. config.max_skill_size contains the substring but
    # not the token.
    import re
    hits = re.findall(r"config\.max_skill\b(?!_)", src)
    assert hits == [], f"evolve_skill.py still reads config.max_skill: {hits}"


def test_judge_metrics_receive_max_skill_size():
    """Smoke: the judge branch can build metrics without AttributeError."""
    cfg = EvolutionConfig()
    from evolution.core.fitness import make_judge_metrics

    class FakeJudge:
        def score(self, **kw):
            from evolution.core.fitness import FitnessScore
            return FitnessScore(correctness=1.0, procedure_following=1.0, conciseness=1.0, feedback="ok")

    # This is exactly what evolve_skill.py does — max_size=config.max_skill_size
    score_metric, feedback_metric = make_judge_metrics(FakeJudge(), "skill body", max_size=cfg.max_skill_size)
    assert callable(score_metric)
    assert callable(feedback_metric)
