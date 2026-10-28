/**
 * Layer-1 write-policy gate tests.
 *
 * The extension (layer1/extension/skill-memory.ts) gates the `create` op twice:
 *   1. the user approves it (ctx.ui.confirm; no UI = fail closed), and
 *   2. the candidate passes skill-forge's structural linter before it lands.
 *
 * This harness loads the real extension with jiti, under a throwaway HOME so
 * the tool writes into a temp skills root instead of the live one, and drives
 * the registered `skill_manage` tool directly.
 *
 * Run: node tests/test_extension_gate.mjs
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..");
const EXT_PATH = path.join(REPO, "layer1", "extension", "skill-memory.ts");
const REAL_HOME = os.homedir();
const FORGE_DIR = path.join(REAL_HOME, ".agents", "skills", "skill-forge");

/**
 * The extension imports @earendil-works/pi-ai and typebox as values, and both
 * are ESM-only. Load it the way pi does: stage a copy of the real source next
 * to a node_modules tree that has them, and import it natively (Node strips the
 * types). The copy is refreshed per scenario, so the test always exercises the
 * current source file.
 */
function findPkgRoot() {
	const candidates = [
		"/usr/lib/node_modules/@earendil-works/pi-coding-agent/node_modules",
		path.join(REAL_HOME, ".pi/agent/npm/node_modules"),
		path.join(REPO, "node_modules"),
	];
	for (const root of candidates) {
		if (
			fs.existsSync(path.join(root, "@earendil-works", "pi-ai")) &&
			fs.existsSync(path.join(root, "typebox"))
		) {
			return root;
		}
	}
	throw new Error("could not locate a node_modules tree with @earendil-works/pi-ai + typebox");
}
const PKG_ROOT = findPkgRoot();

let importCounter = 0;

// ── a minimal ExtensionAPI mock ────────────────────────────────────────────
function mockPi() {
	const handlers = new Map();
	const tools = new Map();
	return {
		handlers,
		tools,
		on(event, handler) {
			if (!handlers.has(event)) handlers.set(event, []);
			handlers.get(event).push(handler);
		},
		registerTool(tool) {
			tools.set(tool.name, tool);
		},
		sendMessage() {},
		/** Fire an event the way pi's runner does: handlers in order, the
		 *  first blocking result wins. */
		async emit(event, payload, ctx) {
			let result;
			for (const handler of handlers.get(event) ?? []) {
				const r = await handler(payload, ctx);
				if (r) {
					result = r;
					if (r.block) return r;
				}
			}
			return result;
		},
	};
}

/** Load the extension with a throwaway HOME and return the skill_manage tool. */
async function loadTool(env = {}) {
	const home = fs.mkdtempSync(path.join(os.tmpdir(), "skill-gate-home-"));
	const skills = path.join(home, ".agents", "skills");
	fs.mkdirSync(skills, { recursive: true });
	// skill-forge must be reachable in the roots the tool writes to
	fs.symlinkSync(FORGE_DIR, path.join(skills, "skill-forge"));

	// stage the extension next to the packages it imports
	const stage = fs.mkdtempSync(path.join(os.tmpdir(), "skill-gate-stage-"));
	fs.symlinkSync(PKG_ROOT, path.join(stage, "node_modules"));
	const staged = path.join(stage, "skill-memory.ts");
	fs.copyFileSync(EXT_PATH, staged);

	const prevHome = process.env.HOME;
	const prevEnv = { ...process.env };
	process.env.HOME = home;
	// The evidence bar is computed by the monitor, which lives in this repo.
	// Point the extension at it so the bar is actually exercised.
	process.env.PI_SKILL_EVOLUTION_ROOT = REPO;
	// The monitor reads the telemetry from the throwaway HOME.
	process.env.PI_SKILL_TELEMETRY_PATH = path.join(
		home,
		".local",
		"share",
		"skill-evolution",
		"skill-usage.jsonl",
	);
	for (const [k, v] of Object.entries(env)) {
		if (v === undefined) delete process.env[k];
		else process.env[k] = v;
	}
	try {
		const mod = await import(`${pathToFileURL(staged).href}?v=${++importCounter}`);
		const pi = mockPi();
		mod.default(pi);
		return {
			tool: pi.tools.get("skill_manage"),
			pi,
			home,
			skills,
			cleanup() {
				process.env.HOME = prevHome;
				for (const k of Object.keys(process.env)) if (!(k in prevEnv)) delete process.env[k];
				Object.assign(process.env, prevEnv);
				fs.rmSync(home, { recursive: true, force: true });
				fs.rmSync(stage, { recursive: true, force: true });
			},
		};
	} catch (err) {
		process.env.HOME = prevHome;
		Object.assign(process.env, prevEnv);
		fs.rmSync(home, { recursive: true, force: true });
		fs.rmSync(stage, { recursive: true, force: true });
		throw err;
	}
}

