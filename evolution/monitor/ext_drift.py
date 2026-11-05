"""Extension drift — is each deployed extension behind its upstream repo?

The Layer-3 usage/error triage catches extensions that misbehave. This module
catches the quieter failure: the deployed copy is fine, but the upstream repo
has fixed bugs (or shipped features) it does not have. The registry already
records `repo_root` for every deployment (ext_registry), so the data is on-site.

Two modes, cost-aware (deliberate, per design notes in
references/monitor-and-digest.md §6):

    local   (default, digest) — reads refs only, zero network: classifies
            DIRTY (uncommitted changes), LOCAL-AHEAD, NO-ORIGIN, NO-GIT.
            Cannot detect BEHIND without a fetch.
    fetch   (--check-drift, opt-in) — runs `git fetch origin --quiet` first,
            so BEHIND-BY-N is detectable. One network call per deployment;
            never part of the shutdown path.

Verdicts (same legend as bin/skill-update-check.py):
    UP-TO-DATE, LOCAL-AHEAD, BEHIND-BY-N, DIRTY, OFFLINE, NO-ORIGIN, NO-GIT, ABSENT
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ext_registry import ExtensionFacts

#: Verdicts that mean "worth a human's attention" in the drift table.
ATTENTION = frozenset({"BEHIND-BY-N", "DIRTY", "NO-GIT"})

_TIMEOUT = 20  # seconds, per git call


@dataclass
class DriftFacts:
	name: str
	repo_root: str
	verdict: str
	remote_branch: str = ""
	detail: str = ""

	def to_dict(self) -> dict:
		return {
			"name": self.name,
			"repo_root": self.repo_root,
			"verdict": self.verdict,
			"remote_branch": self.remote_branch,
			"detail": self.detail,
		}


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
	try:
		return subprocess.run(
			["git", *args],
			cwd=cwd,
			capture_output=True,
			text=True,
			timeout=_TIMEOUT,
		)
	except (OSError, subprocess.TimeoutExpired):
		return None


def check_one(facts: ExtensionFacts, fetch: bool = False) -> DriftFacts | None:
	"""Drift verdict for one deployment (None when no repo to check)."""
	if not facts.repo_root:
		return None
	repo = Path(facts.repo_root)
	name = facts.name

	head = _git(["rev-parse", "HEAD"], repo)
	if head is None or head.returncode != 0:
		return DriftFacts(name=name, repo_root=str(repo), verdict="NO-GIT")

	# Resolve origin's default branch, most- to least-authoritative (live-run
	# lesson: a repo can carry origin + upstream remotes; "first origin/* ref"
	# picked origin/fork-v1 and produced a phantom LOCAL-AHEAD).
	default_branch = None
	r_head = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo)
	if r_head is not None and r_head.returncode == 0 and r_head.stdout.strip():
		default_branch = r_head.stdout.strip().split("/")[-1]
	else:
		r_branch = _git(["symbolic-ref", "--short", "HEAD"], repo)
		local_branch = r_branch.stdout.strip().split("/")[-1] if (r_branch is not None and r_branch.returncode == 0) else ""
		for candidate in ("main", "master", local_branch):
			if not candidate:
				continue
			rv = _git(["rev-parse", "--verify", f"refs/remotes/origin/{candidate}"], repo)
			if rv is not None and rv.returncode == 0:
				default_branch = candidate
				break
		if default_branch is None:
			r_list = _git(["branch", "-r", "--list", "origin/*"], repo)
			default_branch = "master"
			if r_list is not None and r_list.returncode == 0:
				for line in r_list.stdout.splitlines():
					line = line.strip()
					if line.startswith("origin/") and not line.endswith("/HEAD"):
						default_branch = line.split("/", 1)[1]
						break

	remote_ref = f"origin/{default_branch}"

	if fetch:
		fr = _git(["fetch", "origin", "--quiet"], repo)
		if fr is None:
			return DriftFacts(
				name=name,
				repo_root=str(repo),
				verdict="OFFLINE",
				remote_branch=remote_ref,
				detail="git fetch timed out or git is unavailable",
			)
		if fr.returncode != 0:
			detail = (fr.stderr or fr.stdout or "").strip().splitlines()[0] if (fr.stderr or fr.stdout) else "fetch failed"
			if fr.returncode == 128 and ("No remote" in detail or "does not appear" in detail or "not a git" in detail):
				return DriftFacts(name=name, repo_root=str(repo), verdict="NO-ORIGIN")
			return DriftFacts(
				name=name, repo_root=str(repo), verdict="OFFLINE", remote_branch=remote_ref, detail=detail
			)
	else:
		# no fetch: NO-ORIGIN when the remote ref does not exist locally
		r_or = _git(["rev-parse", "--verify", remote_ref], repo)
		if r_or is None:
			return DriftFacts(name=name, repo_root=str(repo), verdict="OFFLINE", remote_branch=remote_ref)
		if r_or.returncode != 0:
			return DriftFacts(name=name, repo_root=str(repo), verdict="NO-ORIGIN")

	# untracked-files=no: scratch/untracked artifacts (index caches, handoff
	# files, runtime state) are not drift — DIRTY must mean TRACKED changes only.
	status = _git(["status", "--porcelain", "--untracked-files=no"], repo)
	if status is not None and status.returncode == 0 and status.stdout.strip():
		return DriftFacts(
			name=name,
			repo_root=str(repo),
			verdict="DIRTY",
			remote_branch=remote_ref,
			detail=f"{len(status.stdout.strip().splitlines())} uncommitted tracked change(s)",
		)

	if not fetch:
		# Without a fetch the remote ref may be arbitrarily stale; only DIRTY,
		# NO-ORIGIN, NO-GIT and AHEAD are meaningful locally.
		_, ab_local = _ab(repo, remote_ref)
		if ab_local and ab_local[0] > 0:
			ahead, _ = ab_local
			return DriftFacts(
				name=name,
				repo_root=str(repo),
				verdict="LOCAL-AHEAD",
				remote_branch=remote_ref,
				detail=f"{ahead} unpushed commit(s); fetch-based BEHIND check skipped (no network at digest time)",
			)
		return DriftFacts(
			name=name,
			repo_root=str(repo),
			verdict="LOCAL-ONLY",
			remote_branch=remote_ref,
			detail="local checks only (AHEAD visible); run `bin/triage --extensions --check-drift` to fetch and detect BEHIND",
		)

	r_ab = _git(["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"], repo)
	if r_ab is not None and r_ab.returncode == 0:
		ahead, behind = (int(x) for x in r_ab.stdout.split())
		if behind > 0:
			return DriftFacts(
				name=name,
				repo_root=str(repo),
				verdict="BEHIND-BY-N",
				remote_branch=remote_ref,
				detail=f"behind {remote_ref} by {behind} commit(s)" + (f", ahead {ahead}" if ahead else ""),
			)
		if ahead > 0:
			return DriftFacts(
				name=name,
				repo_root=str(repo),
				verdict="LOCAL-AHEAD",
				remote_branch=remote_ref,
				detail=f"{ahead} unpushed commit(s)",
			)
		return DriftFacts(name=name, repo_root=str(repo), verdict="UP-TO-DATE", remote_branch=remote_ref)
	return DriftFacts(name=name, repo_root=str(repo), verdict="OFFLINE", remote_branch=remote_ref, detail="rev-list failed")


def _ab(repo: Path, remote_ref: str):
	r = _git(["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"], repo)
	if r is None or r.returncode != 0:
		return remote_ref, None
	ahead, behind = (int(x) for x in r.stdout.split())
	return remote_ref, (ahead, behind)


def check_all(
	facts: dict[str, ExtensionFacts],
	fetch: bool = False,
) -> list[DriftFacts]:
	"""Drift verdicts for every deployment that has a repo root."""
	out: list[DriftFacts] = []
	for name in sorted(facts):
		d = check_one(facts[name], fetch=fetch)
		if d is not None:
			out.append(d)
	return out


def render_markdown(rows: list[DriftFacts], limit: int = 20) -> str:
	out = [
		"Deployed extensions vs their upstream repos (repo_root from the registry).",
		"BEHIND needs `--check-drift` (git fetch); the digest path is local-only.",
		"",
		"| # | Extension | Verdict | Detail |",
		"| --- | --- | --- | --- |",
	]
	for i, r in enumerate(rows[:limit], start=1):
		out.append("| %d | `%s` | **%s** | %s |" % (i, r.name, r.verdict, r.detail or "—"))
	attention = [r for r in rows if r.verdict in ATTENTION]
	if attention:
		out.append("")
		out.append(
			"_Needs attention: " + ", ".join(f"`{r.name}` ({r.verdict})" for r in attention) + "_"
		)
	return "\n".join(out)
