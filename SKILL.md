---
name: skill-evolution
description: 'Two-layer skill self-improvement for Pi: Layer 1 runtime (the skill_manage extension that patches skills in-session behind an accumulated-experience bar and user approval, records per-skill usage telemetry, writes a triage digest at session end, and suggests the ranked next target in the prompt) and Layer 2 offline optimizer (DSPy+GEPA over SKILL.md bodies) with a monitor/triage step that ranks which skill to evolve first. Mine session history or golden JSONL into eval data, evolve the skill body under constraint gates (15KB cap, ≤1.2x growth, frontmatter contract, lint gate), and deploy as a local evolve/ branch for human review — never pushes. Use when asked to "optimize/evolve a skill", "improve a SKILL.md offline", "run layer-2", "which skill should I evolve", "triage digest", "pi-skill-evolution", "skill evolution", or about the skill_manage runtime layer. Always dry-run first; ask-user gate before any model spend; report +0.000 outcomes as honest no-ops.'
version: 1.8.0
updated: "2026-11-05"
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
| **Layer 3** | Extension monitor: inventories deployed extensions (symlink-resolved, repo-rooted), attributes session `toolResult` calls + errors post-hoc, ranks them `fix` / `watch` / `healthy` / `dusty` / `inherent`; zero new runtime hooks; a `fix` patches the source repo on a review branch | `evolution/monitor/ext_*.py`, `bin/triage --extensions` | offline, on demand + digest section |
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
│   ├── telemetry-schema.md           # the telemetry wire contract
│   └── digest-schema.md              # the session-end digest format
├── evolution/                        # OFFLINE layer (DSPy+GEPA)
│   └── monitor/                      # telemetry reader + triage + evidence bar
├── references/                       # full detail (write policy, procedure, signatures)
├── bin/evolve, bin/triage, bin/smoke-candidate, bin/skill-update-check.py
├── datasets/skills/<name>/golden.jsonl
└── tests/                            # pytest + the Node write-gate harness
```

## Layer 1 — runtime (`skill_manage`)

The extension is the **write side** of Pi's natively read-only skill system.

| Capability | Detail |
| --- | --- |
| Tool | `skill_manage` — ops `create` / `patch` / `write_file` / `remove_file` / `delete`, applied ATOMICALLY (any failure rolls back every touched skill) |
| Target root | `~/.agents/skills/` (extra roots via `PI_SKILL_MANAGE_ROOTS`, `:`-separated) |
| Guardrails | name regex, realpath-escape guard, frontmatter contract (name == target, description required), 15KB cap, patch `old_string` must match exactly once, >5% growth warning, full-tree snapshot + rollback, symlink-aware delete (removes the deployment link, keeps the source tree) |
| Create gate | Every skill write (`create` / `patch` / `write_file`, and a direct `edit` of a deployed `SKILL.md`) is gated twice: the accumulated-experience bar (`evolution/monitor/evidence.py` — ≥2 sessions AND a deficiency signal) and the user's approval (`ctx.ui.confirm`; no UI = fail closed). A `create` must also pass skill-forge's lint gate. Policy: `references/layer1-write-policy.md` |
| Prompt injection | `before_agent_start` adds the `skill_memory` section (skills-first routing: task knowledge → skill, cross-session facts → wiki) |
| Nudge | `agent_before_settle` delivers ONE learning nudge per session (≥ `PI_SKILL_MANAGE_NUDGE_MIN_TOOLS` tool calls, no learning tool used) |
| Telemetry | Appends per-skill usage events (`skill_loaded` via `skill_search`, `skill_read` via `read` of a `SKILL.md`, `skill_managed` / `skill_failed` via `skill_manage`, `learning_tool`) to a JSONL outside the skill tree — the feed the monitor/triage step consumes. Schema: `layer1/telemetry-schema.md` |
| Session-end digest | On `session_shutdown` the extension runs the monitor (`evolution.monitor.triage --json`, stdlib-only) and appends a dated digest: ranking, top action, delta vs the previous digest. Skipped on `/reload` |
| Suggestion | One ranked target per session (`triage --suggest`) injected into the `skill_memory` prompt section. Pointer only — no spend, no automatic run |
| Env knobs | `PI_SKILL_MANAGE_ROOTS`, `PI_SKILL_MANAGE_MAX_SKILL_BYTES` (15360), `PI_SKILL_MANAGE_ALLOW_CREATE` / `PI_SKILL_MANAGE_ALLOW_REVISION` (=1 skips that approval), `PI_SKILL_EVIDENCE_MIN_*` (the bar), `PI_SKILL_MANAGE_NUDGE_MIN_TOOLS` (6), `PI_SKILL_MANAGE_DISABLE_NUDGE`; the full list is in `references/layer1-write-policy.md` and `references/monitor-and-digest.md` §4 |

**Deployment** — the live file is a symlink into this repo, never a copy
(`ln -sfn <repo>/layer1/extension/skill-memory.ts ~/.pi/agent/extensions/`).
**Verify after any change**: `node --experimental-strip-types --check
layer1/extension/skill-memory.ts`, then `pi --print --no-session "list your
tool names"` — `skill_manage` must be present. Layer-1 code changes go
through the same review and push as Layer-2 code; no second repo, no second
copy to drift.

## Skill writes are gated

A new skill is the only unbounded growth in the runtime loop: Pi loads every
skill at every session start, so a skill that is never used again is dead
weight forever, and a revision made in an unrelated session leaks into that
session's handoff. Every skill write — `create`, `patch`, `write_file`, and
a direct `edit` of a deployed `SKILL.md` — passes two gates:

