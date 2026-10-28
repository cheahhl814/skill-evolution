# Layer-1 write policy — what `skill_manage` may write, and who says yes

> v1.6.0 (2026-10-28). The contract for the `create` op of the Layer-1
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
| **Convention drift** | A hand-rolled `SKILL.md` (no `version`, no `triggers`, no README, no dependency declaration) entered the tree and was loaded by every later session. |

The policy closes both: **a new skill needs the user's approval, and it must
satisfy the skill-forge contract before it lands.** Patching an existing skill
is unaffected — that is the healthy path.

## The two gates

Both gates live in the TOOL, not in the agent's judgement. The prompt section
and the session-end nudge *tell* the agent the policy; the tool *enforces* it.

### Gate 1 — the user approves every new skill

`create` calls `ctx.ui.confirm(...)` before a single byte is written. The
dialog lists each skill, its description and its size, and states why the
request is being made.

| Situation | Behaviour |
| --- | --- |
| Interactive session (`ctx.hasUI`) | The user is asked. **Declining writes nothing** — the op fails atomically with `create: declined by the user`. |
| No UI (`pi --print`, RPC-less mode) | **Fail closed.** `create: a new skill needs the user's approval and this session has no UI to ask`. A headless run must not silently grow the skill tree. |
| `PI_SKILL_MANAGE_ALLOW_CREATE=1` | The prompt is skipped — the explicit unattended opt-out. Gate 2 still applies. |

The approval is asked **once per batch**, not once per op: a batch that
creates two skills asks one question listing both.

### Gate 2 — skill-forge conformance, staged before any live write

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

Anything the linter reports as FAIL rejects the whole batch:

```
create: 'my-skill' failed the skill-forge lint gate
(skill-forge/bin/skill-lint.py, tool-skill profile) — nothing was written:
  FAIL [C1] my-skill: frontmatter missing or empty required key `version`
```

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

The `write_file` op may now follow a `create` op **in the same batch** — the
pre-flight resolves it against the skill directory the batch is about to
create. That is how the README travels with the skill.

## What is NOT gated

| Op | Gated? | Why |
| --- | --- | --- |
| `patch` | no | Correcting a skill that already exists cannot pile up new ones. This is the path the nudge should point at. |
| `write_file` (existing skill) | no | Same reasoning — it cannot create a skill. |
| `remove_file` | no | Removing cruft is the opposite of piling up. |
| `delete` | no | Destructive, but it *reduces* the pile. Symlink-aware: it removes the deployment link, not the source tree. |

## Knobs

| Env var | Default | Meaning |
| --- | --- | --- |
| `PI_SKILL_MANAGE_ALLOW_CREATE` | unset (approval required) | `1` skips the user-approval prompt for `create` (unattended runs). Gate 2 still applies. |
| `PI_SKILL_MANAGE_LINT_TIMEOUT_MS` | 20000 | Budget for the skill-forge linter run. |
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
fourth, shorter form.

## Tests

```bash
node tests/test_extension_gate.mjs      # 13 tests, no framework, no network
```

The harness stages a copy of the real extension next to the packages it
imports, under a throwaway `HOME`, and drives the registered `skill_manage`
tool directly with a mocked `ExtensionAPI`. It covers: decline → nothing
written; approve → the skill lands with its README; no UI → fail closed;
`ALLOW_CREATE=1` → no prompt; a missing README / `version` / `triggers` /
`skills:` dependency → rejected; an over-long description → rejected by the
lint gate; a batch of two → one prompt, both land; `patch` → never asks; no
temp-dir leak.

## Failure signatures

| Signature | Cause | Fix |
| --- | --- | --- |
| `create: a new skill needs the user's approval and this session has no UI` | Headless run (`pi --print`) | Run interactively, or set `PI_SKILL_MANAGE_ALLOW_CREATE=1` deliberately. |
| `create: declined by the user — nothing was written.` | The user said no | Expected. Patch an existing skill instead. |
| `create: 'x' does not follow the skill-forge convention: - frontmatter must declare the external dependency…` | The frontmatter has no `skills:` list | Add `skills:\n  - skill-forge` to the frontmatter. |
| `create: 'x' does not follow the skill-forge convention: - a new skill must ship a README.md` | The batch had no `write_file` for `README.md` | Add a `write_file` op with `file_path: "README.md"` after the `create` op. |
| `create: 'x' failed the skill-forge lint gate …` | The linter reported FAIL | Read the FAIL lines: they name the check (C1 / C1-desc / C11 / C12). Fix and retry. |
| `create: skill-forge's linter was not found` | skill-forge is not installed in any skill root | Install skill-forge — the gate cannot be skipped by design. |
