# 🧬 skill-evolution — the two-layer self-improvement skill for Pi

Offline evolutionary optimization of **Pi agent skills** (`SKILL.md` bodies)
using **DSPy + GEPA** — a port of the Hermes-agent-self-evolution architecture
to the Pi ecosystem, with guardrails scaled to Pi's deployment conventions —
plus the **Layer-1 runtime extension** that writes and patches skills during
a session. Shipped as an AiX-BIO **tool-skill**: this directory is
simultaneously the skill (`~/.agents/skills/skill-evolution` symlink → this
tree), the Python package, the git repo, and the home of the Pi extension
(`~/.pi/agent/extensions/skill-memory.ts` symlink → `layer1/extension/`).

## The two layers

| Layer | What | Where | When |
| --- | --- | --- | --- |
| **Layer 1** | `skill_manage` tool: create / patch / write_file / remove_file / delete skills atomically, with guardrails (name regex, escape guard, frontmatter contract, 15KB cap, unique-patch match, snapshot+rollback, symlink-aware delete). Injects the skills-first routing prompt; one learning nudge per session; **records per-skill usage telemetry**; **writes a triage digest at session end**; **suggests the ranked next target in the prompt**. | `layer1/extension/skill-memory.ts` | in-session, live |
| **Monitor** | Reads the telemetry, ranks skills by `usage × potential`, prints the next action per skill (`measure` / `evolve` / `shrink` / `stabilize` / `review`). No model calls. | `evolution/monitor/`, `bin/triage` | offline, on demand |
| **Layer 2** | DSPy + GEPA optimizer over a skill's `SKILL.md` body: eval data from sessions / golden JSONL / synthetic, constraint gates, structural lint gate, human-reviewed local-branch deployment. | `evolution/`, `bin/evolve`, `datasets/`, `tests/` | offline, on demand |

Both layers ship from this one repo, so a runtime fix and an optimizer change
are reviewed and released together.

## Contents

| Path | Role |
| --- | --- |
| `SKILL.md` | Agent-facing skill contract: procedures, gates, signature library |
| `layer1/extension/skill-memory.ts` | **Layer 1** — the Pi extension (source of truth; deployed by symlink) |
| `layer1/agents-routing-section.md` | Copy of the agent's global instruction file's routing section (§7) — keep in sync |
| `layer1/telemetry-schema.md` | The telemetry wire contract (JSONL) shared by the TS writer and the Python reader |
| `layer1/digest-schema.md` | The session-end digest format (what is written, where, and when it is skipped) |
| `references/monitor-and-digest.md` | Full reference: telemetry weights, triage table, digest format, prompt suggestion, all knobs |
| `evolution/` | The Python package (config, constraints, dataset builders, fitness, CLI) |
| `evolution/monitor/` | Telemetry reader + triage ranking (upstream Hermes leaves this a placeholder) |
| `bin/evolve` | CLI wrapper — the optimizer |
| `bin/triage` | CLI wrapper — "which skill should I evolve first?" (read-only, no cost) |
| `bin/skill-update-check.py` | Self-update check (git-SHA diff vs origin; no GitHub API) |
| `datasets/skills/<name>/` | Golden JSONL eval sets (curated; highest signal) |
| `tests/` | Offline pytest suite — the skill's selftest |

## 🚀 Installation

```bash
uv venv && uv pip install -e ".[evolution,dev]"

# Layer 1: deploy the extension as a symlink (never a copy)
ln -sfn ~/claude_workspace/bioinformatics/AIx-BIO/skills/skill-evolution/layer1/extension/skill-memory.ts \
        ~/.pi/agent/extensions/skill-memory.ts
```

## 💡 Usage

