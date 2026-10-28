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
 *   - A NEW skill is the only unbounded growth in this loop, so `create` is
 *     gated twice: the user approves it, and the candidate must pass
 *     skill-forge's structural linter (see "Layer-1 write policy" below).
 *     `patch` / `write_file` / `remove_file` are not gated.
 *
 * Config via env:
 *   PI_SKILL_MANAGE_ROOTS              ':'-separated extra skill roots
 *   PI_SKILL_MANAGE_MAX_SKILL_BYTES    default 15360
 *   PI_SKILL_MANAGE_ALLOW_CREATE       =1 to skip the user-approval prompt for
 *                                        `create` (unattended runs only; the
 *                                        skill-forge lint gate still applies)
 *   PI_SKILL_MANAGE_LINT_TIMEOUT_MS    default 20000 (skill-forge lint budget)
 *   PI_SKILL_MANAGE_NUDGE_MIN_TOOLS    default 6 (0 disables trigger floor)
 *   PI_SKILL_MANAGE_DISABLE_NUDGE      =1 to disable the session-end nudge
 *   PI_SKILL_TELEMETRY                  =0 to disable per-skill usage telemetry
 *   PI_SKILL_TELEMETRY_PATH             default ~/.local/share/skill-evolution/skill-usage.jsonl
 *   PI_SKILL_TELEMETRY_MAX_BYTES       default 5242880 (5MB, rotates to .1)
 *   PI_SKILL_TRIAGE_ON_SHUTDOWN         =0 to skip the session-end triage digest
 *   PI_SKILL_EVOLUTION_ROOT             default ~/.agents/skills/skill-evolution
 *   PI_SKILL_TRIAGE_DIGEST_DIR          default ~/.local/share/skill-evolution/digests
 *   PI_SKILL_TRIAGE_TIMEOUT_MS          default 15000
 *   PI_SKILL_TRIAGE_TOP                 default 10 (rows in the digest table)
 *   PI_SKILL_TRIAGE_DIGEST_KEEP_DAYS    default 30 (older digest files are pruned)
 *   PI_SKILL_SUGGEST                     =0 to stop injecting the ranked target into the prompt
 *   PI_SKILL_SUGGEST_MAX_AGE_DAYS        default 7 (a suggestion older than this is not injected)
 *   PI_SKILL_SUGGEST_REFRESH_MIN         default 30 (min minutes between suggestion refreshes)
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
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

// ── session-end triage digest ──────────────────────────────────────────────
//
// On session_shutdown the extension runs the Layer-2 monitor (`bin/triage`,
// stdlib-only: no venv required) and appends a dated digest section listing
// which skills are worth evolving. Read-only: triage never runs the optimizer,
// never merges, never spends tokens. Skipped on `reason: "reload"` so a
// /reload does not spam the digest directory.

const TRIAGE_ON_SHUTDOWN = process.env.PI_SKILL_TRIAGE_ON_SHUTDOWN !== "0";
const TRIAGE_TIMEOUT_MS = Number(process.env.PI_SKILL_TRIAGE_TIMEOUT_MS ?? "15000");
const TRIAGE_TOP = Number(process.env.PI_SKILL_TRIAGE_TOP ?? "10");
const DIGEST_DIR =
	process.env.PI_SKILL_TRIAGE_DIGEST_DIR ?? path.join(HOME, ".local", "share", "skill-evolution", "digests");
const DIGEST_KEEP_DAYS = Number(process.env.PI_SKILL_TRIAGE_DIGEST_KEEP_DAYS ?? "30");

type TriageRow = {
	skill: string;
	usage: number;
	potential: number;
	priority: number;
	action: string;
	reason: string;
};

/** Locate the skill-evolution checkout (deployed symlink, or an explicit override). */
function resolveEvolutionRoot(): string | null {
	const candidates = [
		process.env.PI_SKILL_EVOLUTION_ROOT,
		path.join(HOME, ".agents", "skills", "skill-evolution"),
	].filter((c): c is string => Boolean(c));
	for (const c of candidates) {
		try {
			const root = fs.realpathSync(c);
			if (fs.existsSync(path.join(root, "evolution", "monitor", "triage.py"))) return root;
		} catch {
			/* candidate does not exist */
		}
	}
	return null;
}

