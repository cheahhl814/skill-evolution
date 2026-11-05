"""Extension registry — inventory every deployed Pi extension and what it provides.

Why this module exists
----------------------
The skill triage ranks *skills* from Layer-1 telemetry. Pi extensions are a
blind spot: they register tools, commands and event hooks, but nothing
records whether they are used, error out, or drift from their upstream repo.
The 12 extensions under `~/.pi/agent/extensions/` (files, dirs and symlinks)
have no usage telemetry, no dust detection and no error accounting.

Extensions do not need a runtime write gate (unlike skills, they are not
authoring targets), so v1 instrumentation is deliberately **post-hoc**:
this module builds a static registry of *what each extension provides*,
and `ext_usage.py` attributes *what actually happened* (session tool calls
and errors) back to the provider. No new runtime hooks, no session overhead.

The unit of inventory is a **deployment** — one entry in an extension root.
Deployments are usually bare `.ts` files, directories, or symlinks into a
source tree (the Layer-1 convention: live file is a symlink, never a copy).
The registry resolves each symlink to its absolute target and, when the
target lives inside a git checkout, records the checkout root — that is
what makes drift checks and fix-branches possible later.

Kind
----
    tool  — registers at least one tool or command (usage is attributable,
            dust is scoreable)
    hook  — no registered tools/commands detected; presumably fires on Pi
            events or renders UI every session. Usage is *inherent*: it
            cannot be "unused", so it is excluded from dust scoring in triage.

Extraction is heuristic (regex over source), so a hand-curated overrides map
supplements regex for forms it cannot see. Overrides add tools; they never
subtract regex hits.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Default deployment root — where Pi loads extensions from.
DEFAULT_EXTENSIONS_ROOT = Path.home() / ".pi" / "agent" / "extensions"

#: Regex blind spots, curated by hand. Key = deployment name as seen in the
#: extension root; value = extra tool names regex extraction misses.
KNOWN_EXTRA_TOOLS: dict[str, list[str]] = {
	# pi-mem-wiki registers its 14 wiki tools via nested registerTool({name:...})
	# calls spread over many files; regex catches most, these cover exotic ones.
	"pi-mem-wiki": ["wiki_read"],
	# package-doctor lives in a subdirectory; regex is tuned for it, keep a guard.
	"package-doctor": ["package_doctor"],
	# chartz-renderer registers by identifier reference: registerTool(listEngines)
	# — no string literal, so regex cannot see the names.
	"chartz-renderer.ts": ["listEngines", "renderChart"],
}

#: How deep / how broad to recurse when a deployment is a directory.
_MAX_FILES = 40
_MAX_FILE_BYTES = 400_000

_REGISTER_TOOL_RE = re.compile(r"registerTool\(\s*\"([A-Za-z_][\w-]*)\"")
_REGISTER_TOOL_NAME_RE = re.compile(
	r"registerTool\(\s*\{?[^)]{0,160}?name\s*:\s*\"([A-Za-z_][\w-]*)\"", re.DOTALL
)
_REGISTER_COMMAND_RE = re.compile(r"registerCommand\(\s*\"([A-Za-z_][\w-]*)\"")
_HOOK_RE = re.compile(r"\.on\(\s*\"([a-z_]+)\"")

_SKIP_DIR_NAMES = {"node_modules", "__pycache__", ".git", "state", "dist"}


@dataclass
class ExtensionFacts:
	"""Static facts about one deployed extension."""

	name: str
	deploy_path: str
	target_path: str
	is_symlink: bool
	repo_root: str | None
	tools: list[str] = field(default_factory=list)
	commands: list[str] = field(default_factory=list)
	hooks: list[str] = field(default_factory=list)

	@property
	def kind(self) -> str:
		return "tool" if self.tools or self.commands else "hook"

	@property
	def primary_tool(self) -> str:
		return self.tools[0] if self.tools else (self.commands[0] if self.commands else "")

	def to_dict(self) -> dict:
		return {
			"name": self.name,
			"deploy_path": self.deploy_path,
			"target_path": self.target_path,
			"is_symlink": self.is_symlink,
			"repo_root": self.repo_root,
			"kind": self.kind,
			"tools": self.tools,
			"commands": self.commands,
			"hooks": self.hooks,
		}


def extension_roots(extra: list[str] | None = None) -> list[Path]:
	"""Extension deployment roots: the default root plus ``PI_EXTENSION_SCAN_ROOTS``
	(``:``-separated), deduplicated in resolved order. Later roots win, so a
	project-scoped deployment can shadow the global one — mirrors the
	``PI_SKILL_MANAGE_ROOTS`` rule used for skills."""
	roots = [DEFAULT_EXTENSIONS_ROOT]
	raw = extra if extra is not None else (os.environ.get("PI_EXTENSION_SCAN_ROOTS") or "").split(":")
	for r in raw:
		if r:
			roots.append(Path(r).expanduser())
	seen: set[Path] = set()
	out: list[Path] = []
	for r in roots:
		try:
			rp = r.resolve()
		except OSError:
			continue
		if rp not in seen and rp.is_dir():
			seen.add(rp)
			out.append(rp)
	return out


def _it_source_files(target: Path):
	"""Yield source files for a deployment target (single file or shallow dir walk)."""
	try:
		if target.is_file():
			yield target
			return
		if not target.is_dir():
			return
		count = 0
		for pattern in ("*.ts", "*.js"):
			for f in sorted(target.rglob(pattern)):
				if count >= _MAX_FILES:
					return
				if _SKIP_DIR_NAMES.intersection(f.relative_to(target).parts):
					continue
				try:
					if f.stat().st_size > _MAX_FILE_BYTES:
						continue
				except OSError:
					continue
				yield f
				count += 1
	except OSError:
		return


def find_repo_root(target: Path) -> Path | None:
	"""Walk up from `target` to the nearest git checkout root."""
	cur = target.resolve()
	if cur.is_file():
		cur = cur.parent
	for _ in range(12):
		if (cur / ".git").exists():
			return cur
		parent = cur.parent
		if parent == cur:
			return None
		cur = parent
	return None


def extract_provides(target: Path) -> tuple[list[str], list[str], list[str]]:
	"""Best-effort (tools, commands, hooks) regex extraction from extension source."""
	tools: set[str] = set()
	commands: set[str] = set()
	hooks: set[str] = set()
	for f in _it_source_files(target):
		try:
			text = f.read_text(errors="replace")[:_MAX_FILE_BYTES]
		except OSError:
			continue
		tools.update(_REGISTER_TOOL_RE.findall(text))
		tools.update(_REGISTER_TOOL_NAME_RE.findall(text))
		commands.update(_REGISTER_COMMAND_RE.findall(text))
		hooks.update(_HOOK_RE.findall(text))
	tools.discard("")
	return sorted(tools), sorted(commands), sorted(hooks)


def scan_extensions(roots: list[Path] | None = None) -> dict[str, ExtensionFacts]:
	"""Inventory every deployment in the given extension roots.

	Roots come from the environment (`PI_EXTENSION_SCAN_ROOTS`, `:`-separated)
	by default, plus `DEFAULT_EXTENSIONS_ROOT`. The same extension name may
	appear in several roots; later roots win (documented), so a project-scoped
	deployment can shadow the global one in the registry — mirrors Pi's own
	skill-root shadowing rule.
	"""
	return _scan(roots if roots is not None else extension_roots())


def _scan(root_list: list[Path]) -> dict[str, ExtensionFacts]:
	facts: dict[str, ExtensionFacts] = {}
	for root in root_list:
		try:
			if not root.is_dir():
				continue
			entries = sorted(root.iterdir())
		except OSError:
			continue
		for entry in entries:
			try:
				name = entry.name
				is_symlink = entry.is_symlink()
				# Only .ts/.js files, directories, and symlinks count as
				# deployments; readme/scratch files in the root are ignored.
				if not entry.is_symlink() and not entry.is_dir() and entry.suffix not in (".ts", ".js"):
					continue
				try:
					target = entry.resolve()
				except OSError:
					continue
				if not target.exists():
					continue
				tools, commands, hooks = extract_provides(target)
				extra = KNOWN_EXTRA_TOOLS.get(name)
				if extra:
					tools = sorted(set(tools) | set(extra))
				repo = find_repo_root(target)
				facts[name] = ExtensionFacts(
					name=name,
					deploy_path=str(entry),
					target_path=str(target),
					is_symlink=is_symlink,
					repo_root=str(repo) if repo else None,
					tools=tools,
					commands=commands,
					hooks=hooks,
				)
			except OSError:
				continue
	return facts