```bash
# 0) pick the target — read-only, no model spend
bin/triage
bin/triage --json

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

## The monitor / triage step

`bin/triage` implements the piece Hermes upstream never built: Hermes'
Phase 5 "auto-triage" ranks optimization targets by *(potential improvement ×
usage frequency)*, but `evolution/monitor/` in
`hermes-agent-self-evolution` is still a one-line placeholder ("Phase
placeholder: monitor evolution."). This port fills it in using the signals Pi
actually has:

- **Usage** — recency-decayed per-skill telemetry written by the Layer-1
  extension (`skill_loaded` 1.0, `skill_managed` 0.8, `skill_read` 0.6,
  `skill_failed` 1.5; 14-day half-life).
- **Potential** — structural payoff estimate: used-but-unmeasured 0.90
  (`measure`), used-and-over-cap 0.75 (`shrink`), churn ≥3 0.70
  (`stabilize`), used-and-measured 0.60 (`evolve`), never-used 0.35
  (`review`).
- **Priority** — `usage × potential`; the ranking key.

Triage never runs the optimizer, never merges, and never spends tokens. It is
the input to a *human-gated* `bin/evolve` run, not a replacement for one.

## Session-end digest (the loop closes itself)

The Layer-1 extension runs the monitor on `session_shutdown` and appends a
digest, so every session leaves a record of what to evolve next:

```
~/.local/share/skill-evolution/digests/2026-10-28.md   # one file per UTC day
~/.local/share/skill-evolution/digests/latest.md        # the newest section
```

Each section carries the session's own event counts, the top action, the
delta vs the previous digest, and the ranked table. The monitor is
stdlib-only, so this costs **zero** model calls. `/reload` is skipped (it
must not spam the directory), one section is written per session even though
`session_shutdown` can fire twice, and files older than 30 days are pruned.
Turn it off with `PI_SKILL_TRIAGE_ON_SHUTDOWN=0`.

## The extension suggests, the agent acts

The same ranking reaches the agent as a **prompt suggestion**: once per
session the extension asks the monitor for the single highest-priority
actionable target (`evolution.monitor.triage --suggest`) and injects it into
the `skill_memory` prompt section.

```
Skill self-improvement: the triage digest ranks `ncal` as the next target
(action: measure, priority 0.90). used but has no golden eval set — …
Act on it only when the user asks to improve a skill; run `bin/evolve
--dry-run` first and keep the ask-user cost gate before any model spend.
```

It is a pointer, not an action: the suggestion spends nothing, triggers no
optimizer run, and `review` targets (fix the trigger / prune) are never
suggested because that is a human's call. The skill's own dry-run →
ask-user-cost-gate → human-merge procedure still governs any real run.
Knobs: `PI_SKILL_SUGGEST=0` disables it, `PI_SKILL_SUGGEST_MAX_AGE_DAYS`
(7) expires a stale suggestion, `PI_SKILL_SUGGEST_REFRESH_MIN` (30) paces
recomputation.

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
package = repo = extension source), exposed to Pi twice by symlink, never by
copy:

| Deployment | Symlink |
| --- | --- |
| Skill | `~/.agents/skills/skill-evolution` → this tree |
| Layer-1 extension | `~/.pi/agent/extensions/skill-memory.ts` → `layer1/extension/skill-memory.ts` |

(one-way sync per the project's git-tree convention)

## 🔒 Hard guarantees

- Layer 1 writes only inside the skill roots; ops are atomic and
  rollback-able; the live extension is a symlink, so this tree is the only
  copy.
- Nothing pushed; deployment = local `evolve/` branch or `output/` copy.
- Constraint gates run before anything is written.
- Human review mandatory.

## Tests

```bash
source .venv/bin/activate && python -m pytest tests/ -q
node --experimental-strip-types --check layer1/extension/skill-memory.ts
pi --print --no-session "list your tool names"   # skill_manage present
```

## Provenance

Forged 2026-10-23 from the pre-existing `pi-skill-evolution` repo (relocated
into the skills tree, history intact) via skill-forge v1.8.0 tool-skill
profile; preflight verdict GO (`preflight.md`, untracked run artifact).
Layer 1 (`skill-memory.ts`, previously living only in
`~/.pi/agent/extensions/`) was folded into this tree on 2026-10-28 so both
layers are committed and pushed together.