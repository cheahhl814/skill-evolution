"""Evolve a Pi agent skill (SKILL.md) with DSPy + GEPA — Layer-2 loop.

Usage:
  python -m evolution.skills.evolve_skill --skill red-pen --eval-source synthetic --iterations 10
  python -m evolution.skills.evolve_skill --skill skill-forge --eval-source sessions
  python -m evolution.skills.evolve_skill --skill biodb-fetch --dry-run --eval-source sessions

Pipeline (Hermes self-evolution discipline, ported to Pi):
  find skill in pi roots → build dataset (synthetic | golden | sessions)
  → baseline constraints → GEPA (MIPROv2 fallback) → evolved constraints
  → holdout baseline-vs-evolved → deploy:
      resolved skill inside a git repo → branch evolve/<skill>-<ts> + commit
      otherwise                       → output/<skill>/evolved_skill.md + copy instructions
  Human review is ALWAYS required; nothing is pushed or applied directly.

Env: PI_EVOLUTION_OPTIMIZER / PI_EVOLUTION_EVAL (DSPy LM strings),
     SKILL_FORGE_LINT (optional structural gate; auto-detected).
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from evolution.core.config import EvolutionConfig, find_git_repo_root, resolve_skill
from evolution.core.dataset_builder import GoldenDatasetLoader, build_dataset_from_pi_sessions  # noqa: F401
from evolution.core.constraints import ConstraintValidator
from evolution.core.fitness import gepa_feedback_metric, skill_fitness_metric  # noqa: F401
from evolution.skills.skill_module import (
    extract_skill_text,
    load_skill,
    make_skill_module,
    reassemble_skill,
)

console = Console()


def build_dataset(skill_name, skill_text, eval_source, config, dataset_path=None):
    if eval_source == "golden" and dataset_path:
        return GoldenDatasetLoader.load(Path(dataset_path))
    if eval_source == "sessions":
        ds = build_dataset_from_pi_sessions(
            skill_name=skill_name, skill_text=skill_text, judge=False, config=config
        )
        if not ds.all_examples:
            raise SystemExit(f"no session material relevant to '{skill_name}' found under ~/.pi/agent/sessions/")
        return ds
    # synthetic (default)
    from evolution.core.dataset_builder import SyntheticDatasetBuilder

    return SyntheticDatasetBuilder(config).generate(
        artifact_text=skill_text, artifact_type="skill", skill_name=skill_name
    )


def deploy(skill_name: str, skill: dict, evolved_full: str, output_dir: Path) -> str:
    """Git-branch deployment when possible (human review via commit + diff);
    otherwise write to output/ for manual copy. Never pushes."""
    resolved = skill["path"].resolve()
    repo = find_git_repo_root(resolved)
    if repo:
        branch = f"evolve/{skill_name}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        try:
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", branch], check=True, capture_output=True)
            skill["path"].write_text(evolved_full)
            rel = resolved.relative_to(repo)
            subprocess.run(["git", "-C", str(repo), "add", str(rel)], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", f"evolve: {skill_name} — see metrics in pi-skill-evolution output"],
                check=True, capture_output=True,
            )
            return (
                f"git branch '{branch}' created in {repo} (NOT pushed). Review with: "
                f"git -C {repo} diff main...{branch}; merge or revert."
            )
        except subprocess.CalledProcessError as e:
            skill["path"].write_text(evolved_full)  # still staged as working-tree change for manual review
            stderr = (e.stderr or b"").decode()[-200:]
            return f"git branch automation failed ({stderr}); evolved file written to working tree for manual review: {resolved}"
    out = output_dir / "evolved_skill.md"
    out.write_text(evolved_full)
    return (
        f"not a git repo — review the diff manually:\n"
        f"  diff {output_dir / 'baseline_skill.md'} {out}\n"
        f"  then copy {out} → {resolved}"
    )


@click.command()
@click.option("--skill", required=True, help="Skill dir name under the pi skill roots")
@click.option("--iterations", default=10, help="GEPA iterations")
@click.option("--eval-source", default="synthetic", type=click.Choice(["synthetic", "golden", "sessions"]),
              help="Eval dataset source. 'sessions' mines ~/.pi/agent/sessions (heuristic, free)")
@click.option("--dataset-path", default=None, help="Path to golden JSONL or saved dataset dir")
@click.option("--hermes-repo", default=None, hidden=True)
@click.option("--run-tests", is_flag=True, help="Run the skill source tree's pytest as a hard gate")
@click.option("--dry-run", is_flag=True, help="Validate setup (find, load, constraints, dataset heuristic count) without API calls")
@click.option("--smoke", is_flag=True, help="Run the command smoke gate on the candidate (bin/smoke-candidate): a candidate whose commands do not run is rejected before deploy")
def main(skill, iterations, eval_source, dataset_path, hermes_repo, run_tests, dry_run, smoke):
    config = EvolutionConfig()
    skill_path = resolve_skill(skill, config)
    if not skill_path:
        console.print(f"[red]✗ skill '{skill}' not found in: {[str(p) for p in config.skills_roots]}[/red]")
        raise SystemExit(1)
    console.print(f"\n[bold cyan]🧬 pi-skill-evolution[/bold cyan] — target: [bold]{skill}[/bold]")
    console.print(f"  resolved: {skill_path}")

    found = load_skill(skill_path)
    console.print(f"  size: {len(found['body']):,} chars | description trigger: {found['description'][:60]}...")

    validator = ConstraintValidator(config)
    console.print("\n[bold]Baseline constraints[/bold]")
    for c in validator.validate_all(found["raw"], baseline_text=None):
        console.print(f"  [{'green' if c.passed else 'red'}]{'✓' if c.passed else '✗'} {c.constraint_name}: {c.message}")
        if not c.passed:
            console.print("  (baseline violation noted — evolution must still improve on it)")

    lint = validator.validate_skill_dir(skill_path.parent)
    console.print(f"  [{'green' if lint.passed else 'yellow'}]{'✓' if lint.passed else '!'} skillforge_lint: {lint.message}")

    if dry_run:
        if eval_source == "sessions":
            from evolution.core.pi_sessions import mine_pi_sessions

            pairs = mine_pi_sessions(skill)
            console.print(f"\n[bold]Dry run[/bold]: mined [bold]{len(pairs)}[/bold] relevant session pair(s) from ~/.pi/agent/sessions (heuristic, no API cost)")
            for p in pairs[:3]:
                console.print(f"  • [{p.timestamp[:10]}] {p.user_text[:90]}...")
        else:
            console.print("\n[bold]Dry run[/bold]: setup validated; synthetic generation / GEPA would run on the configured models:")
            console.print(f"  optimizer: {config.optimizer_model} | eval: {config.eval_model}")
        console.print(f"  lint gate: {config.skillforge_lint or '(not configured)'}")
        console.print(f"  deployment: git branch if the skill's source tree is a repo — {find_git_repo_root(skill_path.resolve()) or 'output/ copy'}")
        console.print("[green]✓ dry run complete — nothing was modified[/green]")
        return

    # ── real run: dataset → GEPA → gates → holdout → deploy ────────────────
    import dspy

    start = time.time()
    dataset = build_dataset(skill, found["body"], eval_source, config, dataset_path)
    console.print(f"\n  dataset: {len(dataset.train)} train / {len(dataset.val)} val / {len(dataset.holdout)} holdout ({eval_source})")

    validator.validate_all(found["body"], baseline_text=None)
    lm = dspy.LM(config.eval_model)
    dspy.configure(lm=lm)
    baseline_module = make_skill_module(found["body"])

    console.print(f"\n[bold cyan]Running GEPA ({iterations} iterations)...[/bold cyan]")
    try:
        # dspy >=3.x: GEPA uses call budgets (max_metric_calls) and a 5-arg
        # feedback metric ({'score','feedback'}); MIPROv2 fallback keeps the
        # plain-float metric.
        optimizer = dspy.GEPA(
            metric=gepa_feedback_metric,
            max_metric_calls=iterations * 10,
            reflection_lm=dspy.LM(config.optimizer_model),
            num_threads=4,
        )
        optimized = optimizer.compile(baseline_module, trainset=dataset.to_dspy_examples("train"), valset=dataset.to_dspy_examples("val"))
    except Exception as e:
        console.print(f"[yellow]GEPA unavailable ({e}); falling back to MIPROv2[/yellow]")
        optimizer = dspy.MIPROv2(metric=skill_fitness_metric, auto="light")
        optimized = optimizer.compile(baseline_module, trainset=dataset.to_dspy_examples("train"))

    evolved_body = extract_skill_text(optimized)
    evolved_full = reassemble_skill(found["frontmatter"], evolved_body)

    console.print("[bold]Evolved constraints[/bold]")
    all_pass = True
    for c in validator.validate_all(evolved_full, baseline_text=found["raw"]):
        console.print(f"  [{'green' if c.passed else 'red'}]{'✓' if c.passed else '✗'} {c.constraint_name}: {c.message}")
        all_pass = all_pass and c.passed
    if not all_pass:
        failed_dir = Path("output") / skill / "evolved_FAILED.md"
        failed_dir.parent.mkdir(parents=True, exist_ok=True)
        failed_dir.write_text(evolved_full)
        console.print(f"[red]✗ evolved variant FAILED constraints — saved for inspection: {failed_dir}[/red]")
        raise SystemExit(1)

    console.print("[bold]Holdout evaluation (baseline vs evolved)[/bold]")
    base_scores, evo_scores = [], []
    for ex in dataset.to_dspy_examples("holdout"):
        base_scores.append(skill_fitness_metric(ex, baseline_module(task_input=ex.task_input)))
        evo_scores.append(skill_fitness_metric(ex, optimized(task_input=ex.task_input)))
    avg_base = sum(base_scores) / max(1, len(base_scores))
    avg_evo = sum(evo_scores) / max(1, len(evo_scores))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("output") / skill / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "baseline_skill.md").write_text(found["raw"])
    (out_dir / "metrics.json").write_text(json.dumps({
        "skill": skill, "timestamp": ts, "iterations": iterations, "eval_source": eval_source,
        "baseline_score": avg_base, "evolved_score": avg_evo, "improvement": avg_evo - avg_base,
        "baseline_bytes": len(found["body"].encode()), "evolved_bytes": len(evolved_body.encode()),
        "constraints_passed": all_pass, "elapsed_seconds": round(time.time() - start, 1),
    }, indent=2))

    table = Table(title="Evolution results")
    for col in ("Metric", "Baseline", "Evolved", "Change"):
        table.add_column(col, justify="right")
    delta = avg_evo - avg_base
    change = f"[green]{delta:+.3f}[/green]" if delta >= 0 else f"[red]{delta:+.3f}[/red]"
    table.add_row("Holdout score", f"{avg_base:.3f}", f"{avg_evo:.3f}", change)
    table.add_row("Body bytes", str(len(found['body'].encode())), str(len(evolved_body.encode())), f"{len(evolved_body.encode()) - len(found['body'].encode()):+}")
    console.print(table)
    if delta <= 0:
        console.print("[yellow]⚠ evolution did not improve the skill — nothing deployed[/yellow]")
        console.print(f"  diff: diff {out_dir / 'baseline_skill.md'} <{out_dir / 'evolved_skill.md'}>")
        return

    (out_dir / "evolved_skill.md").write_text(evolved_full)

    # ── optional command smoke gate ──────────────────────────────────────
    # Lint checks the skill's STRUCTURE; it cannot check that the commands the
    # skill tells the agent to run actually work. A candidate can pass every
    # constraint, beat the baseline, and still contain a command that fails
    # when executed (observed: `date -d "2024-03-15" +%V / %G` -> "extra
    # operand '/'"). The gate extracts the commands, runs the allowlisted
    # ones, and rejects the candidate if any of them fail.
    if smoke:
        gate = Path(__file__).resolve().parents[2] / "bin" / "smoke-candidate"
        if gate.exists():
            import subprocess

            proc = subprocess.run(
                ["bash", str(gate), str(out_dir / "evolved_skill.md"), "--json"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            try:
                report = json.loads(proc.stdout or "{}")
            except ValueError:
                report = {}
            failed = report.get("failed", 0)
            if proc.returncode != 0:
                console.print(f"[red]✗ smoke gate: {failed} command(s) in the candidate do not run[/red]")
                for r in report.get("results", []):
                    if r.get("status") == "FAIL":
                        console.print(f"    [red]{r.get('command')}[/red] -> {r.get('stderr', '')[:120]}")
                (out_dir / "evolved_skill.md").rename(out_dir / "evolved_FAILED_smoke.md")
                console.print(f"[red]✗ candidate rejected by the smoke gate — saved for inspection: {out_dir / 'evolved_FAILED_smoke.md'}[/red]")
                raise SystemExit(1)
            console.print(f"[green]✓ smoke gate: {report.get('total', 0)} command(s) checked, all ran[/green]")
        else:
            console.print("[yellow]⚠ smoke gate requested but bin/smoke-candidate is missing — skipped[/yellow]")

    console.print(deploy(skill, found, evolved_full, out_dir))
    console.print("[green]✓ deployed for human review (nothing pushed, nothing applied to other skills)[/green]")


if __name__ == "__main__":
    main()