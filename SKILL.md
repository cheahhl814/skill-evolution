---
name: skill-evolution
description: 'Two-layer skill self-improvement for Pi: Layer 1 runtime (the skill_manage extension that writes and patches skills in-session, records per-skill usage telemetry, writes a triage digest at session end, and suggests the ranked next target in the prompt) and Layer 2 offline optimizer (DSPy+GEPA over SKILL.md bodies) with a monitor/triage step that ranks which skill to evolve first. Mine session history or golden JSONL into eval data, evolve the skill body under constraint gates (15KB cap, ≤1.2x growth, frontmatter contract, lint gate), and deploy as a local evolve/ branch for human review — never pushes. Use when asked to "optimize/evolve a skill", "improve a SKILL.md offline", "run layer-2", "which skill should I evolve", "triage digest", "pi-skill-evolution", "skill evolution", or about the skill_manage runtime layer. Always dry-run first; ask-user gate before any model spend; report +0.000 outcomes as honest no-ops.'
version: 1.5.0
updated: "2026-10-28"
triggers:
  - "evolve a skill"
  - "optimize skill"
  - "layer-2 evolution"
  - "skill evolution"
  - "pi-skill-evolution"
  - "skill_manage"
  - "runtime skill memory"
  - "which skill should I evolve"
  - "skill usage telemetry"
  - "triage digest"
requires:
  - "uv (python env for the vendored .venv)"
  - "A model backend: ollama (cloud or local) or an OpenAI-compatible key"
  - "pi (for the Layer-1 extension; jiti loads the TypeScript source, no build step)"
---

# Skill self-improvement — Layer 1 runtime + Layer 2 offline optimizer

## What this skill is

**One tree, both layers.** This directory is the Pi skill
(`~/.agents/skills/skill-evolution` symlink → this tree), the Python package,
the git repo (`github.com/cheahhl814/skill-evolution`) — and, since
2026-10-28, also the home of the **Layer-1 runtime extension**. Both halves
of the self-improvement loop are therefore versioned, reviewed and shipped
together, and a Layer-1 patch is a Layer-2 evolution target.

| Layer | What it is | Where it lives here | When it runs |
| --- | --- | --- | --- |
| **Layer 1** | `skill_manage` — writes/patches/deletes skills under guardrails; injects the skills-first routing prompt; one learning nudge per session; **records per-skill usage telemetry** | `layer1/extension/skill-memory.ts` (deployed as a symlink into `~/.pi/agent/extensions/`) | in-session, live |
| **Monitor** | Reads the telemetry, ranks skills by `usage × potential`, and says what to do next (`measure` / `evolve` / `shrink` / `stabilize` / `review`) | `evolution/monitor/`, `bin/triage` | offline, on demand |
| **Layer 2** | DSPy + GEPA optimizer over a skill's `SKILL.md` body, with eval data, constraint gates and a structural lint gate | `evolution/` package, `bin/evolve`, `datasets/`, `tests/` | offline, on demand |

Layer 1 handles skill CREATION and PATCHES during a session. Layer 2 is
OFFLINE optimization of a skill's instruction body with eval data + an
evolutionary optimizer, deploying only via a local branch you review and
merge.

## Layout

```
skill-evolution/                      # = repo = package = deployed skill
├── SKILL.md                          # this contract
├── layer1/                           # RUNTIME layer
│   ├── extension/skill-memory.ts     # the Pi extension (source of truth)
│   ├── agents-routing-section.md     # copy of ~/.pi/agent/AGENTS.md §7
│   └── telemetry-schema.md           # the telemetry wire contract
├── evolution/                        # OFFLINE layer (DSPy+GEPA)
│   └── monitor/                      # telemetry reader + triage ranking
├── bin/evolve, bin/triage, bin/skill-update-check.py
├── datasets/skills/<name>/golden.jsonl
└── tests/
```

## Layer 1 — runtime (`skill_manage`)

The extension is the **write side** of Pi's natively read-only skill system.

