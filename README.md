# 🧬 skill-evolution — Layer 2 for Pi

Offline evolutionary optimization of **Pi agent skills** (`SKILL.md` bodies)
using **DSPy + GEPA** — a port of the Hermes-agent-self-evolution architecture
to the Pi ecosystem, with guardrails scaled to Pi's deployment conventions.
Shipped as an AiX-BIO **tool-skill**: this directory is simultaneously the
skill (`~/.agents/skills/skill-evolution` symlink → this tree), the Python
package, and the git repo.

## Contents

| Path | Role |
| --- | --- |
| `SKILL.md` | Agent-facing skill contract: procedures, gates, signature library |
| `evolution/` | The Python package (config, constraints, dataset builders, fitness, CLI) |
| `bin/evolve` | CLI wrapper — the one command to remember |
| `bin/skill-update-check.py` | Self-update check (git-SHA diff vs origin; no GitHub API) |
| `datasets/skills/<name>/` | Golden JSONL eval sets (curated; highest signal) |
| `tests/` | Offline pytest suite — the skill's selftest |

## 🚀 Installation

```bash
uv venv && uv pip install -e ".[evolution,dev]"
```

## 💡 Usage

```bash
# validate setup without any API cost — ALWAYS first
bin/evolve --skill red-pen --eval-source sessions --dry-run

# generate eval data from your own pi usage history and evolve
bin/evolve --skill my-skill --eval-source sessions --iterations 10

# curated golden dataset
bin/evolve --skill my-skill --eval-source golden \
    --dataset-path datasets/skills/my-skill/golden.jsonl
```

Models via env: `PI_EVOLUTION_OPTIMIZER` (default `openai/gpt-4.1`),
`PI_EVOLUTION_EVAL` (default `openai/gpt-4.1-mini`) — any DSPy LM string works
(e.g. `ollama_chat/gemma4:31b-cloud`). Runs cost ~$2–10 on OpenAI models or
~0 on ollama.

## What it optimizes

Tier 1 (this repo): SKILL.md bodies. Tier 2/3/4 (tool descriptions, prompt
sections, code) are future phases, mirroring the upstream Hermes roadmap.

## How it differs from the Hermes upstream

| Aspect | Hermes upstream | This port |
| --- | --- | --- |
| Skill roots | `<hermes-repo>/skills` | Pi roots: `~/.agents/skills` (native, incl. symlink deployments), `~/.pi` skills dirs, settings custom dirs |
| Eval data | SessionDB, Claude Code, Copilot importers | Pi session JSONL miner (heuristic relevance, free) + synthetic + golden |
| Structural gate | — | skill-forge `bin/skill-lint.py` (auto-detected) |
| Deployment | PR against hermes-agent | git branch `evolve/<skill>-<ts>` in the skill's **source tree** (symlink-followed); never pushed; human merges. Non-git targets → `output/` copy + diff instructions |

## Guardrails (all enforced)

1. Size cap — SKILL.md ≤ 15KB (`PI_EVOLUTION_MAX_SKILL`)
2. Growth cap — ≤ 1.2x baseline bytes (`PI_EVOLUTION_MAX_GROWTH`)
3. Frontmatter contract — name + description preserved
4. skill-forge lint — `python3 skill-lint.py <skill-dir>` exit 0 (if detected)
5. Optional pytest gate (`--run-tests`)
6. Human review — deployment is a local branch or an `output/` file; nothing pushed

Anti-bloat: fitness applies a length penalty ramping 0→0.3 past 90% of budget.

## Design notes

- The GEPA metric is a fast keyword-overlap proxy (matches upstream's shipped
  trade-off): affordable during search; holdout deltas are reported separately
  and the LLM rubric judge (`evolution.core.fitness.LLMJudge`) is available for
  final candidate scoring.
- Deployment edits the skill through its deployment symlink, so git branches
  live in the source tree this repo already tracks — local evolution commits
  show as drift vs origin until merged and pushed (correct signal, not a bug).
- Sessions miner parses Pi session JSONL (type=session/message events), skips
  system-prefixed user messages, redacts common secret shapes. **A mined count
  of 0 for a used skill is a bug, not a result** — trust the parser tests.
- dspy 3.4.x compat: GEPA takes call budgets (`max_metric_calls`), 5-arg
  metrics; MIPROv2 fallback needs `dspy[optuna]`.

## 🔄 Update check

```bash
bin/skill-update-check.py
# UP-TO-DATE / LOCAL-AHEAD (0) · BEHIND-BY-N (1) · OFFLINE / NO-ORIGIN (2)
```

## Repo layout

Single tree at
`claude_workspace/bioinformatics/AIx-BIO/skills/skill-evolution/` (skill =
package = repo), exposed to Pi via the `~/.agents/skills` symlink deployment
(one-way sync per the project's git-tree convention).

## 🔒 Hard guarantees

- Nothing pushed; deployment = local `evolve/` branch or `output/` copy.
- Constraint gates run before anything is written.
- Human review mandatory.

## Tests

```bash
source .venv/bin/activate && python -m pytest tests/ -q
```

## Provenance

Forged 2026-10-23 from the pre-existing `pi-skill-evolution` repo (relocated
into the skills tree, history intact) via skill-forge v1.8.0 tool-skill
profile; preflight verdict GO (`preflight.md`, untracked run artifact).