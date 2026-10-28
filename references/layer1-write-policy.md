# Layer-1 write policy — what `skill_manage` may write, and who says yes

> v1.7.0 (2026-10-28). The contract for the write ops of the Layer-1
> extension (`layer1/extension/skill-memory.ts`). The extension is the only
> registrar of the `skill_manage` tool in the whole stack, so this file is the
> place where "may the agent write a skill?" is answered.

## The problem this policy solves

Pi loads every skill at every session start, from `~/.agents/skills/`. A skill
that is written once and never used again is dead weight in every future
session — and it cannot be un-written by the agent, only deleted by hand. A
runtime loop that creates skills on its own therefore piles up cruft faster
than it produces value.

Two failure modes were observed in the wild:

| Failure | What happened |
| --- | --- |
| **Unbounded creation** | Sessions "discovered" a workflow, wrote a new skill, and the skill was never used again. Skills stacked up in `~/.agents/skills/` with no way for the user to opt out. |
| **Revision contamination** | The 2026-10-28 OceanDecade round: a session working on a proposal also revised `design-pack` and `wpmgn-pack-sync`, and both revisions landed in **that session's handoff** — so the handoff carried skill-maintenance work that had nothing to do with the session's task. The nudge fired on tool-call count alone ("≥6 tool calls, no learning tool used"), with no check for whether any skill was actually deficient. |
| **Convention drift** | A hand-rolled `SKILL.md` (no `version`, no `triggers`, no README, no dependency declaration) entered the tree and was loaded by every later session. |

The policy closes all three: **a skill write needs accumulated experience
behind it, the user's approval, and — for a new skill — the skill-forge
convention.** Patching an existing skill is not a free-for-all either: it is
the healthy path, but it is still a conclusion, so it needs the same two
gates.

## The gates

All three live in the TOOL, not in the agent's judgement. The prompt section
and the session-end nudge *tell* the agent the policy; the tool *enforces* it.

### Gate 1 — the accumulated-experience bar

A conclusion needs accumulated experience, not one session's impression. The
model lives in `evolution/monitor/evidence.py` (the Python side owns the
telemetry contract; the extension asks the monitor, so the two cannot drift).

**Revise** an existing skill — the skill must clear BOTH:

| Condition | Default threshold | Why |
| --- | --- | --- |
| **Breadth** — used across ≥ N independent sessions | `PI_SKILL_EVIDENCE_MIN_SESSIONS` = 2 | A skill used in one session is a one-off, not a pattern. |
| **Deficiency** — at least one of: revisited ≥ R times inside one session, a failed write, or proposed for revision in ≥ P independent sessions | `PI_SKILL_EVIDENCE_MIN_REVISITS` = 3, `PI_SKILL_EVIDENCE_MIN_FAILURES` = 1, `PI_SKILL_EVIDENCE_MIN_PROPOSALS` = 2 | A well-used skill needs no work. Breadth alone is not a defect. |

**Create** a new skill — a skill that does not exist has no usage history, so
the only accumulated experience available is the agent's own repeated
conclusion: the same name proposed in ≥ P independent sessions
(`PI_SKILL_EVIDENCE_MIN_PROPOSALS` = 2). Two proposals in the *same* session
are not independent and do not count.

A proposal is recorded whenever the agent *attempts* a `create` or `patch` —
the attempt is the conclusion, whether or not it was approved.

```bash
# ask the monitor directly
python -m evolution.monitor.evidence --skill ncal --kind revise --json
python -m evolution.monitor.evidence --eligible --json
```

### Gate 2 — the user's permission

`ctx.ui.confirm` — one dialog per batch (two skills in one batch ask once).

| Situation | Behaviour |
| --- | --- |
| Interactive session (`ctx.hasUI`) | The user is asked. **Declining writes nothing.** |
| No UI (`pi --print`, RPC-less mode) | **Fail closed.** |
| `PI_SKILL_MANAGE_ALLOW_CREATE=1` | The create prompt is skipped (unattended opt-out). The bar and the lint gate still apply. |
| `PI_SKILL_MANAGE_ALLOW_REVISION=1` | The same for revisions. |

### Gate 3 — the skill-forge convention (new skills only)

The candidate is built in a temp directory (`skill-manage-stage-*` under
`$TMPDIR`), never in the live tree, and must pass skill-forge's own
structural linter before it is allowed to land:

```bash
python3 <skill-forge>/bin/skill-lint.py <staging-dir> \
        --profile tool-skill --include-vendored --skill-dirs <skill roots>
```

* `--profile tool-skill` — the profile skill-forge defines for a
  frontmatter + README + lint-gate skill.
* `--include-vendored` — without it a `SKILL.md` with no `version` field is
  *skipped* rather than linted, which would be a hole in the gate.
* `--skill-dirs` — dependency resolution (C12) uses the same roots the tool
  writes to, so a declared `skills:` entry must resolve where it will live.

Anything the linter reports as FAIL rejects the whole batch.

`skill-forge` is the **external dependency** that owns this contract. A
created skill must therefore:

| Requirement | Where it is checked |
| --- | --- |
| `name` matches the skill name | the extension (pre-flight) + C1 |
| `description` present, ≤ 1024 chars | C1 / C1-desc |
| `version` present | the extension + C1 |
| `triggers` list non-empty | the extension + C1 |
| `skills:` declares `skill-forge` | the extension (explicit dependency) |
| `skills:` entries resolve to a real skill dir | C12 (the linter) |
| `README.md` shipped in the same batch | the extension (a `write_file` op with `file_path: "README.md"`) |
| No unfilled slots / machine paths in the README | C11 (the linter) |

