"""Independent verifier gate — post-GEPA, veto-only, different-family.

Design: plans/2026-11-03-independent-verifier.md + plans/verifier-discussion-claude.md
Principle: verifier sees evidence about behaviour, not the optimizer's story.
Isolation is enforced by the function signature — no diff/feedback/score param.

Pipeline order (cheapest first): no-op → ritual detector → smoke → paid verifier → deploy
The no-op check lives in evolve_skill.py; ritual + verifier live here.

Costs: gate scales with holdout H (~10-20) → 20-40 calls, not with GEPA budget.
Zero extra executor calls — holdout outputs are retained.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Ritual detector — deterministic, free, no model
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _ngrams(text: str, n: int) -> set[str]:
    words = _WORD_RE.findall(text.lower())
    if len(words) < n:
        return set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def detect_ritual_phrases(
    rubrics: list[str],
    baseline_outputs: list[str],
    candidate_outputs: list[str],
    n_values: tuple[int, ...] = (4, 5, 6),
    candidate_threshold: float = 0.5,
    baseline_threshold: float = 0.2,
) -> list[str]:
    """Return n-grams that look like rubric-parroting rituals.

    A phrase is flagged when:
      - it appears in >= candidate_threshold of candidate outputs
      - it appears in <= baseline_threshold of baseline outputs
      - it appears verbatim in the rubric text (joined rubrics)

    This is the ncal failure: a fixed opening sentence that parrots the rubric
    appears in every candidate output but never in baseline outputs.
    """
    if not candidate_outputs or not rubrics:
        return []
    rubric_text = " ".join(rubrics).lower()
    rubric_words = set(_WORD_RE.findall(rubric_text))
    # Quick pre-check: if rubric has too few words, no n-gram can be meaningful
    if len(rubric_words) < 4:
        return []

    hits: list[str] = []
    n_cand = len(candidate_outputs)
    n_base = len(baseline_outputs) if baseline_outputs else 1

    for n in n_values:
        # Collect candidate n-gram frequencies
        cand_counts: dict[str, int] = {}
        for out in candidate_outputs:
            for ng in _ngrams(out, n):
                cand_counts[ng] = cand_counts.get(ng, 0) + 1
        base_sets = [_ngrams(o, n) for o in baseline_outputs]

        for phrase, cnt in cand_counts.items():
            if cnt / n_cand < candidate_threshold:
                continue
            # How many baseline outputs contain this phrase?
            base_hit = sum(1 for s in base_sets if phrase in s)
            if base_hit / max(1, n_base) > baseline_threshold:
                continue
            # Must appear verbatim in rubric text
            if phrase not in rubric_text:
                continue
            # Also require that most words of the phrase come from the rubric
            # (avoid flagging generic phrases like "the following steps are")
            phrase_words = set(phrase.split())
            if len(phrase_words & rubric_words) / max(1, len(phrase_words)) < 0.8:
                continue
            hits.append(phrase)

    # Deduplicate while preserving order; prefer shorter n (already iterated 4→6)
    seen: set[str] = set()
    deduped: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            deduped.append(h)
    return deduped


# ---------------------------------------------------------------------------
# Verdict + verifier
# ---------------------------------------------------------------------------

@dataclass
class VerifierVerdict:
    status: str  # PASS | FAIL | INCONCLUSIVE | UNAVAILABLE
    wins: int = 0
    losses: int = 0
    ties: int = 0
    position_inconsistent: int = 0
    ritual_hits: list[str] = field(default_factory=list)
    skill_flags: list[str] = field(default_factory=list)  # V2 advisory (future)
    reason: str = ""


# Cache for verifier calls: key = sha1(task|outputA|outputB|verifier_model|prompt_version)
_VERIFIER_CACHE: dict[str, str] = {}
PROMPT_VERSION = "v1"


def _cache_key(task: str, output_a: str, output_b: str, verifier_model: str) -> str:
    raw = "\x00".join([task, output_a, output_b, verifier_model, PROMPT_VERSION])
    return hashlib.sha1(raw.encode()).hexdigest()


# The V1 prompt — must NOT contain skill text, diff, feedback, or scores.
V1_PROMPT_TEMPLATE = """Given the task and the rubric of a good response, which response is better?