// ── fixtures ───────────────────────────────────────────────────────────────
function skillMd(name, overrides = {}) {
	const {
		description = `Use when testing the create gate. A ${name} skill.`,
		version = "1.0.0",
		triggers = ["demo trigger"],
		skills = ["skill-forge"],
	} = overrides;
	return `---
name: ${name}
description: "${description}"
version: ${version}
updated: "2026-10-28"
triggers:
${triggers.map((t) => `  - "${t}"`).join("\n")}
skills:
${skills.map((s) => `  - ${s}`).join("\n")}
---

# ${name}

Body.
`;
}

const README = "# demo-skill\n\nA demo skill for the gate tests.\n";

function createOps(name, opts = {}) {
	const ops = [{ action: "create", name, content: skillMd(name, opts.md ?? {}) }];
	if (opts.readme !== false) {
		ops.push({ action: "write_file", name, file_path: "README.md", content: README });
	}
	return ops;
}

function ctxWith(confirm) {
	return { hasUI: true, ui: { confirm } };
}

/**
 * A telemetry file that makes the accumulated-experience bar pass for the
 * given skills (used across several sessions AND a deficiency signal), so
 * the tests can exercise the permission gate independently of the bar.
 */
function seedEvidence(home, skill, { sessions = 2, revisits = 3, failures = 1, proposals = 0 } = {}) {
	const dir = path.join(home, ".local", "share", "skill-evolution");
	fs.mkdirSync(dir, { recursive: true });
	const lines = [];
	for (let i = 0; i < sessions; i++) {
		lines.push({ ts: "2026-10-28T00:00:00Z", session: `s-seed-${i}`, event: "skill_loaded", skill, source: "test" });
	}
	for (let i = 0; i < revisits; i++) {
		lines.push({ ts: "2026-10-28T00:00:00Z", session: "s-seed-0", event: "skill_read", skill, source: "test" });
	}
	for (let i = 0; i < failures; i++) {
		lines.push({ ts: "2026-10-28T00:00:00Z", session: "s-seed-0", event: "skill_failed", skill, source: "test" });
	}
	for (let i = 0; i < proposals; i++) {
		lines.push({ ts: "2026-10-28T00:00:00Z", session: `s-seed-${i}`, event: "skill_proposed", skill, source: "test" });
	}
	fs.appendFileSync(path.join(dir, "skill-usage.jsonl"), lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
}

/**
 * A new skill has no usage history — its accumulated experience is the
 * agent's own repeated conclusion. Seed the proposals so the create bar is
 * crossed and the test exercises the gate BEHIND the bar.
 */
function seedProposals(home, skill, sessions = 2) {
	seedEvidence(home, skill, { sessions: 0, revisits: 0, failures: 0, proposals: sessions });
}

// ── the tests ──────────────────────────────────────────────────────────────
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("create: the user declines → nothing is written", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		let asked = 0;
		const res = await t.tool.execute(
			"c1",
			{ operations: createOps("demo-skill") },
			undefined,
			undefined,
			ctxWith(async () => {
				asked++;
				return false;
			}),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /not approved by the user/);
		assert.equal(asked, 1, "the user must be asked exactly once");
		assert.equal(fs.existsSync(path.join(t.skills, "demo-skill")), false, "no skill dir may be created");
	} finally {
		t.cleanup();
	}
});

test("create: the user approves → the skill lands with its README", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		const res = await t.tool.execute(
			"c2",
			{ operations: createOps("demo-skill") },
			undefined,
			undefined,
			ctxWith(async () => true),
		);
		assert.equal(res.details.ok, true, res.details.error ?? "");
		const dir = path.join(t.skills, "demo-skill");
		assert.ok(fs.existsSync(path.join(dir, "SKILL.md")));
		assert.ok(fs.existsSync(path.join(dir, "README.md")));
	} finally {
		t.cleanup();
	}
});

test("create: no UI (print mode) → fail closed", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		const res = await t.tool.execute(
			"c3",
			{ operations: createOps("demo-skill") },
			undefined,
			undefined,
			{ hasUI: false, ui: {} },
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /no UI/);
		assert.equal(fs.existsSync(path.join(t.skills, "demo-skill")), false);
	} finally {
		t.cleanup();
	}
});

