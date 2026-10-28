/**
 * skill-memory — Layer-1 runtime learning loop for Pi.
 *
 * Ports Hermes's self-improvement runtime layer (skill_manage + nudges +
 * skills-first memory routing) onto Pi, informed by REA evidence of Pi's
 * shipped bundle (semantic graph: loadSkills/loadSkillsFromDir read-only
 * discovery, skills escape guard, ~/.agents/skills native root):
 *
 *   - Pi only READS skills natively; this extension adds the WRITE side.
 *   - Guardrails mirror Hermes self-evolution constraints: 15KB size cap,
 *     frontmatter contract, atomic all-or-nothing ops, trigger descriptions.
 *   - Routing doctrine adapted from Hermes build_memory_guidance():
 *     skills-first for task knowledge; every-session facts to wiki/memory.
 *
 * Config via env:
 *   PI_SKILL_MANAGE_ROOTS              ':'-separated extra skill roots
 *   PI_SKILL_MANAGE_MAX_SKILL_BYTES    default 15360
 *   PI_SKILL_MANAGE_NUDGE_MIN_TOOLS    default 6 (0 disables trigger floor)
 *   PI_SKILL_MANAGE_DISABLE_NUDGE      =1 to disable the session-end nudge
 *   PI_SKILL_TELEMETRY                  =0 to disable per-skill usage telemetry
 *   PI_SKILL_TELEMETRY_PATH             default ~/.local/share/skill-evolution/skill-usage.jsonl
 *   PI_SKILL_TELEMETRY_MAX_BYTES       default 5242880 (5MB, rotates to .1)
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const HOME = os.homedir();
const PRIMARY_ROOT = path.join(HOME, ".agents", "skills");
const SKILL_ROOTS: string[] = [
	PRIMARY_ROOT,
	...(process.env.PI_SKILL_MANAGE_ROOTS?.split(":").filter(Boolean).map((p) => path.resolve(p)) ?? []),
];
const MAX_SKILL_BYTES = Number(process.env.PI_SKILL_MANAGE_MAX_SKILL_BYTES ?? "15360");
const NUDGE_MIN_TOOL_CALLS = Number(process.env.PI_SKILL_MANAGE_NUDGE_MIN_TOOLS ?? "6");
const NUDGE_ENABLED = process.env.PI_SKILL_MANAGE_DISABLE_NUDGE !== "1";
const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

// ── per-skill usage telemetry (the Layer-1 signal feed for Layer-2 triage) ─
//
// Append-only JSONL. Schema: layer1/telemetry-schema.md (the single contract).
// Never throws — a telemetry failure must not block the tool call it observes.
const TELEMETRY_ENABLED = process.env.PI_SKILL_TELEMETRY !== "0";
const TELEMETRY_PATH =
	process.env.PI_SKILL_TELEMETRY_PATH ?? path.join(HOME, ".local", "share", "skill-evolution", "skill-usage.jsonl");
const TELEMETRY_MAX_BYTES = Number(process.env.PI_SKILL_TELEMETRY_MAX_BYTES ?? "5242880");
const TELEMETRY_SESSION = `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

type TelemetryEvent = {
	event: string;
	skill?: string;
	op?: string;
	tool?: string;
	source?: string;
	reason?: string;
};

function record(ev: TelemetryEvent): void {
	if (!TELEMETRY_ENABLED) return;
	try {
		const dir = path.dirname(TELEMETRY_PATH);
		fs.mkdirSync(dir, { recursive: true });
		try {
			const st = fs.statSync(TELEMETRY_PATH);
			if (st.size > TELEMETRY_MAX_BYTES) fs.renameSync(TELEMETRY_PATH, `${TELEMETRY_PATH}.1`);
		} catch {
			/* no file yet */
		}
		const line = JSON.stringify({ ts: new Date().toISOString(), session: TELEMETRY_SESSION, ...ev }) + "\n";
		fs.appendFileSync(TELEMETRY_PATH, line);
	} catch {
		/* telemetry is best-effort; never block a tool call */
	}
}

/** Skill name from a path that points at a SKILL.md (parent directory name). */
function skillFromSkillMdPath(p: string): string | undefined {
	if (!p) return undefined;
	if (path.basename(p) !== "SKILL.md") return undefined;
	const name = path.basename(path.dirname(p));
	return NAME_RE.test(name) ? name : undefined;
}

