"""Eval datasets for skill evolution (synthetic, golden) + pi sessions importer."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from evolution.core.pi_sessions import mine_pi_sessions

SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{16,})"),
    re.compile(r"(ghp_[A-Za-z0-9]{36,})"),
    re.compile(r"(AKIA[0-9A-Z]{16})"),
    re.compile(r"(Bearer\s+[A-Za-z0-9._\-]{20,})"),
]


def scrub_secrets(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("<REDACTED>", text)
    return text


@dataclass
class EvalExample:
    task_input: str
    expected_behavior: str  # rubric, not exact text
    source: str = "synthetic"

    def to_dict(self) -> dict:
        return {
            "task_input": self.task_input,
            "expected_behavior": self.expected_behavior,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvalExample":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class EvalDataset:
    train: list[EvalExample] = field(default_factory=list)
    val: list[EvalExample] = field(default_factory=list)
    holdout: list[EvalExample] = field(default_factory=list)

    @property
    def all_examples(self) -> list[EvalExample]:
        return self.train + self.val + self.holdout

    def _split(self, examples: list[EvalExample]) -> "EvalDataset":
        n = len(examples)
        n_train = max(1, int(n * 0.6))
        n_val = max(1, int(n * 0.2))
        return EvalDataset(
            train=examples[:n_train],
            val=examples[n_train : n_train + n_val],
            holdout=examples[n_train + n_val :] or examples[-1:],
        )

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "dataset.json").write_text(
            json.dumps({s: [e.to_dict() for e in getattr(self, s)] for s in ("train", "val", "holdout")}, indent=2)
        )

    @classmethod
    def load(cls, path: Path) -> "EvalDataset":
        data = json.loads((path / "dataset.json").read_text())
        return cls(
            train=[EvalExample.from_dict(d) for d in data.get("train", [])],
            val=[EvalExample.from_dict(d) for d in data.get("val", [])],
            holdout=[EvalExample.from_dict(d) for d in data.get("holdout", [])],
        )

    def to_dspy_examples(self, split: str) -> list:
        import dspy

        return [
            dspy.Example(task_input=e.task_input, expected_behavior=e.expected_behavior).with_inputs(
                "task_input"
            )
            for e in getattr(self, split)
        ]


class SyntheticDatasetBuilder:
    """Generates (task_input, expected_behavior) rubric pairs from skill text
    using a strong model. Rubric-style: no exact-output matching."""

    class GenerateTestCases:
        pass  # signature defined lazily in __init__ (dspy import cost)

    def __init__(self, config):
        import dspy

        class _Gen(dspy.Signature):
            """Generate realistic test cases for an agent skill.

            For each case: a task a user might give, and a rubric describing
            what a GOOD agent response looks like when following the skill
            (outcomes to hit — never exact strings)."""

            skill_text: str = dspy.InputField(desc="The full skill the agent would follow")
            skill_name: str = dspy.InputField(desc="Name of the skill")
            task_input: str = dspy.OutputField(desc="A realistic task for this skill")
            expected_behavior: str = dspy.OutputField(desc="Rubric of desired outcomes, not exact text")

        self._Gen = _Gen
        self.config = config

    def generate(
        self,
        artifact_text: str,
        artifact_type: str = "skill",
        skill_name: str = "skill",
        n: int = 15,
    ) -> EvalDataset:
        import dspy

        gen = dspy.ChainOfThought(self._Gen)
        results: list[EvalExample] = []
        with dspy.context(lm=dspy.LM(self.config.eval_model)):
            for _ in range(n):
                try:
                    pred = gen(skill_text=artifact_text, skill_name=skill_name)
                    results.append(
                        EvalExample(
                            task_input=pred.task_input.strip(),
                            expected_behavior=pred.expected_behavior.strip(),
                            source="synthetic",
                        )
                    )
                except Exception:
                    continue
        if not results:
            raise RuntimeError("synthetic generation produced no examples; check PI_EVOLUTION_EVAL model access")
        return _default_split(results)


def _default_split(examples: list[EvalExample]) -> EvalDataset:
    n = len(examples)
    n_train = max(1, int(n * 0.6))
    n_val = max(1, int(n * 0.2))
    return EvalDataset(
        train=examples[:n_train],
        val=examples[n_train : n_train + n_val],
        holdout=examples[n_train + n_val :] or examples[-1:],
    )


SyntheticDatasetBuilder.generate.__defaults__ = (None, None, "skill", 15)  # type: ignore[assignment]


def split_examples(examples: list[EvalExample]) -> EvalDataset:
    return _default_split(examples)


class GoldenDatasetLoader:
    """Loads golden JSONL: {"task_input": ..., "expected_behavior": ...} per line."""

    @staticmethod
    def load(path: Path) -> EvalDataset:
        examples = []
        if path.is_dir():
            path = path / "golden.jsonl"
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            examples.append(
                EvalExample(
                    task_input=scrub_secrets(d["task_input"]),
                    expected_behavior=d["expected_behavior"],
                    source="golden",
                )
            )
        return _default_split(examples)


def build_dataset_from_pi_sessions(
    skill_name: str,
    skill_text: str,
    judge: bool = True,
    config=None,
    limit: int = 200,
) -> EvalDataset:
    """Mine ~.pi sessions for (task, outcome) pairs relevant to the skill.

    Heuristic relevance first (no API cost); optional LLM-as-judge scoring
    turns high-signal pairs into rubric examples."""
    pairs = mine_pi_sessions(skill_name, limit=limit)
    if not pairs:
        return EvalDataset()
    examples = []
    for pair in pairs:
        user_text = scrub_secrets(pair.user_text)
        if not judge:
            # Heuristic rubric: the skill name/topic plus "session succeeded" proxy
            examples.append(
                EvalExample(
                    task_input=user_text,
                    expected_behavior=(
                        f"A response that addresses the request using the '{skill_name}' procedure, "
                        "reports what was done with evidence, and does not fabricate results."
                    ),
                    source="pi-sessions",
                )
            )
        else:
            import dspy

            class _Score(dspy.Signature):
                """Score whether a user message is a natural task for the given skill,
                and state the rubric of a good response."""

                user_message: str = dspy.InputField()
                skill_name: str = dspy.InputField()
                skill_summary: str = dspy.InputField()
                relevant: bool = dspy.OutputField(desc="True if this message is a natural task for this skill")
                rubric: str = dspy.OutputField(desc="Rubric of a good response to this task under the skill")

            judge_mod = dspy.ChainOfThought(_Score)
            with dspy.context(lm=dspy.LM(config.eval_model)):
                pred = judge_mod(
                    user_message=user_text[:2000],
                    skill_name=skill_name,
                    skill_summary=skill_text[:1500],
                )
            if pred.relevant and pred.rubric:
                examples.append(
                    EvalExample(task_input=user_text, expected_behavior=pred.rubric.strip(), source="pi-sessions")
                )
    return _default_split(examples)