test("create: PI_SKILL_MANAGE_ALLOW_CREATE=1 skips the ask (lint still applies)", async () => {
	const t = await loadTool({ PI_SKILL_MANAGE_ALLOW_CREATE: "1" });
	try {
		seedProposals(t.home, "demo-skill");
		let asked = 0;
		const res = await t.tool.execute(
			"c4",
			{ operations: createOps("demo-skill") },
			undefined,
			undefined,
			ctxWith(async () => {
				asked++;
				return true;
			}),
		);
		assert.equal(res.details.ok, true, res.details.error ?? "");
		assert.equal(asked, 0, "no approval prompt in unattended mode");
		assert.ok(fs.existsSync(path.join(t.skills, "demo-skill", "SKILL.md")));
	} finally {
		t.cleanup();
	}
});

test("create: a missing README.md is rejected (skill-forge contract)", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		const res = await t.tool.execute(
			"c5",
			{ operations: createOps("demo-skill", { readme: false }) },
			undefined,
			undefined,
			ctxWith(async () => true),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /README\.md/);
		assert.equal(fs.existsSync(path.join(t.skills, "demo-skill")), false);
	} finally {
		t.cleanup();
	}
});

test("create: a missing `skills: [skill-forge]` dependency is rejected", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		const res = await t.tool.execute(
			"c6",
			{ operations: createOps("demo-skill", { md: { skills: [] } }) },
			undefined,
			undefined,
			ctxWith(async () => true),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /skill-forge/);
		assert.equal(fs.existsSync(path.join(t.skills, "demo-skill")), false);
	} finally {
		t.cleanup();
	}
});

test("create: a missing `version` is rejected", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		const res = await t.tool.execute(
			"c7",
			{ operations: createOps("demo-skill", { md: { version: "" } }) },
			undefined,
			undefined,
			ctxWith(async () => true),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /version/);
	} finally {
		t.cleanup();
	}
});

test("create: a missing `triggers` list is rejected", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		const res = await t.tool.execute(
			"c8",
			{ operations: createOps("demo-skill", { md: { triggers: [] } }) },
			undefined,
			undefined,
			ctxWith(async () => true),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /triggers/);
	} finally {
		t.cleanup();
	}
});

test("create: an over-long description is rejected by the lint gate", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		const res = await t.tool.execute(
			"c9",
			{ operations: createOps("demo-skill", { md: { description: "x".repeat(1200) } }) },
			undefined,
			undefined,
			ctxWith(async () => true),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /skill-forge lint gate/);
		assert.equal(fs.existsSync(path.join(t.skills, "demo-skill")), false);
	} finally {
		t.cleanup();
	}
});

test("create: the lint gate runs BEFORE the approval prompt is trusted (lint fail → no write)", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		const res = await t.tool.execute(
			"c10",
			{ operations: createOps("demo-skill", { md: { description: "x".repeat(1200) } }) },
			undefined,
			undefined,
			ctxWith(async () => true),
		);
		assert.equal(res.details.ok, false);
		assert.equal(fs.existsSync(path.join(t.skills, "demo-skill")), false);
	} finally {
		t.cleanup();
	}
});

test("patch: an existing skill is patched only with the user's approval", async () => {
	const t = await loadTool();
	try {
		const dir = path.join(t.skills, "demo-skill");
		fs.mkdirSync(dir, { recursive: true });
		fs.writeFileSync(path.join(dir, "SKILL.md"), skillMd("demo-skill"));
		seedEvidence(t.home, "demo-skill");
		let asked = 0;
		const res = await t.tool.execute(
			"c11",
			{ operations: [{ action: "patch", name: "demo-skill", old_string: "Body.", new_string: "Better body." }] },
			undefined,
			undefined,
			ctxWith(async () => {
				asked++;
				return true;
			}),
		);
		assert.equal(res.details.ok, true, res.details.error ?? "");
		assert.equal(asked, 1, "a revision needs the user's approval");
		assert.match(fs.readFileSync(path.join(dir, "SKILL.md"), "utf8"), /Better body\./);
	} finally {
		t.cleanup();
	}
});

