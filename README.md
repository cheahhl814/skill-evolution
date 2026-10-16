# 🧬 pi-skill-evolution — Layer 2 for Pi

Offline evolutionary optimization of **Pi agent skills** (`~/.agents/skills/.../SKILL.md`)
using **DSPy + GEPA** — a port of the Hermes-agent-self-evolution architecture to the
Pi ecosystem, with guardrails scaled to Pi's deployment conventions.

## What it optimizes

Tier 1 (this repo): SKILL.md bodies. Tier 2/3/4 (tool descriptions, prompt sections,
code) are future phases, mirroring the upstream roadmap.

## How it differs from the Hermes upstream

| Aspect          | Hermes upstream                           | This port                                                                                                                                                            |
| --------------- | ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Skill roots     | `<hermes-repo>/skills`                    | Pi roots: `~/.agents/skills` (native, incl. symlink deployments), `~/.pi/agent/skills`, settings custom dirs                                                         |
| Eval data       | SessionDB, Claude Code, Copilot importers | `~/.pi/agent/sessions/*/*.jsonl` miner (heuristic relevance, free) + synthetic + golden                                                                              |
| Structural gate | —                                         | skill-forge `bin/skill-lint.py` (auto-detected)                                                                                                                      |
| Deployment      | PR against hermes-agent                   | git branch `evolve/<skill>-<ts>` in the skill's **source tree** (symlink-followed); never pushed; human merges. Non-git targets → `output/` copy + diff instructions |

## Guardrails (all enforced)

1. Size cap — SKILL.md ≤ 15KB (`PI_EVOLUTION_MAX_SKILL`)
2. Growth cap — ≤ 1.2x baseline bytes (`PI_EVOLUTION_MAX_GROWTH`)
3. Frontmatter contract — name + description preserved
4. skill-forge lint — `python3 skill-lint.py <skill-dir>` exit 0 (if detected)
5. Optional pytest gate (`--run-tests`)
6. Human review — deployment is a local branch or an `output/` file; nothing pushed

Anti-bloat: fitness applies a length penalty ramping 0→0.3 past 90% of budget.

## Usage

```bash
uv venv && uv pip install -e ".[evolution,dev]" && source .venv/bin/activate

# validate setup without any API cost
python -m evolution.skills.evolve_skill --skill red-pen --eval-source sessions --dry-run

# generate eval data from your own pi usage history and evolve
python -m evolution.skills.evolve_skill --skill skill-forge --eval-source sessions --iterations 10

# synthetic eval data (LLM-generated, needs model access)
python -m evolution.skills.evolve_skill --skill my-skill --eval-source synthetic

# golden dataset
python -m evolution.skills.evolve_skill --skill my-skill --eval-source golden \
  --dataset-path datasets/skills/my-skill/golden.jsonl
```

Models: `PI_EVOLUTION_OPTIMIZER` (default `openai/gpt-4.1`), `PI_EVOLUTION_EVAL`
(default `openai/gpt-4.1-mini`) — any DSPy LM string works (e.g. `ollama_chat/qwen3:30b`).
Runs cost ~$2–10 (or ~0 on local models).

## Design notes

- The GEPA metric is a fast keyword-overlap proxy (matches upstream's shipped
  trade-off): affordable during search; holdout deltas are reported separately and
  the LLM rubric judge (`evolution.core.fitness.LLMJudge`) is available for
  final candidate scoring.
- Deployment edits the skill through its deployment symlink, so git branches live
  in the source tree your AIx-BIO `skill-update-check.py` already tracks — local
  evolution commits will show as drift vs origin until merged and pushed (correct
  signal, not a bug).
- Sessions miner parses Pi session JSONL (type=session/message events), skips
  system-prefixed user messages, redacts common secret shapes.

## Tests

```bash
python -m pytest tests/ -q
```