/** Skill name from a skill_search load call (name field, or path to a skill dir/SKILL.md). */
function skillFromLoadInput(input: Record<string, unknown>): string | undefined {
	const name = typeof input.name === "string" ? input.name.trim() : "";
	if (name && NAME_RE.test(name)) return name;
	const p = typeof input.path === "string" ? input.path : "";
	if (!p) return undefined;
	return skillFromSkillMdPath(p) ?? (path.basename(p).match(NAME_RE) ? path.basename(p) : undefined);
}

/** Turn a tool call into telemetry events. */
function recordToolUsage(toolName: string, rawInput: unknown): void {
	try {
		const input = (rawInput ?? {}) as Record<string, unknown>;
		if (toolName === "skill_search") {
			const action = typeof input.action === "string" ? input.action : "";
			if (action !== "load") return;
			const skill = skillFromLoadInput(input);
			if (skill) record({ event: "skill_loaded", skill, source: "skill_search" });
			return;
		}
		if (toolName === "read") {
			const p = typeof input.path === "string" ? input.path : "";
			const skill = skillFromSkillMdPath(p);
			if (skill) record({ event: "skill_read", skill, source: "read" });
			return;
		}
		if (toolName === "skill_manage") {
			const ops = Array.isArray(input.operations) ? (input.operations as Record<string, unknown>[]) : [];
			for (const op of ops) {
				const name = typeof op.name === "string" ? op.name : "";
				const action = typeof op.action === "string" ? op.action : "";
				if (!name) continue;
				record({ event: "skill_managed", skill: name, op: action, source: "skill_manage" });
			}
			return;
		}
		if (/^wiki_(retro|observe)$/.test(toolName)) {
			record({ event: "learning_tool", tool: toolName, source: "tool" });
		}
	} catch {
		/* never block a tool call */
	}
}

// ── path safety (mirrors pi's own "escapes the skills dir" contract) ───────

function resolveInsideRoot(name: string, root: string): string | null {
	if (!fs.existsSync(root)) return null;
	if (!NAME_RE.test(name)) return null;
	const realRoot = fs.realpathSync(root);
	const target = path.join(realRoot, name);
	const rel = path.relative(realRoot, target);
	if (rel.startsWith("..") || path.isAbsolute(rel)) return null;
	return target;
}

function findSkill(name: string): { root: string; entry: string; skillDir: string; isSymlink: boolean } | null {
	for (const root of SKILL_ROOTS) {
		if (!NAME_RE.test(name)) continue;
		const entry = path.join(root, name);
		if (!fs.existsSync(entry)) continue;
		const st = fs.lstatSync(entry);
		const isSymlink = st.isSymbolicLink();
		const skillDir = path.join(root, name);
		return { root, entry, skillDir, isSymlink };
	}
	return null;
}

function parseFrontmatter(raw: string): { name?: string; description?: string } {
	if (!raw.trimStart().startsWith("---")) return {};
	const m = raw.match(/^---\r?\n([\s\S]*?)\r?\n---/);
	if (!m) return {};
	const get = (key: string) => {
		const line = m[1].match(new RegExp(`^${key}:\\s*(?:"([^"]*)"|'([^']*)'|(.*))$`, "m"));
		return (line?.[1] ?? line?.[2] ?? line?.[3])?.trim();
	};
	return { name: get("name"), description: get("description") };
}

// ── atomic snapshot / rollback ──────────────────────────────────────────────

type Snap = { kind: "file"; content: string } | { kind: "dir"; entries: string[] } | null;

function snapshotTree(dir: string, into: Map<string, Snap>): void {
	if (!fs.existsSync(dir)) {
		into.set(dir, null);
		return;
	}
	const st = fs.lstatSync(dir);
	if (st.isSymbolicLink()) return; // don't follow or snapshot through symlinked entries
	if (st.isDirectory()) {
		into.set(dir, { kind: "dir", entries: fs.readdirSync(dir) });
		for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
			const fp = path.join(dir, e.name);
			if (e.isDirectory()) snapshotTree(fp, into);
			else if (e.isFile()) into.set(fp, { kind: "file", content: fs.readFileSync(fp, "utf8") });
		}
	} else if (st.isFile()) {
		into.set(dir, { kind: "file", content: fs.readFileSync(dir, "utf8") });
	}
}