test("create: a batch of two skills asks once and lands both", async () => {
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-a");
		seedProposals(t.home, "demo-b");
		let asked = 0;
		const res = await t.tool.execute(
			"c12",
			{ operations: [...createOps("demo-a"), ...createOps("demo-b")] },
			undefined,
			undefined,
			ctxWith(async () => {
				asked++;
				return true;
			}),
		);
		assert.equal(res.details.ok, true, res.details.error ?? "");
		assert.equal(asked, 1, "one prompt for the batch");
		assert.ok(fs.existsSync(path.join(t.skills, "demo-a", "SKILL.md")));
		assert.ok(fs.existsSync(path.join(t.skills, "demo-b", "SKILL.md")));
	} finally {
		t.cleanup();
	}
});

test("create: the staged candidate is cleaned up (no temp dir leak)", async () => {
	const before = fs.readdirSync(os.tmpdir()).filter((f) => f.startsWith("skill-manage-stage-"));
	const t = await loadTool();
	try {
		seedProposals(t.home, "demo-skill");
		await t.tool.execute(
			"c13",
			{ operations: createOps("demo-skill") },
			undefined,
			undefined,
			ctxWith(async () => true),
		);
	} finally {
		t.cleanup();
	}
	const after = fs.readdirSync(os.tmpdir()).filter((f) => f.startsWith("skill-manage-stage-"));
	assert.equal(after.length, before.length, "staging dirs must be removed");
});

test("create: the accumulated-experience bar refuses a first-time proposal", async () => {
	const t = await loadTool();
	try {
		// one proposal in one session -> not yet a conclusion
		seedProposals(t.home, "demo-skill", 1);
		let asked = 0;
		const res = await t.tool.execute(
			"c14",
			{ operations: createOps("demo-skill") },
			undefined,
			undefined,
			ctxWith(async () => {
				asked++;
				return true;
			}),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /accumulated-experience bar/);
		assert.equal(asked, 0, "the user must not be asked when the bar is not met");
		assert.equal(fs.existsSync(path.join(t.skills, "demo-skill")), false);
	} finally {
		t.cleanup();
	}
});

// ── the revision gate (patch / write_file on an existing skill) ────────────

/** Seed an existing skill on disk so a patch has a target. */
function seedSkill(t, name, body = "Body.\n") {
	const dir = path.join(t.skills, name);
	fs.mkdirSync(dir, { recursive: true });
	fs.writeFileSync(path.join(dir, "SKILL.md"), skillMd(name).replace("Body.", body));
	return dir;
}

test("patch: needs the user's approval → declined means nothing is written", async () => {
	const t = await loadTool();
	try {
		const dir = seedSkill(t, "demo-skill");
		seedEvidence(t.home, "demo-skill");
		let asked = 0;
		const res = await t.tool.execute(
			"r1",
			{
				operations: [
					{ action: "patch", name: "demo-skill", old_string: "Body.", new_string: "Better body." },
				],
			},
			undefined,
			undefined,
			ctxWith(async () => {
				asked++;
				return false;
			}),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /not approved by the user/);
		assert.equal(asked, 1, "the user must be asked exactly once");
		assert.match(fs.readFileSync(path.join(dir, "SKILL.md"), "utf8"), /Body\./, "the patch must not land");
	} finally {
		t.cleanup();
	}
});

test("patch: the user approves → the patch lands", async () => {
	const t = await loadTool();
	try {
		const dir = seedSkill(t, "demo-skill");
		seedEvidence(t.home, "demo-skill");
		const res = await t.tool.execute(
			"r2",
			{
				operations: [
					{ action: "patch", name: "demo-skill", old_string: "Body.", new_string: "Better body." },
				],
			},
			undefined,
			undefined,
			ctxWith(async () => true),
		);
		assert.equal(res.details.ok, true, res.details.error ?? "");
		assert.match(fs.readFileSync(path.join(dir, "SKILL.md"), "utf8"), /Better body\./);
	} finally {
		t.cleanup();
	}
});

test("patch: the accumulated-experience bar refuses a one-off skill", async () => {
	const t = await loadTool();
	try {
		const dir = seedSkill(t, "demo-skill");
		// one session, no deficiency signal -> the bar says "not a conclusion"
		seedEvidence(t.home, "demo-skill", { sessions: 1, revisits: 1, failures: 0 });
		let asked = 0;
		const res = await t.tool.execute(
			"r3",
			{
				operations: [
					{ action: "patch", name: "demo-skill", old_string: "Body.", new_string: "Better body." },
				],
			},
			undefined,
			undefined,
			ctxWith(async () => {
				asked++;
				return true;
			}),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /accumulated-experience bar/);
		assert.equal(asked, 0, "the user must not even be asked when the bar is not met");
		assert.match(fs.readFileSync(path.join(dir, "SKILL.md"), "utf8"), /Body\./);
	} finally {
		t.cleanup();
	}
});