The `write_file` op may follow a `create` op **in the same batch** — the
pre-flight resolves it against the skill directory the batch is about to
create. That is how the README travels with the skill.

## The direct-edit path

`skill_manage` is not the only way to change a skill. A plain `edit` or
`write` of a deployed `SKILL.md` is a revision too, and in the OceanDecade
round that is exactly how `design-pack` was revised. The extension therefore
intercepts `tool_call` events for `edit`/`write` whose path is a `SKILL.md`
under a skill root and applies **the same two gates** (bar, then permission),
blocking the call when they are not met.

The exemption: a skill whose source tree **is** the session's working
directory. That is ordinary work — the agent is developing that skill, not
the runtime revising one.

Only skill *definitions* are gated. A `references/notes.md` inside a skill
is not gated by the direct-edit path.

## What is NOT gated

| Op | Gated? | Why |
| --- | --- | --- |
| `patch` | bar + permission | A revision is a conclusion. |
| `write_file` (existing skill) | bar + permission | Same. |
| `create` | bar + permission + lint | The heaviest op. |
| `remove_file` | bar + permission | Removing cruft is a conclusion about the skill too. |
| `delete` | bar + permission | Destructive, but it *reduces* the pile. |

## Knobs

| Env var | Default | Meaning |
| --- | --- | --- |
| `PI_SKILL_MANAGE_ALLOW_CREATE` | unset (approval required) | `1` skips the user-approval prompt for `create`. The bar and the lint gate still apply. |
| `PI_SKILL_MANAGE_ALLOW_REVISION` | unset (approval required) | `1` skips the user-approval prompt for revisions. The bar still applies. |
| `PI_SKILL_EVIDENCE_MIN_SESSIONS` | 2 | Breadth: distinct sessions of use. |
| `PI_SKILL_EVIDENCE_MIN_REVISITS` | 3 | Revisits inside one session. |
| `PI_SKILL_EVIDENCE_MIN_FAILURES` | 1 | Failed writes against the skill. |
| `PI_SKILL_EVIDENCE_MIN_PROPOSALS` | 2 | Independent sessions that proposed the change. |
| `PI_SKILL_MANAGE_LINT_TIMEOUT_MS` | 20000 | Budget for the skill-forge linter run. |
| `PI_SKILL_MANAGE_EVIDENCE_TIMEOUT_MS` | 15000 | Budget for the monitor query. |
| `PI_SKILL_MANAGE_ROOTS` | — | `:`-separated extra skill roots; also passed to the linter as `--skill-dirs`. |
| `PI_SKILL_MANAGE_MAX_SKILL_BYTES` | 15360 | The 15KB cap (unchanged). |

## The three copies of the routing rule

The "ask before creating; follow skill-forge" rule exists in three places and
**all three must agree** (this is the sync rule from
`layer1/agents-routing-section.md`):

1. The extension's `skill_memory` prompt section (`before_agent_start`) — what
   the agent reads every session.
2. `~/.pi/agent/AGENTS.md` §7 — the global instruction file.
3. `layer1/agents-routing-section.md` — the verbatim copy of (2) in this repo.

The session-end nudge (`agent_before_settle`) carries the same rule in a
fourth, shorter form — and it now points at the **wiki** as the default
sink, so a session with nothing reusable to save does not get pushed into
skill authoring.

## Tests

```bash
node tests/test_extension_gate.mjs      # 23 tests, no framework, no network
python -m pytest tests/ -q               # 45 tests
```

The Node harness stages a copy of the real extension next to the packages it
imports, under a throwaway `HOME`, and drives the registered `skill_manage`
tool directly with a mocked `ExtensionAPI`. It covers: the bar refusing a
one-off; a revision needing approval; approval applying the patch; no UI
failing closed; a batch asking once; the direct-edit gate (outside cwd
blocked, inside cwd allowed, non-SKILL.md never gated); and the create bar
and lint gate.

The Python tests pin the evidence model itself: one session is not enough,
breadth without a deficiency is not enough, revisits / failures / repeated
proposals each clear it, two proposals in one session are not independent,
and the triage suggestion skips a skill below the bar.

## Failure signatures

| Signature | Cause | Fix |
| --- | --- | --- |
| `create: 'x' does not clear the accumulated-experience bar` | The need was proposed fewer than `PI_SKILL_EVIDENCE_MIN_PROPOSALS` times, in independent sessions | Record the observation in the wiki; the bar is crossed by real use. |
| `revise: 'x' does not clear the accumulated-experience bar` | The skill was used in too few sessions, or shows no deficiency signal | Not a conclusion yet. Use the wiki; revisit when the signal appears. |
| `create: not approved by the user` | The user declined | Expected. Nothing was written. |
| `create: a new skill needs the user's approval and this session has no UI` | Headless run (`pi --print`) | Run interactively, or set `PI_SKILL_MANAGE_ALLOW_CREATE=1` deliberately. |
| `revise: a skill write needs the user's approval and this session has no UI` | Headless run | Run interactively, or set `PI_SKILL_MANAGE_ALLOW_REVISION=1` deliberately. |
| `create: 'x' does not follow the skill-forge convention` | Missing version / triggers / README / `skills:` dependency | Fix the frontmatter or add the `write_file` op for README.md. |
| `create: 'x' failed the skill-forge lint gate` | The linter reported FAIL | Read the FAIL lines: they name the check (C1 / C1-desc / C11 / C12). Fix and retry. |
| `create: skill-forge's linter was not found` | skill-forge is not installed in any skill root | Install skill-forge — the gate cannot be skipped by design. |