| Capability | Detail |
| --- | --- |
| Tool | `skill_manage` — ops `create` / `patch` / `write_file` / `remove_file` / `delete`, applied ATOMICALLY (any failure rolls back every touched skill) |
| Target root | `~/.agents/skills/` (extra roots via `PI_SKILL_MANAGE_ROOTS`, `:`-separated) |
| Guardrails | name regex, realpath-escape guard, frontmatter contract (name == target, description required), 15KB cap, patch `old_string` must match exactly once, >5% growth warning, full-tree snapshot + rollback, symlink-aware delete (removes the deployment link, keeps the source tree) |
| Prompt injection | `before_agent_start` adds the `skill_memory` section (skills-first routing: task knowledge → skill, cross-session facts → wiki) |
| Nudge | `agent_before_settle` delivers ONE learning nudge per session (≥ `PI_SKILL_MANAGE_NUDGE_MIN_TOOLS` tool calls, no learning tool used) |
| Telemetry | Appends per-skill usage events (`skill_loaded` via `skill_search`, `skill_read` via `read` of a `SKILL.md`, `skill_managed` / `skill_failed` via `skill_manage`, `learning_tool`) to a JSONL outside the skill tree — the feed the monitor/triage step consumes. Schema: `layer1/telemetry-schema.md` |
| Session-end digest | On `session_shutdown` the extension runs the monitor (`evolution.monitor.triage --json`, stdlib-only) and appends a dated digest: ranking, top action, delta vs the previous digest. Skipped on `/reload` |
| Suggestion | One ranked target per session (`triage --suggest`) injected into the `skill_memory` prompt section. Pointer only — no spend, no automatic run |
| Env knobs | `PI_SKILL_MANAGE_ROOTS`, `PI_SKILL_MANAGE_MAX_SKILL_BYTES` (15360), `PI_SKILL_MANAGE_NUDGE_MIN_TOOLS` (6), `PI_SKILL_MANAGE_DISABLE_NUDGE`; telemetry + digest knobs in `references/monitor-and-digest.md` §4 |

**Deployment** — the live file is a symlink into this repo, never a copy
(`ln -sfn <repo>/layer1/extension/skill-memory.ts ~/.pi/agent/extensions/`).
**Verify after any change**: `node --experimental-strip-types --check
layer1/extension/skill-memory.ts`, then `pi --print --no-session "list your
tool names"` — `skill_manage` must be present. Layer-1 code changes go
through the same review and push as Layer-2 code; no second repo, no second
copy to drift.

## Session-end digest

On `session_shutdown` the extension runs the monitor and appends a digest
(`~/.local/share/skill-evolution/digests/YYYY-MM-DD.md` + `latest.md`): zero
model calls, skipped on `/reload`, one section per session, 30-day pruning.
Format: `layer1/digest-schema.md`.

## Monitor + triage — pick the target before spending tokens

`bin/triage` ranks skills by `usage × potential` from the telemetry feed:
`usage` = recency-decayed telemetry score (14-day half-life: loaded 1.0,
managed 0.8, read 0.6, failed 1.5); `potential` = estimated payoff.

| Condition | Potential | Action |
| --- | --- | --- |
| used, no golden dataset | 0.90 | `measure` — build `datasets/skills/<name>/golden.jsonl` first |
| used, golden dataset, over the 15KB cap | 0.75 | `shrink` |
| used, ≥3 authoring ops (churn) | 0.70 | `stabilize` — rewrite before optimizing |
| used, golden dataset | 0.60 | `evolve` — GEPA candidate (human merge required) |
| installed, never used | 0.35 | `review` — fix the trigger description or prune it |

`measure` deliberately outranks `evolve`: a used-but-unmeasured skill cannot
be evolved responsibly yet. This is the port of Hermes' Phase-5 auto-triage
("rank by potential improvement × usage frequency"), which upstream still
leaves a placeholder.

## Extension suggests, agent acts

