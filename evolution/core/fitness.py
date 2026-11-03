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
        # Prefer dedicated judge_model when present; eval_model param is
        # kept for backwards compat but judge_model is the intended source.
        if eval_model is None:
            eval_model = getattr(config, "judge_model", None) or getattr(config, "eval_model", None)
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

    GAMING WARNING (observed on the 2026-10-28 ncal run): this proxy is
    gameable — `0.3 + 0.7 * overlap` rewards fixed opening sentences that
    parrot the rubric and deletes of worked examples. It is kept ONLY as the
    `--metric proxy` opt-in for cheap experiments. The default Layer-2 metric
    is `make_judge_metrics(...)` in this module, which scores with the
    LLMJudge rubric instead.

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


def gepa_feedback_metric(example, prediction, trace=None, pred_name=None, pred_trace=None):
    """GEPA feedback metric (dspy >=3.4 contract, 5-arg).

    Program level (no pred_name): a plain float — GEPA aggregates program
    scores arithmetically and crashes on `int + dict` otherwise (observed).
    Predictor level (pred_name given): {'score', 'feedback'} dict — feedback
    names the rubric elements the response missed so the reflection LM can
    rewrite the skill instructions toward them."""
    score = skill_fitness_metric(example, prediction)
    if pred_name is None:
        return score
    agent_output = (getattr(prediction, "output", "") or "").strip()
    expected = (getattr(example, "expected_behavior", "") or "").lower()
    if not agent_output:
        return {"score": 0.0,
                "feedback": "The agent produced no output. The skill instructions left "
                            "no actionable path to follow — make the next step explicit."}
    covered = set(agent_output.lower().split()) & set(expected.split())
    missed = set(expected.split()) - covered
    feedback = (
        f"Heuristic coverage {score:.2f}. The response hit rubric elements like "
        f"{', '.join(sorted(covered)[:8]) or '(none)'}. Missing rubric elements the "
        f"output never mentioned: {', '.join(sorted(missed)[:12])}. Rewrite the skill "
        f"instructions so following them naturally produces an answer addressing the "
        f"missing elements; keep instructions concrete, imperative, one rule per lesson."
    )
    return {"score": score, "feedback": feedback}


def _instructions_from_trace(trace) -> Optional[str]:
    """Best-effort extraction of the ACTIVE candidate's skill instructions from
    a DSPy metric trace. GEPA mutates the predictor instruction (each candidate
    *is* a proposed skill body), so judge must score procedure-following
    against the candidate's own text, not the frozen baseline. Falls back to
    None when no trace carries a signature (callers then use the baseline)."""
    from evolution.skills.skill_module import _SKILL_FOOTER

    try:
        for item in trace or []:
            if not isinstance(item, (tuple, list)) or not item:
                continue
            holder = item[0]
            sig = getattr(holder, "signature", None)
            if sig is None:
                for attr in ("predictor", "predict"):
                    inner = getattr(holder, attr, None)
                    if inner is None:
                        continue
                    if getattr(inner, "instructions", None):
                        sig = inner  # the inner object IS the signature
                        break
                    sig = getattr(inner, "signature", None)
                    if sig is not None:
                        break
            instructions = getattr(sig, "instructions", None)
            if instructions:
                # Strip the module footer so the judged text is the bare
                # candidate body (matches extract_skill_text semantics).
                if instructions.endswith(_SKILL_FOOTER):
                    return instructions[: -len(_SKILL_FOOTER)].strip()
                return instructions.strip()
    except Exception:
        return None
    return None


def make_judge_metrics(judge, skill_text: str, artifact_size: Optional[int] = None,
                       max_size: Optional[int] = None):
    """Build the DEFAULT Layer-2 metrics: LLMJudge rubric scoring instead of the
    gameable keyword-overlap proxy.

    `judge` is anything with `.score(task_input, expected_behavior,
    agent_output, skill_text, artifact_size, max_size) -> FitnessScore` —
    normally an `LLMJudge`, but any offline stub satisfying the protocol works
    (this is what the tests inject to stay free).

    When DSPy supplies a metric trace carrying the predictor's instructions,
    the judge scores against the ACTIVE candidate's instructions (each GEPA
    candidate IS a proposed skill body); without a trace the baseline
    `skill_text` is used.

    Returns (feedback_metric, score_metric):
    - feedback_metric: 5-arg DSPy metric for GEPA — program level returns a
      plain float composite; predictor level returns {'score', 'feedback'}
      with the judge's actionable feedback so the reflection LM rewrites the
      skill toward the missed rubric elements.
    - score_metric: plain-float composite, used by the MIPROv2 fallback and
      for holdout scoring.

    Identical (task_input, agent_output, judged_skill_text) triples are
    memoised — GEPA re-evaluates unchanged candidates and each judge call is
    a paid one.
    """
    import hashlib

    cache: dict = {}
    base_size = len(skill_text.encode()) if artifact_size is None else artifact_size

    def _score(example, prediction, trace=None) -> FitnessScore:
        agent_output = (getattr(prediction, "output", "") or "").strip()
        judged_text = _instructions_from_trace(trace) or skill_text
        size = len(judged_text.encode()) if judged_text is not skill_text else base_size
        key = hashlib.sha1(
            "\x00".join([
                str(getattr(example, "task_input", "")),
                agent_output,
                judged_text,
            ]).encode()
        ).hexdigest()
        if key not in cache:
            if not agent_output:
                cache[key] = FitnessScore(
                    feedback="The agent produced no output. The skill instructions "
                            "left no actionable path to follow — make the next step "
                            "explicit.")
            else:
                cache[key] = judge.score(
                    task_input=str(getattr(example, "task_input", "")),
                    expected_behavior=str(getattr(example, "expected_behavior", "")),
                    agent_output=agent_output,
                    skill_text=judged_text,
                    artifact_size=size,
                    max_size=max_size,
                )
        return cache[key]

    def score_metric(example, prediction, trace=None, pred_name=None, pred_trace=None) -> float:
        """Plain-float composite (MIPROv2 fallback + holdout scoring)."""
        return _score(example, prediction, trace).composite

    def feedback_metric(example, prediction, trace=None, pred_name=None, pred_trace=None):
        """GEPA feedback metric (dspy >=3.4 contract, 5-arg): program level →
        float, predictor level → {'score','feedback'} from the judge."""
        fs = _score(example, prediction, trace)
        if pred_name is None:
            return fs.composite
        return {"score": fs.composite, "feedback": fs.feedback or
                "No judge feedback; keep the skill instructions concrete and "
                "verifiable."}

    return score_metric, feedback_metric