test("patch: no UI → fail closed", async () => {
	const t = await loadTool();
	try {
		seedSkill(t, "demo-skill");
		seedEvidence(t.home, "demo-skill");
		const res = await t.tool.execute(
			"r4",
			{
				operations: [
					{ action: "patch", name: "demo-skill", old_string: "Body.", new_string: "Better body." },
				],
			},
			undefined,
			undefined,
			{ hasUI: false, ui: {} },
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /no UI/);
	} finally {
		t.cleanup();
	}
});

test("write_file: revising an existing skill also needs approval", async () => {
	const t = await loadTool();
	try {
		const dir = seedSkill(t, "demo-skill");
		seedEvidence(t.home, "demo-skill");
		const res = await t.tool.execute(
			"r5",
			{
				operations: [
					{ action: "write_file", name: "demo-skill", file_path: "references/x.md", content: "notes" },
				],
			},
			undefined,
			undefined,
			ctxWith(async () => false),
		);
		assert.equal(res.details.ok, false);
		assert.match(res.details.error, /not approved by the user/);
		assert.equal(fs.existsSync(path.join(dir, "references", "x.md")), false);
	} finally {
		t.cleanup();
	}
});

// ── the direct-edit gate (a plain `edit` of a deployed SKILL.md) ───────────

test("direct edit: a SKILL.md outside the session cwd needs approval", async () => {
	const t = await loadTool();
	try {
		const dir = seedSkill(t, "demo-skill");
		seedEvidence(t.home, "demo-skill");
		const target = path.join(dir, "SKILL.md");
		let asked = 0;
		const res = await t.pi.emit(
			"tool_call",
			{ toolName: "edit", input: { path: target } },
			ctxWith(async () => {
				asked++;
				return false;
			}),
		);
		assert.ok(res && res.block, "the direct edit must be blocked");
		assert.match(res.reason, /not approved by the user/);
		assert.equal(asked, 1, "the user must be asked exactly once");
	} finally {
		t.cleanup();
	}
});

test("direct edit: the user approves → the edit is allowed through", async () => {
	const t = await loadTool();
	try {
		const dir = seedSkill(t, "demo-skill");
		seedEvidence(t.home, "demo-skill");
		const target = path.join(dir, "SKILL.md");
		const res = await t.pi.emit(
			"tool_call",
			{ toolName: "edit", input: { path: target } },
			ctxWith(async () => true),
		);
		assert.ok(!res || !res.block, "an approved direct edit must not be blocked");
	} finally {
		t.cleanup();
	}
});

test("direct edit: a SKILL.md inside the session cwd is ordinary work", async () => {
	const t = await loadTool();
	try {
		// the skill the agent is developing lives in the session's own tree
		const cwd = path.join(t.home, "work");
		fs.mkdirSync(cwd, { recursive: true });
		const dir = path.join(cwd, "my-skill");
		fs.mkdirSync(dir, { recursive: true });
		const target = path.join(dir, "SKILL.md");
		fs.writeFileSync(target, skillMd("my-skill"));
		const res = await t.pi.emit(
			"tool_call",
			{ toolName: "edit", input: { path: target } },
			{ hasUI: false, ui: {}, cwd },
		);
		assert.ok(!res || !res.block, "editing the skill you are developing must not be gated");
	} finally {
		t.cleanup();
	}
});

test("direct edit: a non-SKILL.md file is never gated", async () => {
	const t = await loadTool();
	try {
		const target = path.join(t.skills, "demo-skill", "references", "notes.md");
		fs.mkdirSync(path.dirname(target), { recursive: true });
		fs.writeFileSync(target, "notes");
		const res = await t.pi.emit(
			"tool_call",
			{ toolName: "edit", input: { path: target } },
			{ hasUI: false, ui: {} },
		);
		assert.ok(!res || !res.block, "only skill definitions are gated");
	} finally {
		t.cleanup();
	}
});

// ── runner ─────────────────────────────────────────────────────────────────
let failed = 0;
for (const [name, fn] of tests) {
	try {
		await fn();
		console.log(`ok   ${name}`);
	} catch (err) {
		failed++;
		console.error(`FAIL ${name}\n     ${err?.message ?? err}`);
	}
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