function rollback(snapshots: Map<string, Snap>): string[] {
	const problems: string[] = [];
	const paths = [...snapshots.keys()].sort((a, b) => b.length - a.length); // deepest first
	for (const p of paths) {
		const prior = snapshots.get(p)!;
		try {
			if (prior === null) {
				if (fs.existsSync(p)) fs.rmSync(p, { recursive: true, force: true });
			} else if (prior.kind === "file") {
				fs.mkdirSync(path.dirname(p), { recursive: true });
				fs.writeFileSync(p, prior.content);
			} else {
				fs.mkdirSync(p, { recursive: true });
			}
		} catch (e) {
			problems.push(`${p}: ${e instanceof Error ? e.message : String(e)}`);
		}
	}
	return problems;
}

// ── tool schema ─────────────────────────────────────────────────────────────

const SKILL_MANAGE_DESCRIPTION =
	"Create, update, or delete agent skills — your procedural memory for recurring task types. " +
	"Skills live under " +
	PRIMARY_ROOT +
	" (Pi reads them natively at each session start). Call with an operations array applied ATOMICALLY: " +
	"any failure rolls back every touched skill. Ops: create (full SKILL.md with YAML frontmatter; " +
	"frontmatter name must equal the skill name; description required), patch (preferred: targeted " +
	"old_string/new_string edit that must match exactly once; content-only 'create' would replace a whole " +
	"file, so read the skill before rewriting), write_file / remove_file (supporting files), delete (sole op " +
	"only; removes a symlinked deployment link without touching a linked source tree). Discipline: make the " +
	"description's first ~57 chars a self-contained trigger ('Use when <trigger>. <behavior>.'); write " +
	"lessons, not logs — imperative rule + why, one rule per lesson, no dates or PR numbers; extend existing " +
	"references/ files before adding new ones; keep SKILL.md <= 15KB. Use after discovering a non-trivial " +
	"workflow, when a skill you followed was missing or wrong, or to capture a reusable procedure for later " +
	"sessions.";

const SkillManageParams = Type.Object({
	operations: Type.Array(
		Type.Union([
			Type.Object({
				action: StringEnum(["create"] as const),
				name: Type.String({ description: "Skill name: lowercase, hyphens/underscores, max 64 chars" }),
				content: Type.String({ description: "Full SKILL.md text (YAML frontmatter + markdown body)" }),
			}),
			Type.Object({
				action: StringEnum(["patch"] as const),
				name: Type.String(),
				file_path: Type.Optional(Type.String({ description: "Supporting file relative to skill dir; default SKILL.md" })),
				old_string: Type.String({ description: "Exact text to replace (must match once)" }),
				new_string: Type.String({ description: "Replacement; empty string deletes the match" }),
			}),
			Type.Object({
				action: StringEnum(["write_file"] as const),
				name: Type.String(),
				file_path: Type.String({ description: "Relative to the skill dir" }),
				content: Type.String(),
			}),
			Type.Object({
				action: StringEnum(["remove_file"] as const),
				name: Type.String(),
				file_path: Type.String({ description: "Relative to the skill dir" }),
			}),
			Type.Object({ action: StringEnum(["delete"] as const), name: Type.String() }),
		]),
		{ description: "Ordered ops, applied atomically as one unit" },
	),
});

interface OpLog {
	action: string;
	name: string;
	target: string;
	warning?: string;
}