The extension computes one suggestion per session
(`evolution.monitor.triage --suggest` → the highest-priority actionable
target) and injects it into the `skill_memory` prompt section, so the agent
knows what the digest ranks next. It is a pointer, not an action: nothing is
spent and `review` targets are never suggested. Knobs and the exact prompt
text: `references/monitor-and-digest.md` §5.

## Layer 2 — invocation

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
7. **Smoke the candidate (`--smoke`).** Lint checks structure; it cannot
   check that the commands the skill tells the agent to run actually work.
   `bin/smoke-candidate` extracts them, runs the allowlisted ones, and
   rejects a candidate whose commands fail. A candidate can beat the
   baseline and still ship a broken command — the smoke gate is the only
   check that catches that.

## Signature library — known failure signatures

The six that bite most often; the full catalogue (every known signature, with
cause and fix) is in `references/signature-library.md`.

| Signature | Likely cause | Fix |
| --- | --- | --- |
| `mined 0 relevant session pair(s)` for an obviously-used skill | Miner token/parse regression | Run `pytest tests/ -q`; read `evolution/core/pi_sessions.py` — a char-join bug (" ".join over one string) shipped exactly this once. Fixed; trust tests |
| `TypeError: cannot unpack non-iterable SessionPair object` | Dataset builder unpacks old triples post-redesign | Consume `pair.user_text` fields; see `evolution/core/dataset_builder.py`. Fixed 2026-10 (regression-tested) |
| Layer-1 extension not in the live tool list | `~/.pi/agent/extensions/skill-memory.ts` is a stale copy or a broken symlink | The live file must be a symlink into `layer1/extension/`; fix the link, then `/reload` (or restart) — never keep a second copy |
| Candidate beats the baseline but a command in it does not run | Lint passed; the proxy metric cannot execute anything | Run `bin/smoke-candidate <candidate>` (or the run with `--smoke`): a command like `date -d "2024-03-15" +%V / %G` fails with `extra operand '/'`. Reject the candidate |
| Candidate scores higher but only adds a ritual (a fixed opening sentence, "list the command and its output") and deletes worked examples | The keyword-overlap proxy is being gamed, not the skill being improved | Re-score the baseline, the candidate and a ritual-free version: if only the ritual version wins, the gain is an artifact — drop it and fix the metric |
| Baseline `size_limit: exceeded` (e.g. 25KB) | Target skill genuinely over the 15KB cap | Evolve anyway (shrink is the goal) but expect deploy-gate friction; or set `PI_EVOLUTION_MAX_SKILL` to size the target honestly |

## Constants / knobs

| Env var | Default | Meaning |
| --- | --- | --- |
| `PI_EVOLUTION_OPTIMIZER` | `openai/gpt-4.1` | GEPA reflection/darwin optimizer model |
| `PI_EVOLUTION_EVAL` | `openai/gpt-4.1-mini` | Eval/task model |
| `PI_EVOLUTION_MAX_SKILL` | 15360 | SKILL.md byte cap |
| `PI_EVOLUTION_MAX_GROWTH` | 1.2 | Accepted byte growth vs baseline |
| `PI_SKILL_*` | see `references/monitor-and-digest.md` §4–5 | Layer-1 telemetry, digest and suggestion controls |

## Hard guarantees

- Layer 1 never writes outside the skill roots; every op is atomic and
  rollback-able, and the live extension is a symlink into this repo — this
  tree is the single source of truth for both layers.
- Nothing is ever pushed. Deployment = local branch in this repo (human
  review via diff) or an `output/` copy.
- Baseline constraints must hold (or be improved) — an already-over-cap skill
  is flagged, not silently shipped.
- The skill-forge linter (`bin/skill-lint.py`) is auto-detected and enforced as
  the structural gate when present.
- Triage is read-only: it reads telemetry, ranks, and prints. It never runs
  the optimizer, never merges a branch, and never spends model tokens.

## Handoff

If the run produces a candidate, the user reviews it here — this repo's work
tree. Offer: merge to master + push, keep the branch, or drop it. Then update
the target skill's `handoff.md` per the handoff-skill contract.