"""Offline tests for the independent verifier gate (no API calls).

Covers the 11-item test plan from plans/2026-11-03-independent-verifier.md §8:
family table, diversity, veto semantics, position bias, isolation, ritual,
no-op, fail-closed, cache, regression, plus verifier-specific rituals.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evolution.core.config as cfg
from evolution.verifier import (
    V1_PROMPT_TEMPLATE,
    VerifierVerdict,
    clear_verifier_cache,
    detect_ritual_phrases,
    verify_candidate,
)


# ── family() table ──────────────────────────────────────────────────────

def test_family_table():
    assert cfg.family("openai/gpt-4.1") == "openai"
    assert cfg.family("ollama_chat/gpt-oss:120b-cloud") == "openai"
    assert cfg.family("ollama_chat/gemma4:31b-cloud") == "gemma"
    assert cfg.family("anthropic/claude-sonnet-5-5") == "anthropic"
    assert cfg.family("claude-sonnet-5-5") == "anthropic"
    assert cfg.family("ollama_chat/kimi-k2") == "kimi"
    assert cfg.family("ollama_chat/mistral-large") == "mistral"
    assert cfg.family("beam-unknown-model-xyz") is None
    assert cfg.family("") is None
    assert cfg.family(None) is None


def test_family_unknown_refused():
    c = cfg.EvolutionConfig(optimizer_model="openai/gpt-4.1", eval_model="openai/gpt-4.1-mini",
                            judge_model="openai/gpt-4.1-mini", verifier_model="beam-mystery-model")
    try:
        cfg.check_family_diversity(c)
    except SystemExit as e:
        assert "unknown verifier family" in str(e).lower()
        return
    raise AssertionError("unknown family should refuse")


def test_family_diversity_same_family_blocked():
    c = cfg.EvolutionConfig(optimizer_model="openai/gpt-4.1", eval_model="openai/gpt-4.1-mini",
                            judge_model="openai/gpt-4.1-mini", verifier_model="openai/gpt-4o")
    try:
        cfg.check_family_diversity(c)
    except SystemExit as e:
        assert "collides" in str(e).lower()
        return
    raise AssertionError("same family should be blocked")


def test_family_diversity_different_family_passes():
    c = cfg.EvolutionConfig(optimizer_model="openai/gpt-4.1", eval_model="openai/gpt-4.1-mini",
                            judge_model="openai/gpt-4.1-mini", verifier_model="anthropic/claude-sonnet-5-5")
    cfg.check_family_diversity(c)  # should not raise


def test_family_diversity_override_allows_unknown():
    c = cfg.EvolutionConfig(optimizer_model="openai/gpt-4.1", eval_model="openai/gpt-4.1-mini",
                            judge_model="openai/gpt-4.1-mini", verifier_model="beam-mystery")
    cfg.check_family_diversity(c, verifier_family_override="beam")  # should not raise


def test_family_diversity_weak_tier_refused():
    c = cfg.EvolutionConfig(optimizer_model="openai/gpt-4.1", eval_model="openai/gpt-4.1-mini",
                            judge_model="openai/gpt-4.1-mini", verifier_model="anthropic/claude-haiku")
    try:
        cfg.check_family_diversity(c)
    except SystemExit as e:
        assert "weak verifier" in str(e).lower()
        return
    raise AssertionError("weak tier should be refused")


# ── veto semantics ──────────────────────────────────────────────────────

def _always(winner: str):
    """LM factory that always returns winner (A/B/TIE)."""
    def factory():
        def lm(prompt):
            return winner
        return lm
    return factory


def test_veto_always_wins_pass():
    clear_verifier_cache()
    # Candidate always wins: ordering1 B wins, ordering2 A wins → wins
    calls = []
    def factory():
        def lm(prompt):
            calls.append(prompt)
            # Detect ordering by checking which output is A
            if "CAND_BASE" in prompt.split("Response A:")[1].split("Response B:")[0]:
                return "B"  # candidate is B → B wins
            else:
                return "A"  # candidate is A → A wins
        return lm
    v = verify_candidate(
        tasks=["t1", "t2", "t3"], rubrics=["r1", "r2", "r3"],
        baseline_outputs=["CAND_BASE 1", "CAND_BASE 2", "CAND_BASE 3"],
        candidate_outputs=["CAND_EVO 1", "CAND_EVO 2", "CAND_EVO 3"],
        verifier_model="anthropic/claude-sonnet-5-5",
        _lm_factory=factory,
    )
    assert v.status == "PASS"


def test_veto_always_loses_fail():
    clear_verifier_cache()
    def factory():
        def lm(prompt):
            if "CAND_BASE" in prompt.split("Response A:")[1].split("Response B:")[0]:
                return "A"  # baseline wins
            else:
                return "B"  # baseline wins (candidate is A but B wins)
        return lm
    v = verify_candidate(
        tasks=["t1", "t2"], rubrics=["r1", "r2"],
        baseline_outputs=["CAND_BASE 1", "CAND_BASE 2"],
        candidate_outputs=["CAND_EVO 1", "CAND_EVO 2"],
        verifier_model="anthropic/claude-sonnet-5-5",
        _lm_factory=factory,
    )
    assert v.status == "FAIL"


def test_veto_all_ties_inconclusive():
    clear_verifier_cache()
    v = verify_candidate(
        tasks=["t1", "t2"], rubrics=["r1", "r2"],
        baseline_outputs=["b1", "b2"], candidate_outputs=["c1", "c2"],
        verifier_model="anthropic/claude-sonnet-5-5",
        _lm_factory=_always("TIE"),
    )
    assert v.status == "INCONCLUSIVE"


def test_position_bias_absorbed():
    """Stub that always answers A → every pair order-inconsistent → INCONCLUSIVE."""
    clear_verifier_cache()
    v = verify_candidate(
        tasks=["t1", "t2", "t3"], rubrics=["r1", "r2", "r3"],
        baseline_outputs=["b1", "b2", "b3"], candidate_outputs=["c1", "c2", "c3"],
        verifier_model="anthropic/claude-sonnet-5-5",
        _lm_factory=_always("A"),
    )
    assert v.status == "INCONCLUSIVE"
    # All should be position-inconsistent or ties, no decisive wins/losses
    assert v.wins == 0 and v.losses == 0


# ── isolation ───────────────────────────────────────────────────────────

def test_isolation_signature_has_no_leak_params():
    import inspect
    sig = inspect.signature(verify_candidate)
    forbidden = {"diff", "feedback", "score", "baseline_text", "reasoning"}
    for name in sig.parameters:
        assert name.lower() not in forbidden, f"leak param {name} in signature"
        for bad in forbidden:
            assert bad not in name.lower(), f"leak substring {bad} in {name}"


def test_isolation_prompt_has_no_skill_text():
    clear_verifier_cache()
    seen_prompts = []
    def factory():
        def lm(prompt):
            seen_prompts.append(prompt)
            return "TIE"
        return lm
    verify_candidate(
        tasks=["task"], rubrics=["rubric"],
        baseline_outputs=["baseline output"], candidate_outputs=["candidate output"],
        candidate_skill_text="SECRET SKILL TEXT SHOULD NOT APPEAR IN V1 PROMPT",
        verifier_model="anthropic/claude-sonnet-5-5",
        _lm_factory=factory,
    )
    for p in seen_prompts:
        assert "SECRET SKILL TEXT" not in p
        assert "evolved" not in p.lower()
        assert "baseline" not in p.lower() or "Baseline constraints" not in p


def test_v1_prompt_template_isolation():
    # V1 template must not mention skill/diff/feedback/score as words
    # (substring check is too strict — "differs" contains "diff")
    import re
    words = set(re.findall(r"[a-z]+", V1_PROMPT_TEMPLATE.lower()))
    for bad in ("skill", "diff", "feedback", "score", "evolved", "baseline"):
        assert bad not in words, f"V1 template leaks {bad}"


# ── ritual detector ────────────────────────────────────────────────────

def test_ritual_planted_opener_flagged():
    rubric = "the agent must list the command and its output for every step"
    cand = ["list the command and its output is shown below: answer 1"] * 5
    base = ["answer 1", "answer 2", "answer 3", "answer 4", "answer 5"]
    hits = detect_ritual_phrases([rubric] * 5, base, cand)
    assert len(hits) > 0
    assert any("list the command" in h for h in hits)


def test_ritual_equally_frequent_not_flagged():
    rubric = "list the command and its output"
    cand = ["list the command and its output answer 1", "list the command and its output answer 2"]
    base = ["list the command and its output base 1", "list the command and its output base 2"]
    hits = detect_ritual_phrases([rubric] * 2, base, cand)
    assert hits == []


def test_ritual_triggers_fail():
    clear_verifier_cache()
    rubric = "the agent must list the command and its output for every step"
    cand = ["list the command and its output is shown: answer"] * 3
    base = ["answer one", "answer two", "answer three"]
    v = verify_candidate(
        tasks=["t"] * 3, rubrics=[rubric] * 3,
        baseline_outputs=base, candidate_outputs=cand,
        verifier_model="anthropic/claude-sonnet-5-5",
        _lm_factory=_always("TIE"),
    )
    assert v.status == "FAIL"
    assert len(v.ritual_hits) > 0


# ── fail-closed ─────────────────────────────────────────────────────────

def test_fail_closed_unavailable_never_pass():
    clear_verifier_cache()
    def boom_factory():
        def lm(prompt):
            raise RuntimeError("LM down")
        return lm
    v = verify_candidate(
        tasks=["t1"], rubrics=["r1"],
        baseline_outputs=["b1"], candidate_outputs=["c1"],
        verifier_model="anthropic/claude-sonnet-5-5",
        _lm_factory=boom_factory,
    )
    assert v.status == "UNAVAILABLE"
    assert v.status != "PASS"


def test_no_tasks_unavailable():
    v = verify_candidate(tasks=[], rubrics=[], baseline_outputs=[], candidate_outputs=[],
                         verifier_model="anthropic/claude-sonnet-5-5", _lm_factory=_always("A"))
    assert v.status == "UNAVAILABLE"


# ── cache ───────────────────────────────────────────────────────────────

def test_cache_second_call_zero_lm_calls():
    clear_verifier_cache()
    calls = []
    def factory():
        def lm(prompt):
            calls.append(prompt)
            return "TIE"
        return lm
    kwargs = dict(tasks=["t1"], rubrics=["r1"], baseline_outputs=["b1"], candidate_outputs=["c1"],
                  verifier_model="anthropic/claude-sonnet-5-5", _lm_factory=factory)
    verify_candidate(**kwargs)
    n1 = len(calls)
    assert n1 > 0
    verify_candidate(**kwargs)
    assert len(calls) == n1  # cached


# ── regression ──────────────────────────────────────────────────────────

def test_no_config_max_skill_access():
    import re
    src = (Path(__file__).parent.parent / "evolution" / "skills" / "evolve_skill.py").read_text()
    hits = re.findall(r"config\.max_skill\b(?!_)", src)
    assert hits == [], f"still reads config.max_skill: {hits}"
