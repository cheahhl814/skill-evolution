# Layer 1 — global routing section (AGENTS.md §7 companion)

The Layer-1 extension (`extension/skill-memory.ts`) injects its own
`skill_memory` prompt section at `before_agent_start`, so Pi sessions get the
routing doctrine even when no global `AGENTS.md` is present. The text below
is the **global-file counterpart**: it is the section that lives in
`~/.pi/agent/AGENTS.md` as `## 7. Skills-first memory routing`.

**Sync rule:** this file and `~/.pi/agent/AGENTS.md` §7 must say the same
thing. When you change one, change the other in the same session — the
extension's injected text (the `before_agent_start` handler in
`extension/skill-memory.ts`) is a third copy and must change with them.

## The section (verbatim copy of `~/.pi/agent/AGENTS.md` §7)

```markdown
## 7. Skills-first memory routing (skill-memory extension)

Task knowledge has a home; global facts have a different one:

- **Skills** (`skill_manage` tool): procedures, workflows, pitfalls, and the
  user's corrections *for that kind of work*. Prefer **patching an existing
  skill** over creating one: a skill that is never used again is dead weight
  in every later session. Before any skill write, two things must hold — the
  **accumulated-experience bar** (the skill was used across several sessions
  AND shows a real deficiency: revisited repeatedly, a failed write, or
  already proposed twice) and the **user's approval**. The tool enforces
  both, so a write that fails the bar is refused: do not retry, and do not
  work around it with a plain edit of the SKILL.md — a direct edit of a
  deployed skill is gated the same way. Skills land in `~/.agents/skills/`
  and Pi loads them natively at every session start. Discipline: lessons, not
  logs (imperative rule + why, one rule per lesson, no dates/PR numbers);
  trigger-style description (first ~57 chars self-contained); SKILL.md ≤
  15KB, move detail into `references/`. A new skill must follow the
  skill-forge convention (frontmatter with name/description/version/triggers,
  a README.md, and `skill-forge` in the frontmatter `skills:` list).
- **Wiki** (`wiki_retro` / `wiki_observe`): facts that apply to EVERY
  session regardless of task — who the user is, environment facts, standing
  conventions, project-level insights. This is the **default sink**: a
  one-session observation goes here, not into a skill.
- Never write imperatives into memory/profile stores ("Always respond
  concisely" ✗) — they get re-read as directives; write declarative facts
  ("User prefers concise responses" ✓).
- A fact stale within a week belongs in session history; after difficult
  or iterative tasks, offer to save the approach as a skill (ask before
  creating a new one — and only when the accumulated-experience bar is met).
```

## Where the Layer-1 code lives

| Piece | Path |
| --- | --- |
| Extension source (this repo, tracked) | `layer1/extension/skill-memory.ts` |
| Live deployment (symlink) | `~/.pi/agent/extensions/skill-memory.ts` → `layer1/extension/skill-memory.ts` |
| Global instruction section | `~/.pi/agent/AGENTS.md` §7 (copy in this file) |
| Skills the tool writes to | `~/.agents/skills/` (extra roots: `PI_SKILL_MANAGE_ROOTS`) |
