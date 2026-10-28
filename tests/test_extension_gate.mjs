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

// ── the tests ──────────────────────────────────────────────────────────────
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("create: the user declines → nothing is written", async () => {
	const t = await loadTool();
	try {
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
		assert.match(res.details.error, /declined by the user/);
		assert.equal(asked, 1, "the user must be asked exactly once");
		assert.equal(fs.existsSync(path.join(t.skills, "demo-skill")), false, "no skill dir may be created");
	} finally {
		t.cleanup();
	}
});

test("create: the user approves → the skill lands with its README", async () => {
	const t = await loadTool();
	try {
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

test("patch: an existing skill is patched without any approval prompt", async () => {
	const t = await loadTool();
	try {
		const dir = path.join(t.skills, "demo-skill");
		fs.mkdirSync(dir, { recursive: true });
		fs.writeFileSync(path.join(dir, "SKILL.md"), skillMd("demo-skill"));
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
		assert.equal(asked, 0, "patching must not ask the user");
		assert.match(fs.readFileSync(path.join(dir, "SKILL.md"), "utf8"), /Better body\./);
	} finally {
		t.cleanup();
	}
});

test("create: a batch of two skills asks once and lands both", async () => {
	const t = await loadTool();
	try {
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
