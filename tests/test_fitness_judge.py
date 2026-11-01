"""Offline tests for the LLMJudge-wired fitness metrics (no API calls).

The keyword-overlap proxy was gamed on the 2026-10-28 ncal run (a grounding
ritual + deleted worked examples scored +0.096 while being worse). These
tests pin the judge-metric contract that replaced it as the Layer-2 default.

Judge stub protocol: `.score(task_input, expected_behavior, agent_output,
skill_text, artifact_size, max_size) -> FitnessScore`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution.core.fitness import (  # noqa: E402
    FitnessScore,
    length_penalty,
    make_judge_metrics,
)


class FakeJudge:
    """Deterministic judge stub recording every call. Emulates the real
    LLMJudge contract: the length penalty is applied INSIDE .score."""

    def __init__(self, correctness=1.0, procedure=1.0, conciseness=1.0, feedback="ok"):
        self.calls = []
        self._c, self._p, self._k, self._fb = correctness, procedure, conciseness, feedback

    def score(self, task_input, expected_behavior, agent_output, skill_text,
              artifact_size=None, max_size=None):
        self.calls.append({
            "task_input": task_input,
            "expected_behavior": expected_behavior,
            "agent_output": agent_output,
            "skill_text": skill_text,
            "artifact_size": artifact_size,
            "max_size": max_size,
        })
        return FitnessScore(
            correctness=self._c,
            procedure_following=self._p,
            conciseness=self._k,
            length_penalty=length_penalty(artifact_size or 0, max_size or 10 ** 9),
            feedback=self._fb,
        )


class Ex:
    task_input = "What is today's date?"
    expected_behavior = "Runs date and reports the printed YYYY-MM-DD"


class Pred:
    def __init__(self, output):
        self.output = output


def test_judge_score_metric_uses_composite():
    score_metric, feedback_metric = make_judge_metrics(FakeJudge(), "skill body")
    assert score_metric(Ex(), Pred("runs `date` and reports 2026-03-01")) == 1.0


def test_judge_program_level_returns_plain_float():
    # GEPA aggregates program scores arithmetically; a dict here crashes.
    _, feedback_metric = make_judge_metrics(FakeJudge(), "skill body")
    assert isinstance(feedback_metric(Ex(), Pred("an answer")), float)


def test_judge_predictor_level_returns_score_and_feedback():
    _, feedback_metric = make_judge_metrics(
        FakeJudge(correctness=0.4, feedback="missed the grounding rule"), "skill body")
    out = feedback_metric(Ex(), Pred("an answer"), None, pred_name="predictor")
    assert isinstance(out, dict)
    assert out["score"] < 1.0
    assert "grounding rule" in out["feedback"]


def test_empty_output_scores_zero_without_judge_call():
    judge = FakeJudge()
    score_metric, feedback_metric = make_judge_metrics(judge, "skill body")
    assert score_metric(Ex(), Pred("")) == 0.0
    out = feedback_metric(Ex(), Pred("   "), None, pred_name="predictor")
    assert out["score"] == 0.0
    assert "no output" in out["feedback"].lower()
    assert judge.calls == []


def test_identical_triples_are_memoised():
    judge = FakeJudge()
    score_metric, _ = make_judge_metrics(judge, "skill body")
    for _ in range(3):
        score_metric(Ex(), Pred("same output"))
    assert len(judge.calls) == 1


def test_trace_supplies_candidate_instructions():
    """Each GEPA candidate IS a proposed skill body: with a trace carrying the
    mutated predictor instructions the judge must see CANDIDATE text, not the
    frozen baseline."""
    class Sig:
        instructions = "candidate instructions v2"

    class Inner:
        signature = Sig()

    class Predictor:
        predict = Inner()

    trace = [(Predictor(), {"task_input": "x"}, {"output": "y"})]
    judge = FakeJudge()
    score_metric, _ = make_judge_metrics(judge, "baseline body")
    score_metric(Ex(), Pred("output"), trace)
    assert judge.calls[0]["skill_text"] == "candidate instructions v2"


def test_no_trace_falls_back_to_baseline_text():
    judge = FakeJudge()
    score_metric, _ = make_judge_metrics(judge, "baseline body")
    score_metric(Ex(), Pred("output"))
    assert judge.calls[0]["skill_text"] == "baseline body"


def test_artifact_size_tracked_per_variant():
    """Length penalty must ramp on the CANDIDATE's size, not a stale one."""
    class SizedSig:
        instructions = "c" * 15000  # > 90% of 15360 => penalty ~0.3

    class SizedPredictor:
        predict = SizedSig()

    judge = FakeJudge()
    score_metric, _ = make_judge_metrics(judge, "tiny baseline", max_size=15360)
    s = score_metric(Ex(), Pred("output"), [(SizedPredictor(), {}, {})])
    assert len(judge.calls) == 1
    assert judge.calls[0]["artifact_size"] == 15000
    assert s < 1.0  # composite reduced by the length penalty


def test_judge_failure_propagates():
    """A failing judge call must raise, not fall back to the gameable proxy —
    scoring is the paid gate and a silent fallback silently reintroduces the
    bug it replaced."""
    class BoomJudge:
        def score(self, *a, **kw):
            raise RuntimeError("judge LM down")

    score_metric, _ = make_judge_metrics(BoomJudge(), "skill body")
    try:
        score_metric(Ex(), Pred("output"))
    except RuntimeError:
        return
    raise AssertionError("judge failure was swallowed")