"""Wrap a SKILL.md as a DSPy module: the skill body is the optimizable parameter."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def load_skill(skill_path: Path) -> dict:
    raw = skill_path.read_text()
    frontmatter, body = "", raw
    if raw.strip().startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1].strip()
            body = parts[2].strip()
    name = description = ""
    for line in frontmatter.split("\n"):
        s = line.strip()
        if s.startswith("name:") and not name:
            name = s.split(":", 1)[1].strip().strip("'\"")
        elif s.startswith("description:") and not description:
            description = s.split(":", 1)[1].strip().strip("'\"")
    return {
        "path": skill_path,
        "raw": raw,
        "frontmatter": frontmatter,
        "body": body,
        "name": name,
        "description": description,
    }


def reassemble_skill(frontmatter: str, evolved_body: str) -> str:
    return f"---\n{frontmatter}\n---\n\n{evolved_body.strip()}\n"


def make_skill_module(skill_text: str):
    import dspy

    class _TaskWithSkill(dspy.Signature):
        """Complete a task following the provided skill instructions.

        You are an AI agent following specific skill instructions to complete
        a task. Read the skill instructions carefully and follow the
        procedure described."""

        skill_instructions: str = dspy.InputField(desc="The skill instructions to follow")
        task_input: str = dspy.InputField(desc="The task to complete")
        output: str = dspy.OutputField(desc="Your response following the skill instructions")

    class SkillModule(dspy.Module):
        def __init__(self, skill_text: str):
            super().__init__()
            self.skill_text = skill_text
            self.predictor = dspy.ChainOfThought(_TaskWithSkill)

        def forward(self, task_input: str) -> dspy.Prediction:
            result = self.predictor(skill_instructions=self.skill_text, task_input=task_input)
            return dspy.Prediction(output=result.output)

    return SkillModule(skill_text)