/** Run `python -m evolution.monitor.triage --json`. Returns null on any failure. */
function runTriage(root: string): TriageRow[] | null {
	const venvPython = path.join(root, ".venv", "bin", "python");
	const python = fs.existsSync(venvPython) ? venvPython : "python3";
	try {
		const res = spawnSync(
			python,
			["-m", "evolution.monitor.triage", "--json"],
			{
				cwd: root,
				env: { ...process.env, PYTHONPATH: root },
				encoding: "utf8",
				timeout: TRIAGE_TIMEOUT_MS,
				maxBuffer: 8 * 1024 * 1024,
			},
		);
		if (res.status !== 0 || !res.stdout) return null;
		const parsed = JSON.parse(res.stdout);
		return Array.isArray(parsed) ? (parsed as TriageRow[]) : null;
	} catch {
		return null;
	}
}

/** Event counts for THIS session, read back from the telemetry file. */
function sessionEventCounts(): { skills: number; loaded: number; read: number; learning: number } {
	const counts = { skills: 0, loaded: 0, read: 0, learning: 0 };
	try {
		if (!fs.existsSync(TELEMETRY_PATH)) return counts;
		const lines = fs.readFileSync(TELEMETRY_PATH, "utf8").split("\n");
		for (const line of lines) {
			const t = line.trim();
			if (!t) continue;
			let obj: { session?: string; event?: string; skill?: string };
			try {
				obj = JSON.parse(t);
			} catch {
				continue;
			}
			if (obj.session !== TELEMETRY_SESSION) continue;
			if (obj.event === "skill_loaded") {
				counts.loaded++;
				counts.skills++;
			} else if (obj.event === "skill_read") {
				counts.read++;
				counts.skills++;
			} else if (obj.event === "learning_tool") {
				counts.learning++;
			}
		}
	} catch {
		/* telemetry is best-effort */
	}
	return counts;
}

type DigestState = { lastTs: string; top: [string, string][] };

function readDigestState(): DigestState | null {
	try {
		const p = path.join(DIGEST_DIR, "state.json");
		if (!fs.existsSync(p)) return null;
		return JSON.parse(fs.readFileSync(p, "utf8")) as DigestState;
	} catch {
		return null;
	}
}

function writeDigestState(state: DigestState): void {
	try {
		fs.mkdirSync(DIGEST_DIR, { recursive: true });
		fs.writeFileSync(path.join(DIGEST_DIR, "state.json"), JSON.stringify(state, null, 2));
	} catch {
		/* best effort */
	}
}

/** Append the digest section, write latest.md, prune old digests. */
function writeDigest(rows: TriageRow[], counts: { skills: number; loaded: number; read: number; learning: number }, reason: string): void {
	try {
		const now = new Date();
		const stamp = now.toISOString().replace("T", " ").slice(0, 19) + "Z";
		const day = now.toISOString().slice(0, 10);
		const top = rows.slice(0, TRIAGE_TOP);
		const prev = readDigestState();
		const curTop: [string, string][] = rows.slice(0, 5).map((r) => [r.skill, r.action]);
		let delta = "first digest";
		if (prev) {
			const same =
				prev.top.length === curTop.length &&
				prev.top.every(([s, a], i) => s === curTop[i][0] && a === curTop[i][1]);
			if (same) delta = "unchanged since last digest";
			else {
				const changes: string[] = [];
				for (const [s, a] of curTop) {
					const before = prev.top.find(([ps]) => ps === s);
					if (before && before[1] !== a) changes.push(`\`${s}\` ${before[1]} → ${a}`);
					else if (!before) changes.push(`\`${s}\` new → ${a}`);
				}
				delta = changes.length ? changes.join(", ") : "rank reshuffled";
			}
		}
		const leader = top[0];
		const lines: string[] = [];
		lines.push(`## ${stamp} — session ${TELEMETRY_SESSION} (shutdown: ${reason})`);
		lines.push("");
		lines.push(
			`- this session: ${counts.skills} skill event(s) (${counts.loaded} loaded, ${counts.read} read), ${counts.learning} learning call(s)`,
		);
		lines.push(
			leader
				? `- top action: **${leader.action}** → \`${leader.skill}\` (priority ${leader.priority.toFixed(2)})`
				: "- top action: none — no skills found",
		);
		lines.push(`- Δ vs last digest: ${delta}`);
		lines.push("");
		lines.push("| # | Skill | Usage | Potential | Priority | Action | Why |");
		lines.push("| --- | --- | --- | --- | --- | --- | --- |");
		top.forEach((r, i) => {
			lines.push(
				`| ${i + 1} | \`${r.skill}\` | ${r.usage.toFixed(2)} | ${r.potential.toFixed(2)} | ${r.priority.toFixed(2)} | **${r.action}** | ${r.reason} |`,
			);
		});
		lines.push("");
		const section = lines.join("\n");

		fs.mkdirSync(DIGEST_DIR, { recursive: true });
		const dayFile = path.join(DIGEST_DIR, `${day}.md`);
		const header = fs.existsSync(dayFile) ? "" : `# Skill triage digests — ${day}\n\n`;
		fs.appendFileSync(dayFile, `${header}${section}\n`);
		fs.writeFileSync(path.join(DIGEST_DIR, "latest.md"), `${section}\n`);
		writeDigestState({ lastTs: now.toISOString(), top: curTop });

		// prune old digest files
		if (DIGEST_KEEP_DAYS > 0) {
			const cutoff = Date.now() - DIGEST_KEEP_DAYS * 86400000;
			for (const f of fs.readdirSync(DIGEST_DIR)) {
				if (!f.endsWith(".md") || f === "latest.md") continue;
				const fp = path.join(DIGEST_DIR, f);
				try {
					if (fs.statSync(fp).mtimeMs < cutoff) fs.rmSync(fp);
				} catch {
					/* best effort */
				}
			}
		}
	} catch {
		/* a digest failure must never break shutdown */
	}
}


