"""Wrap a SKILL.md as a DSPy module: the skill body is the optimizable parameter.

Design (2026-10-17, post first two Layer-2 runs): DSPy optimizers (GEPA,
MIPROv2) only mutate module *instructions* and few-shot demos — InputField
values are frozen inputs. The first two runs therefore shipped body-identical
candidates (+0 bytes) and metric deltas that were pure noise. The fix: bake
the SKILL.md body into the predictor's signature instruction via a fixed
footer, so each optimizer-authored candidate *is* a proposed skill body.
`extract_skill_text()` strips the footer to reconstruct the evolved body for
frontmatter reassembly and constraint gating.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Fixed suffix appended to the skill body inside the signature instruction.
# Extraction must remove it byte-exactly; keep a single literal, do not format.
_SKILL_FOOTER = (
    "\n\nComplete the task by following the skill instructions above, "
    "verbatim where the procedure is prescribed."
)


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
    """Build a module whose single predictor instruction *is* the skill body
    (plus a fixed footer). Every optimizer-mutated instruction is therefore a
    candidate SKILL.md body."""
    import dspy

    instruction = skill_text.strip() + _SKILL_FOOTER

    _TaskWithSkill = type(
        "_TaskWithSkill",
        (dspy.Signature,),
        {
            "__doc__": instruction,
            "task_input": dspy.InputField(desc="The task to complete"),
            "output": dspy.OutputField(
                desc="Your response following the skill instructions"
            ),
        },
    )

    class SkillModule(dspy.Module):
        def __init__(self, skill_text: str = ""):
            super().__init__()
            self.skill_text = skill_text
            self.predictor = dspy.ChainOfThought(_TaskWithSkill)

        def forward(self, task_input: str) -> dspy.Prediction:
            result = self.predictor(task_input=task_input)
            return dspy.Prediction(output=result.output)

    return SkillModule(skill_text=skill_text)


def _signature_of(module):
    """Resolve the Predict signature across dspy builds: a Predictor exposes
    .signature directly, or wraps it at .predict.signature (ChainOfThought)."""
    pred = module.predictor
    sig = getattr(pred, "signature", None)
    if sig is None:
        inner = getattr(pred, "predict", None)
        sig = getattr(inner, "signature", None) if inner is not None else None
    if sig is None:
        raise AttributeError("no signature found on module's predictor chain")
    return sig


def extract_skill_text(module) -> str:
    """Recover the (possibly evolved) SKILL.md body from a module's mutated
    predictor instruction by stripping the fixed footer. If the optimizer
    rewrote away the footer, the whole instruction is returned."""
    instruction = _signature_of(module).instructions
    if instruction.endswith(_SKILL_FOOTER):
        return instruction[: -len(_SKILL_FOOTER)].strip()
    return instruction.strip()