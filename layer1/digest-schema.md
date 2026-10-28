# Session-end digest format

The Layer-1 extension runs the Layer-2 monitor when a session ends and
appends a digest section. This file is the contract for that artifact.

## Where and when

| Property | Value |
| --- | --- |
| Directory | `~/.local/share/skill-evolution/digests/` (override: `PI_SKILL_TRIAGE_DIGEST_DIR`) |
| Day file | `digests/YYYY-MM-DD.md` (UTC date), sections appended in order |
| Latest | `digests/latest.md` — a copy of the newest section |
| State | `digests/state.json` — `{lastTs, top: [[skill, action], ...]}` for the delta |
| Trigger | `session_shutdown` in the extension |
| Skipped | `reason: "reload"`; also skipped when the monitor subprocess fails |
| Pruning | `.md` files older than `PI_SKILL_TRIAGE_DIGEST_KEEP_DAYS` (30) are removed; `latest.md` and `state.json` are kept |
| Cost | zero — the monitor (`evolution/monitor/triage.py`) is stdlib-only and makes no model calls |

The directory lives **outside** the skill tree: digests are runtime data, not
a deliverable, and must never be committed.

## Section format

```markdown
## 2026-10-28 03:28:06Z — session s-mvrk0tfa-83subc (shutdown: quit)

- this session: 2 skill event(s) (1 loaded, 1 read), 1 learning call(s)
- top action: **measure** → `ncal` (priority 0.90)
- Δ vs last digest: unchanged since last digest

| # | Skill | Usage | Potential | Priority | Action | Why |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `ncal` | 1.00 | 0.90 | 0.90 | **measure** | used but has no golden eval set — build … |
```

| Line | Meaning |
| --- | --- |
| header | UTC timestamp, the extension's session id, the `session_shutdown` reason |
| `this session` | skill events recorded for **this** session id (loaded / read) + learning-tool calls |
| `top action` | the highest-priority row: action, skill, priority |
| `Δ vs last digest` | `first digest` · `unchanged since last digest` · a per-skill change list (`\`x\` review → measure`) · `rank reshuffled` |
| table | the top `PI_SKILL_TRIAGE_TOP` (10) rows from the monitor, ranked by `usage × potential` |

## Invariants

1. **One section per session.** `session_shutdown` can fire more than once for
   the same session (observed in a `pi --print` run that appended its section
   twice) — the extension sets a per-session flag before doing the work.
2. **Read-only.** The digest never runs the optimizer, never merges a branch,
   and never spends model tokens.
3. **Never fatal.** Every step is wrapped; a digest failure must not break
   shutdown.
4. **Bounded.** The subprocess has a hard timeout
   (`PI_SKILL_TRIAGE_TIMEOUT_MS`, 15 s) and the digest directory is pruned.