// ── suggestion: the ranked target, injected into the prompt ────────────────
//
// The extension suggests; the agent acts. The suggestion is computed ONCE per
// session (a stdlib-only subprocess, no model cost) and injected into the
// `skill_memory` prompt section, so the agent knows which skill the triage
// digest ranks next. It never spends anything by itself: the skill's own
// dry-run + ask-user cost gate still governs any real run.

const SUGGEST_ENABLED = process.env.PI_SKILL_SUGGEST !== "0";
const SUGGEST_MAX_AGE_MS = Number(process.env.PI_SKILL_SUGGEST_MAX_AGE_DAYS ?? "7") * 86400000;
const SUGGEST_REFRESH_MS = Number(process.env.PI_SKILL_SUGGEST_REFRESH_MIN ?? "30") * 60000;

type Suggestion = {
	skill: string;
	action: string;
	priority: number;
	usage: number;
	reason: string;
};

/** Run the monitor's suggestion mode. Returns null on any failure. */
function runSuggest(root: string): Suggestion | null {
	const venvPython = path.join(root, ".venv", "bin", "python");
	const python = fs.existsSync(venvPython) ? venvPython : "python3";
	try {
		const res = spawnSync(python, ["-m", "evolution.monitor.triage", "--suggest"], {
			cwd: root,
			env: { ...process.env, PYTHONPATH: root },
			encoding: "utf8",
			timeout: TRIAGE_TIMEOUT_MS,
			maxBuffer: 1024 * 1024,
		});
		if (res.status !== 0 || !res.stdout) return null;
		const parsed = JSON.parse(res.stdout.trim());
		if (!parsed || typeof parsed.skill !== "string") return null;
		return parsed as Suggestion;
	} catch {
		return null;
	}
}

let SUGGESTION: Suggestion | null = null;
let SUGGESTION_TS = 0;

/** Refresh the cached suggestion (at most once per refresh window). */
function refreshSuggestion(): void {
	if (!SUGGEST_ENABLED) return;
	const now = Date.now();
	if (SUGGESTION_TS && now - SUGGESTION_TS < SUGGEST_REFRESH_MS) return;
	const root = resolveEvolutionRoot();
	if (!root) return;
	const s = runSuggest(root);
	SUGGESTION = s;
	SUGGESTION_TS = now;
	if (s) record({ event: "suggestion", skill: s.skill, op: s.action, source: "triage" });
}