1. **The accumulated-experience bar** (`evolution/monitor/evidence.py`): a
   conclusion needs accumulated experience, not one session's impression.
   *Revise* only a skill used across ≥2 sessions that also shows a deficiency
   signal (revisited ≥3 times in one session, a failed write, proposed twice);
   *create* only when the same need was proposed in ≥2 independent sessions.
2. **The user's permission** (`ctx.ui.confirm`; no UI = fail closed). The
   agent cannot self-authorise a skill write.

A `create` must additionally follow the skill-forge convention (frontmatter
with name/description/version/triggers, a `README.md` in the same batch, and
`skill-forge` in the frontmatter `skills:` list) and pass skill-forge's
linter in a temp staging dir. Policy, knobs, failure signatures:
`references/layer1-write-policy.md`.

## Session-end digest

On `session_shutdown` the extension runs the monitor and appends a digest
(`~/.local/share/skill-evolution/digests/YYYY-MM-DD.md` + `latest.md`): zero
model calls, skipped on `/reload`, one section per session, 30-day pruning,
plus an `### Extensions (post-hoc, 30d)` subsection (`ext_triage --md`; skip
with `PI_SKILL_EXT_TRIAGE_ON_SHUTDOWN=0`). Format: `layer1/digest-schema.md`.

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
be evolved responsibly yet. `bin/triage --extensions` runs the Layer-3 ranking
(hook/UI extensions are `inherent` — they fire every session, dust is
meaningless). Every digest also carries a `### Drift (local-only)` table (`ext_drift.py` DIRTY/
AHEAD/NO-ORIGIN/NO-GIT; `--check-drift` fetches to add BEHIND). §6.

## Extension suggests, agent acts

One suggestion per session (`--suggest` → highest-priority actionable target)
is injected into the `skill_memory` prompt — for skills AND, since v1.2,
for the extension triage's `fix` target (async, so startup never blocks).
Pointer only — nothing is spent; `review`/`dusty` targets are never
suggested. `references/monitor-and-digest.md` §5.

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

Full detail: `references/layer2-procedure.md`. The eight steps, in one line
each:

1. **Dry-run always.** `--dry-run` costs nothing; a mined count of **0 is a
   BUG, not a result**.
2. **Ask-user gate before any real run.** Evidence + Recommend + Options —
   the model is the cost decision (`ollama_chat/<model>` vs `openai/gpt-4.1`).
3. **Run.** GEPA, `max_metric_calls = iterations × 10`, 4 threads.
4. **Interpret honestly.** `+0.000` / "nothing deployed" is a CORRECT no-op —
   report it, do not retry. Distrust a delta when `evolved_bytes ==
   baseline_bytes` (the body is byte-identical; the delta is noise).
5. **Review/merge (human, mandatory).** A local `evolve/<skill>-<ts>` branch
   or an `output/<skill>/<ts>/` copy. Never push.
6. **After any deploy() run: `git symbolic-ref HEAD`** — deploy leaves the
   repo on the evolve branch even when the commit fails.
7. **Smoke the candidate (`--smoke`).** Lint cannot check that the skill's
   commands run; `bin/smoke-candidate` does.
8. **Verify the candidate (`--verify`).** Independent-family blind A/B gate
   vetoes ritual gaming; see §8 and `evolution/verifier.py`.

## Signature library — known failure signatures

The full catalogue (signature → cause → fix, including miner regressions,
ritual-gaming re-scores, stale deployment symlinks) lives in
`references/signature-library.md`. Two universal rules: lint-passed ≠
runnable (`bin/smoke-candidate`), and a proxy-metric win that adds ritual
text and deletes worked examples is gaming, not improvement — re-score with
the rubric judge before believing it.

## Constants / knobs

| Env var | Default | Meaning |
| --- | --- | --- |
| `PI_EVOLUTION_OPTIMIZER` | `openai/gpt-4.1` | GEPA reflection/darwin optimizer model |
| `PI_EVOLUTION_EVAL` | `openai/gpt-4.1-mini` | Eval/task model |
| `PI_EVOLUTION_JUDGE` | *(falls back to `PI_EVOLUTION_EVAL`)* | Judge model (different family from eval recommended) |
| `PI_EVOLUTION_VERIFIER` | *(unset → gate off)* | Verifier model — must be different family from optimizer/eval/judge |
| `PI_EVOLUTION_MAX_SKILL` | 15360 | SKILL.md byte cap |
| `PI_EVOLUTION_MAX_GROWTH` | 1.2 | Accepted byte growth vs baseline |
| `PI_SKILL_*` | see `references/monitor-and-digest.md` §4–5 | Layer-1 telemetry, digest and suggestion controls |

## Hard guarantees

- Layer 1 never writes outside the skill roots; every op is atomic and
  rollback-able, and the live extension is a symlink into this repo — this
  tree is the single source of truth for both layers.
- A new skill is never written without the user's approval and never without
  passing skill-forge's lint gate; patching an existing skill is ungated.
- Nothing is ever pushed. Deployment = local branch in this repo (human
  review via diff) or an `output/` copy.
- Baseline constraints must hold (or be improved) — an already-over-cap skill
  is flagged, not silently shipped.
- The skill-forge linter (`bin/skill-lint.py`) is auto-detected and enforced as
  the structural gate when present.
- Triage is read-only: it reads telemetry, ranks, and prints. It never runs
  the optimizer, never merges a branch, and never spends model tokens.
- The verifier gate (when enabled) vetoes candidates that game the judge via
  shared-family bias or rubric-parroting rituals; `UNAVAILABLE` never silently
  passes.

## Handoff

If the run produces a candidate, the user reviews it here — this repo's work
tree. Offer: merge to master + push, keep the branch, or drop it. Then update
the target skill's `handoff.md` per the handoff-skill contract.