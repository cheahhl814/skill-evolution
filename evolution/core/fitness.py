"""Fitness: LLM-as-judge rubric scoring + fast heuristic proxy + length penalty.

Faithful to the Hermes self-evolution design: composite =
0.5*correctness + 0.3*procedure + 0.2*conciseness − length_penalty,
where length_penalty ramps 0→0.3 as size crosses 90% of budget
(anti-bloat evolutionary drift).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FitnessScore:
    correctness: float = 0.0
    procedure_following: float = 0.0
    conciseness: float = 0.0
    length_penalty: float = 0.0
    feedback: str = ""

    @property
    def composite(self) -> float:
        raw = 0.5 * self.correctness + 0.3 * self.procedure_following + 0.2 * self.conciseness
        return max(0.0, raw - self.length_penalty)


def length_penalty(artifact_size: int, max_size: int) -> float:
    ratio = artifact_size / max(1, max_size)
    if ratio > 0.9:
        return min(0.3, (ratio - 0.9) * 3.0)
    return 0.0


def _parse_score(value) -> float:
    try:
        return min(1.0, max(0.0, float(str(value).strip())))
    except (TypeError, ValueError):
        return 0.5


class LLMJudge:
    """Rubric-based judge. scores + actionable feedback for GEPA reflection."""

    def __init__(self, config, eval_model: Optional[str] = None):
        import dspy

        class _Judge(dspy.Signature):
            """Evaluate an agent response against an expected-behavior rubric.

            Score three dimensions 0.0-1.0 each: correctness (did it address
            the task), procedure_following (did it follow the skill's
            procedure), conciseness (appropriately concise, nothing important
            omitted). Provide specific actionable improvement feedback."""

            task_input: str = dspy.InputField()
            expected_behavior: str = dspy.InputField(desc="Rubric of a good response")
            agent_output: str = dspy.InputField()
            skill_text: str = dspy.InputField(desc="Instructions the agent was following")
            correctness: float = dspy.OutputField()
            procedure_following: float = dspy.OutputField()
            conciseness: float = dspy.OutputField()
            feedback: str = dspy.OutputField()

        self.config = config
        self.eval_model = eval_model or config.eval_model
        self._judge = dspy.ChainOfThought(_Judge)

    def score(
        self,
        task_input: str,
        expected_behavior: str,
        agent_output: str,
        skill_text: str,
        artifact_size: Optional[int] = None,
        max_size: Optional[int] = None,
    ) -> FitnessScore:
        import dspy

        with dspy.context(lm=dspy.LM(self.eval_model)):
            r = self._judge(
                task_input=task_input,
                expected_behavior=expected_behavior,
                agent_output=agent_output,
                skill_text=skill_text[:4000],
            )
        return FitnessScore(
            correctness=_parse_score(r.correctness),
            procedure_following=_parse_score(r.procedure_following),
            conciseness=_parse_score(r.conciseness),
            length_penalty=length_penalty(artifact_size or 0, max_size or 10**9),
            feedback=str(r.feedback),
        )


def skill_fitness_metric(example, prediction, trace=None, pred_name=None, pred_trace=None,
                         max_size: int = 15360) -> float:
    """DSPy metric for GEPA. Fast heuristic proxy (keyword overlap) so the
    optimization loop stays affordable; rubric quality is verified on the
    holdout with LLMJudge before any deployment. NOTE: this mirrors the
    Hermes implementation's shipped trade-off.

    5-arg signature (gold, pred, trace, pred_name, pred_trace) is required by
    dspy >=3.4 GEPA's bind(None, None, None, None, None) validation; MIPROv2
    still calls it positionally as (gold, pred, trace) and needs a plain float.
    """
    agent_output = (getattr(prediction, "output", "") or "").strip()
    expected = (getattr(example, "expected_behavior", "") or "").lower()
    if not agent_output:
        return 0.0
    expected_words = set(expected.split())
    overlap = len(set(agent_output.lower().split()) & expected_words) / max(1, len(expected_words))
    score = 0.3 + 0.7 * overlap
    return min(1.0, score)


def gepa_feedback_metric(example, prediction, trace=None, pred_name=None, pred_trace=None) -> dict:
    """GEPA feedback metric: score + textual feedback dict per the dspy >=3.4
    GEPAFeedbackMetric contract. Returns {'score': float, 'feedback': str} —
    feedback names the rubric elements the response missed so the reflection
    LM can rewrite the skill instructions toward them."""
    agent_output = (getattr(prediction, "output", "") or "").strip()
    expected = (getattr(example, "expected_behavior", "") or "").lower()
    if not agent_output:
        return {"score": 0.0,
                "feedback": "The agent produced no output. The skill instructions left "
                            "no actionable path to follow — make the next step explicit."}
    covered = set(agent_output.lower().split()) & set(expected.split())
    missed = set(expected.split()) - covered
    score = skill_fitness_metric(example, prediction)
    feedback = (
        f"Heuristic coverage {score:.2f}. The response hit rubric elements like "
        f"{', '.join(sorted(covered)[:8]) or '(none)'}. Missing rubric elements the "
        f"output never mentioned: {', '.join(sorted(missed)[:12])}. Rewrite the skill "
        f"instructions so following them naturally produces an answer addressing the "
        f"missing elements; keep instructions concrete, imperative, one rule per lesson."
    )
    return {"score": score, "feedback": feedback}