Task: {task}
Rubric (what a good response should do): {rubric}

Response A:
{output_a}

Response B:
{output_b}

Which response better satisfies the rubric? Consider correctness, procedure following, and conciseness.
Sentences shared verbatim between A and B carry no weight — judge what differs.
Answer with exactly one token: A, B, or TIE.
"""


def _parse_verdict_token(raw: str) -> str | None:
    t = raw.strip().upper()
    # Accept "A", "B", "TIE" even if model adds punctuation
    if t.startswith("A") and "TIE" not in t:
        # Distinguish "A" from "A:" or "A - ..."
        first = re.split(r"\W", t, maxsplit=1)[0]
        if first == "A":
            return "A"
    if t.startswith("B"):
        first = re.split(r"\W", t, maxsplit=1)[0]
        if first == "B":
            return "B"
    if "TIE" in t:
        return "TIE"
    return None


def verify_candidate(
    tasks: list[str],
    rubrics: list[str],
    baseline_outputs: list[str],
    candidate_outputs: list[str],
    candidate_skill_text: str = "",
    verifier_model: str = "",
    *,
    seed: int = 0,
    min_win_rate: float = 0.6,
    max_catastrophic: int = 0,  # reserved for future catastrophic check
    ritual_candidate_threshold: float = 0.5,
    ritual_baseline_threshold: float = 0.2,
    _lm_factory=None,  # injectable for tests: () -> LM with .generate(prompt)->str
) -> VerifierVerdict:
    """Post-GEPA verifier — blocking V1 (outcome pairwise) + ritual detector.

    Isolation: signature has no diff/feedback/score/baseline_text/reasoning param.
    V1 never sees skill text; V2 advisory (candidate_skill_text) is not blocking.

    Returns a VerifierVerdict with status PASS/FAIL/INCONCLUSIVE/UNAVAILABLE.
    UNAVAILABLE (LM error) never silently PASSes.

    Cost: 2*H verifier calls (both orderings), early-stopped when veto is determined.
    Zero extra executor calls — holdout outputs are passed in.
    """
    # 1. Ritual detector first (free, deterministic)
    ritual_hits = detect_ritual_phrases(
        rubrics=rubrics,
        baseline_outputs=baseline_outputs,
        candidate_outputs=candidate_outputs,
        candidate_threshold=ritual_candidate_threshold,
        baseline_threshold=ritual_baseline_threshold,
    )
    if ritual_hits:
        return VerifierVerdict(
            status="FAIL",
            ritual_hits=ritual_hits,
            reason=f"ritual detector flagged {len(ritual_hits)} phrase(s): {ritual_hits[:3]}",
        )

    if not tasks or not verifier_model:
        return VerifierVerdict(status="UNAVAILABLE", reason="no tasks or no verifier_model")

    n = min(len(tasks), len(rubrics), len(baseline_outputs), len(candidate_outputs))
    if n == 0:
        return VerifierVerdict(status="UNAVAILABLE", reason="empty holdout")

    # Build LM — prefer injected factory (tests), otherwise dspy.LM
    def _default_lm():
        try:
            import dspy
            return dspy.LM(verifier_model)
        except Exception as e:
            raise RuntimeError(f"cannot create verifier LM {verifier_model!r}: {e}") from e

    lm_factory = _lm_factory or _default_lm

    wins = losses = ties = inconsistent = 0

    for i in range(n):
        task = tasks[i]
        rubric = rubrics[i]
        base_out = baseline_outputs[i]
        cand_out = candidate_outputs[i]

        # Two orderings: (A=baseline, B=candidate) and swapped
        # A win for candidate in ordering1 means B wins; in ordering2 means A wins.
        # A verdict counts only if both orderings agree (otherwise tie — absorbs position bias).
        try:
            # Ordering 1: A=baseline, B=candidate
            key1 = _cache_key(task, base_out, cand_out, verifier_model)
            if key1 in _VERIFIER_CACHE:
                tok1 = _VERIFIER_CACHE[key1]
            else:
                lm = lm_factory()
                prompt1 = V1_PROMPT_TEMPLATE.format(task=task, rubric=rubric, output_a=base_out, output_b=cand_out)
                # dspy.LM is callable; fallback to .generate
                raw1 = lm(prompt1) if callable(lm) else lm.generate(prompt1)
                if isinstance(raw1, list):
                    raw1 = raw1[0] if raw1 else ""
                tok1 = _parse_verdict_token(str(raw1)) or "TIE"
                _VERIFIER_CACHE[key1] = tok1

            # Ordering 2: A=candidate, B=baseline (swapped)
            key2 = _cache_key(task, cand_out, base_out, verifier_model)
            if key2 in _VERIFIER_CACHE:
                tok2 = _VERIFIER_CACHE[key2]
            else:
                lm = lm_factory()
                prompt2 = V1_PROMPT_TEMPLATE.format(task=task, rubric=rubric, output_a=cand_out, output_b=base_out)
                raw2 = lm(prompt2) if callable(lm) else lm.generate(prompt2)
                if isinstance(raw2, list):
                    raw2 = raw2[0] if raw2 else ""
                tok2 = _parse_verdict_token(str(raw2)) or "TIE"
                _VERIFIER_CACHE[key2] = tok2

        except Exception as e:
            return VerifierVerdict(
                status="UNAVAILABLE",
                wins=wins, losses=losses, ties=ties,
                position_inconsistent=inconsistent,
                ritual_hits=ritual_hits,
                reason=f"verifier LM error on task {i}: {e}",
            )

        # Interpret: tok1 says who wins in ordering1, tok2 in ordering2
        # Candidate wins ordering1 when tok1==B; wins ordering2 when tok2==A
        cand_wins_1 = (tok1 == "B")
        cand_wins_2 = (tok2 == "A")
        cand_loses_1 = (tok1 == "A")
        cand_loses_2 = (tok2 == "B")

        if cand_wins_1 and cand_wins_2:
            wins += 1
        elif cand_loses_1 and cand_loses_2:
            losses += 1
        elif tok1 == "TIE" and tok2 == "TIE":
            ties += 1
        else:
            # Position-inconsistent → count as tie (absorbs bias)
            inconsistent += 1
            ties += 1

        # Early stop: veto already determined (more than half decisive losses)
        decisive = wins + losses
        if decisive > 0 and losses >= (n // 2 + 1):
            # Already a majority loss — can stop early, but continue to
            # accumulate ritual_hits accounting; for veto semantics we can break
            # (kept simple: break after majority loss with at least 3 decisive)
            if decisive >= 3:
                break

    total_decisive = wins + losses

    # Verdict rules (veto, default-reject)
    if total_decisive == 0:
        return VerifierVerdict(
            status="INCONCLUSIVE", wins=wins, losses=losses, ties=ties,
            position_inconsistent=inconsistent, ritual_hits=ritual_hits,
            reason="all ties / position-inconsistent — no decisive signal",
        )

    win_rate = wins / total_decisive if total_decisive else 0.0
    if win_rate < min_win_rate:
        return VerifierVerdict(
            status="FAIL", wins=wins, losses=losses, ties=ties,
            position_inconsistent=inconsistent, ritual_hits=ritual_hits,
            reason=f"win rate {win_rate:.2f} < threshold {min_win_rate:.2f} ({wins}W/{losses}L/{ties}T)",
        )

    return VerifierVerdict(
        status="PASS", wins=wins, losses=losses, ties=ties,
        position_inconsistent=inconsistent, ritual_hits=ritual_hits,
        reason=f"PASS win rate {win_rate:.2f} ({wins}W/{losses}L/{ties}T)",
    )


def clear_verifier_cache() -> None:
    _VERIFIER_CACHE.clear()
