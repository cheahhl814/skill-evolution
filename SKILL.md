---
name: skill-evolution
description: 'Layer-2 offline optimizer for Pi skill SKILL.md bodies (DSPy+GEPA). Mine session history or golden JSONL into eval data, evolve the skill body under constraint gates (15KB cap, ≤1.2x growth, frontmatter contract, lint gate), and deploy as a local evolve/ branch for human review — never pushes. Use when asked to "optimize/evolve a skill", "improve a SKILL.md offline", "run layer-2", "pi-skill-evolution", or "skill evolution". Always dry-run first; ask-user gate before any model spend; report +0.000 outcomes as honest no-ops.'
version: 1.0.0
updated: "2026-10-23"
triggers:
  - "evolve a skill"
  - "optimize skill"
  - "layer-2 evolution"
  - "skill evolution"
  - "pi-skill-evolution"
requires:
  - "uv (python env for the vendored .venv)"
  - "A model backend: ollama (cloud or local) or an OpenAI-compatible key"
---

# Skill evolution — offline Layer-2 optimizer

## What this skill is

A tool-skill wrapper around the **whole Layer-2 tooling vendored in this
directory**: the `evolution/` package (DSPy+GEPA optimizer over SKILL.md
bodies), `datasets/` (golden JSONLs), `tests/` (offline pytest), and the two
entry points `bin/evolve` (CLI wrapper) and the rendered
`bin/skill-update-check.py`. This directory IS the git repo
(`github.com/cheahhl814/skill-evolution`) and the deployed skill via the
`~/.agents/skills/skill-evolution` symlink — one tree, one source of truth.

Layer 1 (runtime learning — `skill_manage`) handles skill CREATION and PATCHES
during sessions. This skill is Layer 2: OFFLINE optimization of a skill's
instruction body with eval data + an evolutionary optimizer, deploying only via
a local branch you review and merge.

## Invocation

```bash
# From anywhere:
~/claude_workspace/bioinformatics/AIx-BIO/skills/skill-evolution/bin/evolve \
    --skill <skill-name> --eval-source sessions --dry-run     # ALWAYS first

# Real run (after the user gates):
bin/evolve --skill <skill-name> --eval-source sessions --iterations 5
bin/evolve --skill <name> --eval-source golden \
    --dataset-path datasets/skills/<name>/golden.jsonl
```

| Eval source | Cost | Use when |
| --- | --- | --- |
| `sessions` | free | Heuristic keyword mining over Pi session history; fast but noisy — expect modest signal |
| `golden` | free to load | Curated JSONL (`{"task_input","expected_behavior"}` per line) — the strongest signal; curated datasets live under `datasets/skills/<name>/` |
| `synthetic` | LLM-generated | No real usage history available |

## Procedure

1. **Dry-run always.** `--dry-run` validates skill resolution, size/frontmatter
   constraints, lint-gate detection, and the mined pair count — no API cost.
   The mined count must be plausible (tens for any used skill); if it prints
   **0, treat it as a BUG, not a result** (a miner regression shipped as "0 for
   every skill" and looked like an empty result).
2. **Ask-user gate before any real run.** Evidence + Recommend + Options:
   - Model = the cost decision: `ollama_chat/<model>` (e.g.
     `gemma4:31b-cloud`, ~2 min per 5 iterations) vs `openai/gpt-4.1`
     (~$2–10/run). Env: `PI_EVOLUTION_OPTIMIZER` / `PI_EVOLUTION_EVAL`.
   - Eval source + iterations (default 10; first runs 5).
3. **Run.** GEPA runs with `max_metric_calls = iterations × 10`, 4 threads.
4. **Interpret honestly.** The report prints baseline vs evolved holdout
   scores + bytes. `+0.000` / "nothing deployed" is a CORRECT no-op outcome —
   report it as such; do not retry silently hoping for a better number.
   **Distrust any reported improvement when `evolved_bytes == baseline_bytes`**
   — MIPROv2 mutates only its proposal instruction, so a byte-identical body
   makes the delta pure noise.
5. **Review/merge (human, mandatory).** Deployment lands either on a local
   `evolve/<skill>-<ts>` branch in THIS repo (for skills symlinked from here —
   reads as LOCAL-AHEAD to update-check) or as `output/<skill>/<ts>/` copies.
   Never push. Merge or `git branch -D <branch>` after review.
6. **After any deploy() run: `git symbolic-ref HEAD`** — deploy leaves the repo
   ON the evolve branch even when commit fails; Layer-1 patches applied
   there land in the wrong branch (bitten before). Switch back to master first.

## Signature library — known failure signatures

| Signature | Likely cause | Fix |
| --- | --- | --- |
| `mined 0 relevant session pair(s)` for an obviously-used skill | Miner token/parse regression | Run `pytest tests/ -q`; read `evolution/core/pi_sessions.py` — a char-join bug (" ".join over one string) shipped exactly this once. Fixed; trust tests |
| `TypeError: cannot unpack non-iterable SessionPair object` | Dataset builder unpacks old triples post-redesign | Consume `pair.user_text` fields; see `evolution/core/dataset_builder.py`. Fixed 2026-10 (regression-tested) |
| GEPA rejects `max_steps` kwarg | dspy ≥3.x dropped it for call budgets | Use `max_metric_calls` (CLI already does); metric must accept 5 args |
| `ModuleNotFoundError: optuna` on MIPROv2 fallback | GEPA path unavailable → MIPROv2 fallback needs optuna | `uv pip install "dspy[optuna]"` in the `.venv` |
| `update-check` prints `NO-GIT` | Script runs where SKILL.md + `.git` are not siblings | Run inside this repo root (skill source tree), or check the symlink resolves here |
| Baseline `size_limit: exceeded` (e.g. 25KB) | Target skill genuinely over the 15KB cap | Evolve anyway (shrink is the goal) but expect deploy-gate friction; or set `PI_EVOLUTION_MAX_SKILL` to size the target honestly |
| Evolved candidate fails `test_smoke.py` despite lint pass | Skill-forge §0.8 lesson: lint is necessary, not sufficient | Gate the candidate, not the delta — run the target skill's own structural tests on the evolve branch before merge-or-drop |

## Constants / knobs

| Env var | Default | Meaning |
| --- | --- | --- |
| `PI_EVOLUTION_OPTIMIZER` | `openai/gpt-4.1` | GEPA reflection/darwin optimizer model |
| `PI_EVOLUTION_EVAL` | `openai/gpt-4.1-mini` | Eval/task model |
| `PI_EVOLUTION_MAX_SKILL` | 15360 | SKILL.md byte cap |
| `PI_EVOLUTION_MAX_GROWTH` | 1.2 | Accepted byte growth vs baseline |

## Hard guarantees

- Nothing is ever pushed. Deployment = local branch in this repo (human
  review via diff) or an `output/` copy.
- Baseline constraints must hold (or be improved) — an already-over-cap skill
  is flagged, not silently shipped.
- The skill-forge linter (`bin/skill-lint.py`) is auto-detected and enforced as
  the structural gate when present.

## Handoff

If the run produces a candidate, the user reviews it here — this repo's work
tree. Offer: merge to master + push, keep the branch, or drop it. Then update
the target skill's `handoff.md` per the handoff-skill contract.