/** The prompt lines for the current suggestion (empty when there is none). */
function suggestionLines(): string[] {
	if (!SUGGEST_ENABLED || !SUGGESTION) return [];
	if (Date.now() - SUGGESTION_TS > SUGGEST_MAX_AGE_MS) return [];
	const s = SUGGESTION;
	return [
		"",
		"Skill self-improvement: the triage digest ranks `" +
			s.skill +
			"` as the next target (action: " +
			s.action +
			", priority " +
			s.priority.toFixed(2) +
			"). " +
			s.reason +
			".",
		"Act on it only when the user asks to improve a skill; run `bin/evolve --dry-run` first and keep the",
		"ask-user cost gate before any model spend. Never push: the candidate is a local branch for human review.",
	];
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

// ── Layer-1 write policy: a new skill needs approval + skill-forge conformance ─
//
// A new skill is the only unbounded growth in this loop. Pi loads every skill
// at every session start, so a skill that is never used again is dead weight
// forever, and the clutter cannot be undone without deleting files by hand.
// Two gates protect the live tree, and both live in the TOOL rather than in
// the agent's judgement:
//
//   1. Approval    — `create` asks the user (ctx.ui.confirm) before a single
//                    byte is written. A session with no UI fails CLOSED; the
//                    unattended opt-out is the explicit env var
//                    PI_SKILL_MANAGE_ALLOW_CREATE=1.
//   2. Conformance — the candidate is staged in a temp dir and must pass
//                    skill-forge's structural linter (bin/skill-lint.py,
//                    tool-skill profile) before it reaches ~/.agents/skills.
//                    skill-forge is the external dependency that owns the
//                    contract: frontmatter (name, description, version,
//                    triggers), a README.md public face, and a
//                    `skills: [skill-forge]` dependency declaration.
//
// `patch` / `write_file` / `remove_file` are NOT gated: correcting a skill
// that already exists cannot pile up new ones.

const ALLOW_CREATE = process.env.PI_SKILL_MANAGE_ALLOW_CREATE === "1";
const LINT_TIMEOUT_MS = Number(process.env.PI_SKILL_MANAGE_LINT_TIMEOUT_MS ?? "20000");
const FORGE_SKILL = "skill-forge";
const LINT_PROFILE = "tool-skill";

/** Locate skill-forge's structural linter across the skill roots. */
function findSkillForgeLint(): string | null {
	for (const root of SKILL_ROOTS) {
		const p = path.join(root, FORGE_SKILL, "bin", "skill-lint.py");
		if (fs.existsSync(p)) return p;
	}
	return null;
}

type GateFrontmatter = {
	name?: string;
	description?: string;
	version?: string;
	skills: string[];
	triggers: string[];
};

/**
 * Frontmatter fields the create gate needs. The linter does the real YAML
 * parse; this is a deliberately small reader for the four keys the policy
 * depends on, tolerant of both block lists and inline arrays.
 */
function parseGateFrontmatter(raw: string): GateFrontmatter {
	const out: GateFrontmatter = { skills: [], triggers: [] };
	if (!raw.trimStart().startsWith("---")) return out;
	const m = raw.match(/^---\r?\n([\s\S]*?)\r?\n---/);
	if (!m) return out;
	const block = m[1];
	const scalar = (key: string): string | undefined => {
		const line = block.match(new RegExp(`^${key}:\\s*(?:"([^"]*)"|'([^']*)'|(.*))$`, "m"));
		return (line?.[1] ?? line?.[2] ?? line?.[3])?.trim();
	};
	out.name = scalar("name");
	out.description = scalar("description");
	out.version = scalar("version");
	const list = (key: string): string[] => {
		const inline = block.match(new RegExp(`^${key}:\\s*\\[(.*)\\]$`, "m"));
		if (inline) {
			return inline[1]
				.split(",")
				.map((s) => s.trim().replace(/^["']|["']$/g, ""))
				.filter(Boolean);
		}
		const lines = block.split(/\r?\n/);
		const start = lines.findIndex((l) => new RegExp(`^${key}:\\s*$`).test(l));
		if (start < 0) return [];
		const items: string[] = [];
		for (let i = start + 1; i < lines.length; i++) {
			const item = lines[i].match(/^\s*-\s*(.+)$/);
			if (!item) break;
			items.push(item[1].trim().replace(/^["']|["']$/g, ""));
		}
		return items;
	};
	out.skills = list("skills");
	out.triggers = list("triggers");
	return out;
}

type CreatePlan = {
	name: string;
	content: string;
	supporting: { file_path: string; content: string }[];
};

/**
 * The `create` ops of a batch, each with the `write_file` ops that follow it
 * for the same skill (a README.md lives there). Ops are matched in order, so
 * a write_file only attaches to a create that already appeared.
 */
function collectCreatePlans(ops: { action: string; name: string; content?: string; file_path?: string }[]): CreatePlan[] {
	const plans: CreatePlan[] = [];
	for (const op of ops) {
		if (op.action === "create") {
			plans.push({ name: op.name, content: op.content ?? "", supporting: [] });
		} else if (op.action === "write_file") {
			const plan = plans.find((p) => p.name === op.name);
			if (plan && op.file_path && typeof op.content === "string") {
				plan.supporting.push({ file_path: op.file_path, content: op.content });
			}
		}
	}
	return plans;
}

type GateCtx = { hasUI?: boolean; ui?: { confirm?: (title: string, message: string) => Promise<boolean> } };

/**
 * The two create gates. Throws with an actionable message; the caller turns
 * that into the atomic "nothing was written" failure.
 */
async function gateCreates(ctx: GateCtx, plans: CreatePlan[]): Promise<void> {
	if (!plans.length) return;

	// ── gate 1: the user approves every new skill ──────────────────────
	if (!ALLOW_CREATE) {
		if (!ctx?.hasUI || typeof ctx.ui?.confirm !== "function") {
			throw new Error(
				"create: a new skill needs the user's approval and this session has no UI to ask " +
					"(print / RPC-less mode). Run the session interactively, or set " +
					"PI_SKILL_MANAGE_ALLOW_CREATE=1 to allow unattended creation.",
			);
		}
		const lines = plans.map((p) => {
			const fm = parseGateFrontmatter(p.content);
			const desc = (fm.description ?? "(no description)").replace(/\s+/g, " ").slice(0, 160);
			return `• ${p.name} — ${desc}\n  ${Buffer.byteLength(p.content, "utf8")} B of SKILL.md`;
		});
		const approved = await ctx.ui.confirm(
			`Create ${plans.length} new skill${plans.length > 1 ? "s" : ""}?`,
			[
				"Pi loads every skill at each session start, so a skill that is never used again is dead weight forever.",
				"A new skill must follow the skill-forge convention: frontmatter (name, description, version, triggers),",
				"a README.md, and `skill-forge` in the frontmatter `skills:` list — the tool lints it before it lands.",
				"",
				...lines,
				"",
				"Approve?",
			].join("\n"),
		);
		if (!approved) throw new Error("create: declined by the user — nothing was written.");
	}

	// ── gate 2: skill-forge conformance, staged before any live write ──
	const lint = findSkillForgeLint();
	if (!lint) {
		throw new Error(
			`create: skill-forge's linter was not found (looked for ${FORGE_SKILL}/bin/skill-lint.py in ` +
				`${SKILL_ROOTS.join(", ")}). A new skill must follow the skill-forge convention, so the gate ` +
				"cannot be skipped.",
		);
	}
	const stageRoot = fs.mkdtempSync(path.join(os.tmpdir(), "skill-manage-stage-"));
	try {
		for (const plan of plans) {
			const fm = parseGateFrontmatter(plan.content);
			const problems: string[] = [];
			if (fm.name !== plan.name) {
				problems.push(`frontmatter name is "${fm.name ?? "(none)"}" but the skill is named "${plan.name}"`);
			}
			if (!fm.version) problems.push("frontmatter has no `version` field (skill-forge requires one)");
			if (!fm.triggers.length) problems.push("frontmatter has no `triggers` list (skill-forge requires one)");
			if (!fm.skills.includes(FORGE_SKILL)) {
				problems.push(`frontmatter must declare the external dependency — add:\n      skills:\n        - ${FORGE_SKILL}`);
			}
			if (!plan.supporting.some((f) => f.file_path.replace(/^\.\//, "") === "README.md")) {
				problems.push("a new skill must ship a README.md — add a write_file op with file_path 'README.md' in the same batch");
			}
			if (problems.length) {
				throw new Error(
					`create: '${plan.name}' does not follow the skill-forge convention:\n  - ${problems.join("\n  - ")}`,
				);
			}

			// stage the candidate tree
			const stageDir = path.join(stageRoot, plan.name);
			fs.mkdirSync(stageDir, { recursive: true });
			fs.writeFileSync(path.join(stageDir, "SKILL.md"), plan.content);
			for (const f of plan.supporting) {
				const rel = f.file_path.replace(/^\.\//, "");
				const dest = path.resolve(stageDir, rel);
				if (!dest.startsWith(stageDir + path.sep)) {
					throw new Error(`create: write_file path '${f.file_path}' escapes the skill dir`);
				}
				fs.mkdirSync(path.dirname(dest), { recursive: true });
				fs.writeFileSync(dest, f.content);
			}

			// skill-forge's own linter decides — this extension never second-guesses it.
			// --skill-dirs makes dependency resolution (C12) use the SAME roots this
			// tool writes to, so a `skills:` entry must resolve where it will live.
			const res = spawnSync(
				"python3",
				[lint, stageDir, "--profile", LINT_PROFILE, "--include-vendored", "--skill-dirs", SKILL_ROOTS.join(",")],
				{
					encoding: "utf8",
					timeout: LINT_TIMEOUT_MS,
					maxBuffer: 4 * 1024 * 1024,
				},
			);
			if (res.error) {
				throw new Error(`create: could not run the skill-forge linter: ${res.error.message}`);
			}
			if (res.status !== 0) {
				const fails = (res.stdout ?? "")
					.split(/\r?\n/)
					.filter((l) => l.startsWith("FAIL"))
					.map((l) => "  " + l.trim());
				throw new Error(
					`create: '${plan.name}' failed the skill-forge lint gate ` +
						`(${FORGE_SKILL}/bin/skill-lint.py, ${LINT_PROFILE} profile) — nothing was written:\n` +
						(fails.length ? fails.join("\n") : `  (linter exit ${res.status})\n${(res.stdout ?? "").trim()}`),
				);
			}
		}
	} finally {
		try {
			fs.rmSync(stageRoot, { recursive: true, force: true });
		} catch {
			/* best effort */
		}
	}
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
	"only; removes a symlinked deployment link without touching a linked source tree). DISCIPLINE: make the " +
	"description's first ~57 chars a self-contained trigger ('Use when <trigger>. <behavior>.'); write " +
	"lessons, not logs — imperative rule + why, one rule per lesson, no dates or PR numbers; extend existing " +
	"references/ files before adding new ones; keep SKILL.md <= 15KB. Use after discovering a non-trivial " +
	"workflow, when a skill you followed was missing or wrong, or to capture a reusable procedure for later " +
	"sessions. CREATING A SKILL IS GATED, PATCHING IS NOT: a create op asks the user for approval first " +
	"(no UI = refused), and the candidate must pass skill-forge's structural linter (bin/skill-lint.py, " +
	"tool-skill profile) before it lands: frontmatter needs name/description/version/triggers, a README.md " +
	"must be written in the same batch (write_file op, file_path 'README.md'), and the frontmatter must " +
	"declare the external dependency `skills: [skill-forge]`. So: patch an existing skill freely, but ask " +
	"the user before creating a new one.";

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
	// ── session-scoped state ────────────────────────────────────────────
	let toolCallCount = 0;
	let learningToolUsed = false;
	let nudgeDelivered = false;
	// session_shutdown can fire more than once for the same session (observed:
	// a --print run appended its digest section twice) — one digest per session.
	let digestWritten = false;

	pi.on("session_start", async (event) => {
		toolCallCount = 0;
		learningToolUsed = false;
		nudgeDelivered = false;
		record({ event: "session_start", reason: String((event as { reason?: string })?.reason ?? "") });
		refreshSuggestion();
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
			"When you work out a non-trivial workflow, persist it: PATCH an existing skill with skill_manage (no approval needed),",
			"and only CREATE a new skill after the user approves it (ask first, e.g. via ask_user_question). A new skill",
			"must follow the skill-forge convention — frontmatter with name, description, version and triggers, a README.md,",
			"and `skill-forge` in the frontmatter `skills:` list — and the tool lints it (skill-forge/bin/skill-lint.py)",
			"before it lands. Skills that are never used again are dead weight, so prefer patching over creating.",
			"If a skill you loaded was missing steps or wrong, patch it (skill_manage action='patch') instead of",
			"re-explaining the fix each session. Routing: task-specific knowledge — procedures, pitfalls, and the",
			"user's corrections for that kind of work — belongs in skills; facts that apply to EVERY session",
			"(who the user is, environment facts, standing conventions) belong in durable memory or the wiki",
			"(wiki_retro when llm-wiki is available). Write entries as declarative facts, not imperatives.",
			"After difficult or iterative tasks, offer to save the approach as a skill (ask before creating a new one).",
		].join("\n");
		const extra = suggestionLines();
		if (extra.length) sections.skill_memory += "\n" + extra.join("\n");
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
					"tools yet. Before finishing: if any workflow discovered here will recur, persist it — patch an " +
					"existing skill with skill_manage (procedures, pitfalls, user corrections), or, for something that " +
					"needs a skill of its own, ASK the user before creating one (a new skill must follow the " +
					"skill-forge convention and pass its lint gate). Either save what is worth saving via " +
					"skill_manage, or state plainly that nothing reusable emerged. Do not restate the transcript.",
				display: false,
			},
			{ triggerTurn: true },
		);
		if (ctx.hasUI) ctx.ui.notify("skill-memory: learning nudge delivered", "info");
	});

	// ── session-end triage digest (skip /reload) ──────────────────────────
	pi.on("session_shutdown", async (event, ctx) => {
		const reason = String((event as { reason?: string })?.reason ?? "");
		if (!TRIAGE_ON_SHUTDOWN || reason === "reload" || digestWritten) return;
		digestWritten = true; // set first: a second fire must not duplicate the section
		try {
			const root = resolveEvolutionRoot();
			if (!root) return;
			const rows = runTriage(root);
			if (!rows) return;
			writeDigest(rows, sessionEventCounts(), reason);
			if (ctx?.hasUI) ctx.ui.notify("skill-memory: triage digest written", "info");
		} catch {
			/* never break shutdown */
		}
	});

	// ── skill_manage tool ─────────────────────────────────────────────────
	pi.registerTool({
		name: "skill_manage",
		label: "Skill Manage",
		description: SKILL_MANAGE_DESCRIPTION,
		parameters: SkillManageParams,
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const logs: OpLog[] = [];
			const snapshots = new Map<string, Snap>();
			try {
				// ── 1. Pre-flight: resolve every target, snapshot all affected trees
				const touchedRoots = new Set<string>();
				const planned: { op: (typeof params.operations)[number]; target: string; skillDir: string; root: string }[] = [];
				// A `create` in this batch makes its skill dir a legal target for the
				// `write_file` ops that follow it (a new skill ships its README.md
				// the same way). Ops are applied in order, so the create lands first.
				const pendingCreates = new Map<string, { skillDir: string; root: string }>();
				for (const op of params.operations) {
					const found = findSkill(op.name);
					if (op.action === "create") {
						if (found) throw new Error(`create: skill '${op.name}' already exists at ${found.entry}`);
						const dir = path.join(PRIMARY_ROOT, op.name);
						if (!NAME_RE.test(op.name)) throw new Error(`create: invalid skill name '${op.name}'`);
						fs.mkdirSync(PRIMARY_ROOT, { recursive: true });
						planned.push({ op, target: dir, skillDir: dir, root: PRIMARY_ROOT });
						pendingCreates.set(op.name, { skillDir: dir, root: PRIMARY_ROOT });
					} else {
						if (!found) {
							const pending = pendingCreates.get(op.name);
							if (!pending) {
								throw new Error(`${op.action}: skill '${op.name}' not found in: ${SKILL_ROOTS.join(", ")}`);
							}
							// target inside the skill this batch is about to create
							const rel = op.file_path ?? "SKILL.md";
							const base = pending.skillDir;
							const fpath = path.resolve(base, rel);
							if (!fpath.startsWith(base + path.sep) && fpath !== base) {
								throw new Error(`${op.action}: file_path '${op.file_path}' escapes the skill dir`);
							}
							planned.push({ op, target: fpath, skillDir: base, root: pending.root });
							continue;
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

				// ── 1b. Layer-1 write policy: a new skill needs the user's approval
				//        and must pass skill-forge's linter before anything is written.
				await gateCreates(ctx, collectCreatePlans(params.operations as { action: string; name: string; content?: string; file_path?: string }[]));

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