# Monitor, triage and the session-end digest — full reference

Companion to `SKILL.md` §"Monitor + triage" and §"Session-end digest". The
SKILL.md keeps the contract short; this file holds the detail.

## 1. Monitor — reading the telemetry

| Property | Value |
| --- | --- |
| Telemetry file | `~/.local/share/skill-evolution/skill-usage.jsonl` (`PI_SKILL_TELEMETRY_PATH`) |
| Reader | `evolution/monitor/telemetry.py` — `read_events()` + `aggregate()` |
| Writer | `layer1/extension/skill-memory.ts` — `record()` / `recordToolUsage()` |
| Wire contract | `layer1/telemetry-schema.md` |
| Recency | 14-day half-life (`0.5 ** (age_days / 14)`) |
| Robustness | malformed lines are skipped, unknown `event` values are ignored, a missing file is not an error |

Event weights (per event, before decay):

| Event | Weight | Why |
| --- | --- | --- |
| `skill_loaded` | 1.0 | `skill_search(action=load)` — the agent pulled the skill into context: the strongest "used" signal |
| `skill_managed` | 0.8 | a `skill_manage` op — authoring means the agent cared |
| `skill_read` | 0.6 | a `read` of a `SKILL.md` — the skill was consulted, but the harness listing it is not usage |
| `skill_failed` | 1.5 | a failed `skill_manage` — trouble is the actionable signal, and it must outweigh a success |
| `learning_tool` | 0.05 | `wiki_retro` / `wiki_observe` — global, kept in a synthetic `__learning__` bucket, never per skill |

## 2. Triage — ranking

```bash
bin/triage                      # markdown table, ranked
bin/triage --json               # machine-readable
bin/triage --only-action measure
bin/triage --root /extra/skills # extra skill root (repeatable)
bin/triage --half-life 30       # recency half-life in days
```

Formula: **priority = usage × potential**, where `usage` is the decayed
telemetry score and `potential` is a structural estimate of what optimizing
that skill would buy.

| Condition | Potential | Action | Why this order |
| --- | --- | --- | --- |
| used, no golden dataset | 0.90 | `measure` | an unmeasured skill cannot be evolved responsibly — build `datasets/skills/<name>/golden.jsonl` first |
| used, golden dataset, over the 15KB cap | 0.75 | `shrink` | the size cap blocks the deploy gate; shrinking is the goal |
| used, ≥3 authoring ops (churn) | 0.70 | `stabilize` | a body rewritten three times is unstable — rewrite it before optimizing it |
| used, golden dataset | 0.60 | `evolve` | the normal GEPA candidate (human merge required) |
| used, failed authoring ops | 0.55 | `debug` | fix the failure signature before optimizing anything |
| installed, never used | 0.35 | `review` | either the description trigger is wrong, or the skill is dead weight |
| — | 0.0 | `none` | no signals at all |

`measure` outranks `evolve` on purpose. The optimizer's own guardrails
(frontmatter contract, lint gate, holdout) are only meaningful when there is
an eval set; optimising an unmeasured skill produces a number with no
baseline, which is exactly how the first Layer-2 run produced a clean +0.000.

Triage is read-only: it never runs the optimizer, never merges a branch, and
never spends model tokens. It is the input to a *human-gated* `bin/evolve`
run, not a replacement for one.

## 3. Session-end digest

The Layer-1 extension runs the monitor on `session_shutdown` and appends a
digest, so every session leaves a record of what to evolve next.

```
~/.local/share/skill-evolution/digests/2026-10-28.md   # one file per UTC day
~/.local/share/skill-evolution/digests/latest.md        # a copy of the newest section
~/.local/share/skill-evolution/digests/state.json       # {lastTs, top} for the delta
```

```
## 2026-10-28 03:28:55Z — session s-mvrk1tp5-4mbdfx (shutdown: quit)

- this session: 2 skill event(s) (1 loaded, 1 read), 1 learning call(s)
- top action: **measure** → `ncal` (priority 0.90)
- Δ vs last digest: unchanged since last digest

| # | Skill | Usage | Potential | Priority | Action | Why |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `ncal` | 1.00 | 0.90 | 0.90 | **measure** | used but has no golden eval set — build … |
```