export default function skillMemoryExtension(pi: ExtensionAPI) {
	// ── session-scoped nudge state ────────────────────────────────────────
	let toolCallCount = 0;
	let learningToolUsed = false;
	let nudgeDelivered = false;

	pi.on("session_start", async (event) => {
		toolCallCount = 0;
		learningToolUsed = false;
		nudgeDelivered = false;
		record({ event: "session_start", reason: String((event as { reason?: string })?.reason ?? "") });
	});

	pi.on("tool_call", (event) => {
		toolCallCount++;
		const name = String((event as { toolName?: string; tool?: string }).toolName ?? (event as { tool?: string }).tool ?? "");
		if (/skill_manage|wiki_retro|wiki_observe/i.test(name)) learningToolUsed = true;
		recordToolUsage(name, (event as { input?: unknown }).input);
	});

	pi.on("tool_result", (event) => {
		const name = String((event as { toolName?: string }).toolName ?? "");
		if (name !== "skill_manage") return;
		const details = (event as { details?: { ok?: boolean; error?: string } }).details;
		if (!details || details.ok !== false) return;
		const m = /skill '([^']+)'/.exec(details.error ?? "");
		record({ event: "skill_failed", skill: m?.[1], op: "unknown", source: "skill_manage" });
	});

	// ── prompt guidance section (Hermes SKILLS_GUIDANCE analog) ───────────
	pi.on("before_agent_start", (event) => {
		const sections = event.systemPromptOptions?.sections;
		if (!sections) return;
		sections.skill_memory = [
			"When you work out a non-trivial workflow, record it with skill_manage for future reuse: it writes",
			"SKILL.md files under " + PRIMARY_ROOT + ", which Pi loads natively at every session start.",
			"If a skill you loaded was missing steps or wrong, patch it (skill_manage action='patch') instead of",
			"re-explaining the fix each session. Routing: task-specific knowledge — procedures, pitfalls, and the",
			"user's corrections for that kind of work — belongs in skills; facts that apply to EVERY session",
			"(who the user is, environment facts, standing conventions) belong in durable memory or the wiki",
			"(wiki_retro when llm-wiki is available). Write entries as declarative facts, not imperatives.",
			"After difficult or iterative tasks, offer to save the approach as a skill.",
		].join("\n");
	});

	// ── one-time session-end learning nudge (loop-guarded) ────────────────
	pi.on("agent_before_settle", async (_event, ctx) => {
		if (!NUDGE_ENABLED || nudgeDelivered || learningToolUsed) return;
		if (NUDGE_MIN_TOOL_CALLS > 0 && toolCallCount < NUDGE_MIN_TOOL_CALLS) return;
		nudgeDelivered = true;
		pi.sendMessage(
			{
				customType: "skill-memory-nudge",
				content:
					"[skill-memory nudge] This session involved substantial multi-step work and used no learning " +
					"tools yet. Before finishing: if any workflow discovered here will recur, persist it — create or " +
					"patch a skill with skill_manage (procedures, pitfalls, user corrections), or skip if it is purely " +
					"session-bound. Either save what is worth saving via skill_manage, or state plainly that nothing " +
					"reusable emerged. Do not restate the transcript.",
				display: false,
			},
			{ triggerTurn: true },
		);
		if (ctx.hasUI) ctx.ui.notify("skill-memory: learning nudge delivered", "info");
	});

	// ── skill_manage tool ─────────────────────────────────────────────────
	pi.registerTool({
		name: "skill_manage",
		label: "Skill Manage",
		description: SKILL_MANAGE_DESCRIPTION,
		parameters: SkillManageParams,
		async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
			const logs: OpLog[] = [];
			const snapshots = new Map<string, Snap>();
			try {
				// ── 1. Pre-flight: resolve every target, snapshot all affected trees
				const touchedRoots = new Set<string>();
				const planned: { op: (typeof params.operations)[number]; target: string; skillDir: string; root: string }[] = [];
				for (const op of params.operations) {
					const found = findSkill(op.name);
					if (op.action === "create") {
						if (found) throw new Error(`create: skill '${op.name}' already exists at ${found.entry}`);
						const dir = path.join(PRIMARY_ROOT, op.name);
						if (!NAME_RE.test(op.name)) throw new Error(`create: invalid skill name '${op.name}'`);
						fs.mkdirSync(PRIMARY_ROOT, { recursive: true });
						planned.push({ op, target: dir, skillDir: dir, root: PRIMARY_ROOT });
					} else {
						if (!found) {
							throw new Error(`${op.action}: skill '${op.name}' not found in: ${SKILL_ROOTS.join(", ")}`);
						}
						touchedRoots.add(found.root);
						if (op.action === "delete") {
							planned.push({ op, target: found.entry, skillDir: found.skillDir, root: found.root });
							continue;
						}
						let target: string;
						if (op.action === "patch" || op.action === "write_file" || op.action === "remove_file") {
							const rel = op.file_path ?? "SKILL.md";
							const base = fs.realpathSync(found.skillDir); // follows deploy symlinks into the source tree
							const fpath = path.resolve(base, rel);
							if (!fpath.startsWith(base + path.sep) && fpath !== base)
								throw new Error(`${op.action}: file_path '${op.file_path}' escapes the skill dir`);
							target = fpath;
						} else {
							target = found.skillDir;
						}
						planned.push({ op, target, skillDir: found.skillDir, root: found.root });
					}
				}
				for (const p of planned) {
					const rootEntry = path.join(p.root, p.op.name);
					snapshotTree(rootEntry, snapshots);
				}

				// ── 2. Apply ops in order ────────────────────────────────────
				try {
					for (const { op, target, root } of planned) {
						switch (op.action) {
							case "create": {
								const fm = parseFrontmatter(op.content);
								if (!fm.name) throw new Error("create: content must include YAML frontmatter with a name field");
								if (fm.name.replace(/^["']|["']$/g, "") !== op.name)
									throw new Error(`create: frontmatter name '${fm.name}' doesn't match target name '${op.name}'`);
								if (!fm.description) throw new Error("create: frontmatter description is required");
								const bytes = Buffer.byteLength(op.content, "utf8");
								if (bytes > MAX_SKILL_BYTES)
									throw new Error(`create: SKILL.md is ${bytes} bytes (limit ${MAX_SKILL_BYTES}); move detail into references/`);
								fs.mkdirSync(target, { recursive: true });
								fs.writeFileSync(path.join(target, "SKILL.md"), op.content);
								logs.push({
									action: "create",
									name: op.name,
									target,
									warning:
										fm.description.length <= 57
											? undefined
											: "description is long; make its first ~57 chars a self-contained trigger",
								});
								break;
							}
							case "patch": {
								if (!fs.existsSync(target) || !fs.statSync(target).isFile())
									throw new Error(`patch: ${target} does not exist`);
								const prior = fs.readFileSync(target, "utf8");
								const n = op.old_string.length ? prior.split(op.old_string).length - 1 : 0;
								if (n === 0) throw new Error(`patch: old_string not found in ${target}`);
								if (n > 1) throw new Error(`patch: old_string matches ${n} times in ${target}; it must be unique`);
								const next = prior.replace(op.old_string, op.new_string);
								const bytes = Buffer.byteLength(next, "utf8");
								if (bytes > MAX_SKILL_BYTES)
									throw new Error(`patch: result would be ${bytes} bytes (limit ${MAX_SKILL_BYTES}); prune or split into references/`);
								fs.writeFileSync(target, next);
								logs.push({
									action: "patch",
									name: op.name,
									target,
									warning:
										next.length / Math.max(1, prior.length) > 1.05
											? "file grew >5%; watch for bloat drift"
											: undefined,
								});
								break;
							}
							case "write_file": {
								const bytes = Buffer.byteLength(op.content, "utf8");
								if (bytes > MAX_SKILL_BYTES)
									throw new Error(`write_file: ${target} would be ${bytes} bytes (limit ${MAX_SKILL_BYTES})`);
								fs.mkdirSync(path.dirname(target), { recursive: true });
								fs.writeFileSync(target, op.content);
								logs.push({ action: "write_file", name: op.name, target });
								break;
							}
							case "remove_file": {
								if (!fs.existsSync(target)) throw new Error(`remove_file: ${target} does not exist`);
								if (fs.statSync(target).isDirectory()) throw new Error(`remove_file: ${target} is a directory`);
								fs.rmSync(target);
								logs.push({ action: "remove_file", name: op.name, target });
								break;
							}
							case "delete": {
								const found = findSkill(op.name);
								if (!found) throw new Error(`delete: skill '${op.name}' not found`);
								if (found.isSymlink) {
									fs.unlinkSync(found.entry); // deployment symlink: preserve the linked source tree
									logs.push({
										action: "delete",
										name: op.name,
										target: found.entry,
										warning: "was a deployment symlink; only the link was removed, the source tree remains at the link target",
									});
								} else {
									fs.rmSync(found.skillDir, { recursive: true, force: true });
									logs.push({ action: "delete", name: op.name, target: found.skillDir });
								}
								break;
							}
						}
					}
				} catch (err) {
					const problems = rollback(snapshots);
					throw err instanceof Error
						? new Error(`${err.message}${problems.length ? ` (ROLLBACK ISSUES: ${problems.join("; ")})` : ""}`)
						: err;
				}
				const warnings = logs.filter((l) => l.warning).map((l) => `${l.name}: ${l.warning}`);
				return {
					content: [
						{
							type: "text",
							text:
								`skill_manage applied ${logs.length} op(s) atomically:\n` +
								logs.map((l) => `- ${l.action}: ${l.name} → ${l.target}`).join("\n") +
								(warnings.length ? `\nWarnings:\n- ${warnings.join("\n- ")}` : "") +
								`\nNew/changed skill content is visible to Pi on the next session start (or /reload).`,
						},
					],
					details: { ok: true, logs, bytesLimit: MAX_SKILL_BYTES },
				};
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				return {
					content: [{ type: "text", text: `skill_manage FAILED (nothing applied, atomic rollback): ${msg}` }],
					details: { ok: false, error: msg, opsPlanned: params.operations.length, logs },
				};
			}
		},
	});
}