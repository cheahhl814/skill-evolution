# Layer-1 telemetry schema — `skill-usage.jsonl`

The Layer-1 extension records **per-skill usage** so Layer 2 can triage *what
is worth evolving*. This file is the single contract between the TypeScript
writer and the Python reader; if you change it, change both.

## Location & lifecycle

| Property | Value |
| --- | --- |
| Default path | `~/.local/share/skill-evolution/skill-usage.jsonl` |
| Override | `PI_SKILL_TELEMETRY_PATH` |
| Disable | `PI_SKILL_TELEMETRY=0` |
| Rotation | `PI_SKILL_TELEMETRY_MAX_BYTES` (default 5 MB) → renames to `<path>.1` |
| Format | JSON Lines — one JSON object per line, append-only |
| Writer | `layer1/extension/skill-memory.ts` (`record()` / `recordToolUsage()`) |
| Reader | `evolution/monitor/telemetry.py` |

The file lives **outside** the skill tree on purpose: it is runtime data, not a
deliverable, and it must never be committed (per the AiX-BIO convention that
shared data targets absolute paths outside skill directories).

## Record shape

```json
{
  "ts": "2026-10-28T11:41:02.913Z",
  "session": "s-m0k3j1-9fq2xa",
  "event": "skill_loaded",
  "skill": "skill-evolution",
  "op": "",
  "tool": "",
  "source": "skill_search",
  "reason": ""
}
```

| Field | Type | Always present | Meaning |
| --- | --- | --- | --- |
| `ts` | ISO-8601 UTC | yes | write time |
| `session` | string | yes | per extension-load id; `/reload` starts a new one |
| `event` | enum (below) | yes | what happened |
| `skill` | skill name | for skill-scoped events | the skill the event is about |
| `op` | string | for `skill_managed` / `skill_failed` | the `skill_manage` op or `unknown` |
| `tool` | string | for `learning_tool` | the tool name |
| `source` | string | yes | which surface produced it (`skill_search`, `read`, `skill_manage`, `tool`) |
| `reason` | string | for `session_start` | `startup` / `reload` / `new` / `resume` / `fork` |

## Event vocabulary

| `event` | Emitted when | Signal strength (triage weight) |
| --- | --- | --- |
| `session_start` | the extension loads (startup/reload/new/resume/fork) | session counting |
| `skill_loaded` | `skill_search` with `action=load` — the agent pulled a skill into context | **1.0** (strongest "this skill is used") |
| `skill_read` | the `read` tool opened a path whose basename is `SKILL.md` | 0.6 |
| `skill_managed` | a `skill_manage` op targeted the skill (create/patch/write_file/remove_file/delete) | 0.8 (authored = engaged) |
| `skill_failed` | a `skill_manage` call failed; the skill name is parsed from the error | 1.5 (trouble signal) |
| `learning_tool` | `wiki_retro` / `wiki_observe` ran | 0.05 (global, not per skill) |
| `suggestion` | the extension computed the ranked target it injected into the prompt (`op` = the action) | 0 (marker only — it records that a suggestion was made, and which target) |

## What is NOT recorded

- No prompt text, no tool output, no file contents, no message bodies.
- Only skill names, op names, tool names and timestamps.
- Failed `skill_manage` ops record the *skill name parsed from the error
  message*; if the name cannot be parsed, `skill` is omitted and the event
  still counts toward the global failure count.

## Consumers

| Consumer | What it does |
| --- | --- |
| `evolution/monitor/telemetry.py` | parses, aggregates per skill, applies a 14-day recency half-life |
| `evolution/monitor/triage.py` | ranks skills by `usage × potential`, emits the action list |
| `bin/triage` | CLI wrapper: `bin/triage [--json] [--limit N]` |

Unknown `event` values are ignored by the reader (forward-compatible).
Malformed lines are skipped, not fatal.