| Property | Behaviour |
| --- | --- |
| Cost | **zero** — the monitor is stdlib-only and makes no model calls |
| Scope | read-only; never evolves, never merges |
| Skipped | `reason: "reload"` — a `/reload` must not spam the digest directory |
| Idempotency | `session_shutdown` can fire twice for one session; a per-session flag keeps it to one section |
| Subprocess | `python -m evolution.monitor.triage --json`, cwd = repo root, `PYTHONPATH` = repo root, hard timeout `PI_SKILL_TRIAGE_TIMEOUT_MS` (15 s). Prefers `<repo>/.venv/bin/python`, falls back to `python3` |
| Repo discovery | `PI_SKILL_EVOLUTION_ROOT`, default `~/.agents/skills/skill-evolution` (the deployed symlink) |
| Pruning | `.md` files older than `PI_SKILL_TRIAGE_DIGEST_KEEP_DAYS` (30) are removed; `latest.md` and `state.json` are kept |
| Failure | every step is wrapped — a digest failure must never break shutdown |

Format contract: `layer1/digest-schema.md`.

## 4. Knobs (all Layer-1 side)

| Env var | Default | Meaning |
| --- | --- | --- |
| `PI_SKILL_TELEMETRY` | 1 | `0` disables the telemetry writer |
| `PI_SKILL_TELEMETRY_PATH` | `~/.local/share/skill-evolution/skill-usage.jsonl` | telemetry JSONL |
| `PI_SKILL_TELEMETRY_MAX_BYTES` | 5242880 | rotation threshold (renames to `<path>.1`) |
| `PI_SKILL_TRIAGE_ON_SHUTDOWN` | 1 | `0` disables the session-end digest |
| `PI_SKILL_EVOLUTION_ROOT` | `~/.agents/skills/skill-evolution` | where the monitor lives |
| `PI_SKILL_TRIAGE_DIGEST_DIR` | `~/.local/share/skill-evolution/digests` | digest output |
| `PI_SKILL_TRIAGE_TIMEOUT_MS` | 15000 | subprocess timeout |
| `PI_SKILL_TRIAGE_TOP` | 10 | rows in the digest table |
| `PI_SKILL_TRIAGE_DIGEST_KEEP_DAYS` | 30 | prune older digest files |
| `PI_SKILL_SUGGEST` | 1 | `0` disables the prompt suggestion |
| `PI_SKILL_SUGGEST_MAX_AGE_DAYS` | 7 | a suggestion older than this is not injected |
| `PI_SKILL_SUGGEST_REFRESH_MIN` | 30 | minutes between suggestion recomputations |

## 5. The prompt suggestion (the extension suggests, the agent acts)

Once per session (`session_start`) the extension asks the monitor for the
single highest-priority **actionable** target:

```bash
python -m evolution.monitor.triage --suggest
# {"skill": "ncal", "action": "measure", "priority": 0.899, "usage": 0.999,
#  "reason": "used but has no golden eval set — build …/golden.jsonl before evolving"}
```

and injects it into the `skill_memory` prompt section:

```
Skill self-improvement: the triage digest ranks `ncal` as the next target
(action: measure, priority 0.90). used but has no golden eval set — …
Act on it only when the user asks to improve a skill; run `bin/evolve
--dry-run` first and keep the ask-user cost gate before any model spend.
Never push: the candidate is a local branch for human review.
```

| Rule | Why |
| --- | --- |
| Only `measure` / `evolve` / `shrink` / `stabilize` / `debug` are suggested | `review` ("fix the trigger or prune it") is a human's call, not a skill run |
| A target with `usage == 0` is never suggested | optimising a skill nobody uses is waste |
| The suggestion is a pointer, never an action | it spends nothing and starts no optimizer run; the skill's dry-run → ask-user-cost-gate → human-merge procedure still governs |
| Computed at `session_start`, refreshed at most every `PI_SKILL_SUGGEST_REFRESH_MIN` minutes | a subprocess per turn would be wasteful; one per session is ~0.3 s and free |
| Expires after `PI_SKILL_SUGGEST_MAX_AGE_DAYS` | a week-old target is probably not the next one |
| Stdlib-only subprocess (`python -m evolution.monitor.triage --suggest`, `PYTHONPATH=<repo>`) | no venv, no model calls, no cost |

The suggestion is also recorded in the telemetry feed as
`{"event": "suggestion", "skill": …, "op": <action>, "source": "triage"}`, so
"did the agent act on the suggestion?" is answerable from the same JSONL.

