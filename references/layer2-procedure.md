# Layer 2 — the full offline evolution procedure

> The long form of the `## Procedure` section in `SKILL.md`. That section
> keeps the seven steps in one line each; this file is the detail behind
> them. Read it before a real run.

## 1. Dry-run always

`--dry-run` validates skill resolution, size/frontmatter constraints,
lint-gate detection, and the mined pair count — no API cost.

The mined count must be plausible (tens for any used skill). If it prints
**0, treat it as a BUG, not a result**: a miner regression shipped as "0 for
every skill" and looked like an empty result.

```bash
bin/evolve --skill <name> --eval-source sessions --dry-run
```

## 2. Ask-user gate before any real run

Present **Evidence + Recommend + Options** and let the user decide:

| Decision | Options |
| --- | --- |
| Model (the cost decision) | `ollama_chat/<model>` (e.g. `gemma4:31b-cloud`, ~2 min per 5 iterations) vs `openai/gpt-4.1` (~$2–10/run) |
| Eval source | `sessions` (free, noisy) · `golden` (curated, strongest) · `synthetic` (LLM-generated) |
| Iterations | default 10; first runs 5 |

Env: `PI_EVOLUTION_OPTIMIZER` / `PI_EVOLUTION_EVAL` (see `evolution/core/config.py`).

## 3. Run

GEPA runs with `max_metric_calls = iterations × 10`, 4 threads. The MIPROv2
fallback path is used when GEPA is unavailable.

## 4. Interpret honestly

The report prints baseline vs evolved holdout scores + bytes.

- `+0.000` / "nothing deployed" is a **correct no-op outcome** — report it as
  such. Do not retry silently hoping for a better number.
- **Distrust any reported improvement when `evolved_bytes == baseline_bytes`**
  — MIPROv2 mutates only its proposal instruction, so a byte-identical body
  makes the delta pure noise.
- A candidate can beat the baseline on the proxy metric and still be worse in
  reality. The proxy is a keyword overlap (`0.3 + 0.7 × overlap`) and it can
  be bought with a format ritual. That is why the smoke gate and the
  LLM-judge rubric path exist.

## 5. Review / merge (human, mandatory)

Deployment lands either on a local `evolve/<skill>-<ts>` branch in THIS repo
(for skills symlinked from here — reads as LOCAL-AHEAD to update-check) or as
`output/<skill>/<ts>/` copies.

**Never push.** Merge, or `git branch -D <branch>` after review.

## 6. After any deploy() run: `git symbolic-ref HEAD`

Deploy leaves the repo ON the evolve branch even when the commit fails, so
Layer-1 patches applied there land in the wrong branch (bitten before).
Switch back to master first.

## 7. Smoke the candidate (`--smoke`)

Lint checks structure; it cannot check that the commands the skill tells the
agent to run actually work.

`bin/smoke-candidate` extracts them, runs the allowlisted ones, and rejects a
candidate whose commands fail. A candidate can beat the baseline and still
ship a broken command — the smoke gate is the only check that catches that.

```bash
bin/evolve --skill <name> --eval-source golden \
    --dataset-path datasets/skills/<name>/golden.jsonl --smoke
```

## The fitness metric is the next bottleneck

The keyword-overlap proxy is affordable during search but gameable. The
LLM-judge rubric path (`evolution.core.fitness.LLMJudge`) scores a candidate
on correctness / procedure-following / conciseness and cannot be bought with
a format ritual — a candidate must actually contain the procedure.

Wiring the judge into the golden-dataset path is the open work; until then,
a positive delta only means something after a judge-scored re-run.
