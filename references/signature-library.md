# Signature library — known failure signatures

Full catalogue for the Layer-2 pipeline. `SKILL.md` keeps the six that
bite most often; this file holds the rest. Format: signature → likely
cause → fix.

| Signature | Likely cause | Fix |
| --- | --- | --- |
| `mined 0 relevant session pair(s)` for an obviously-used skill | Miner token/parse regression | Run `pytest tests/ -q`; read `evolution/core/pi_sessions.py` — a char-join bug (" ".join over one string) shipped exactly this once. Fixed; trust tests |
| `TypeError: cannot unpack non-iterable SessionPair object` | Dataset builder unpacks old triples post-redesign | Consume `pair.user_text` fields; see `evolution/core/dataset_builder.py`. Fixed 2026-10 (regression-tested) |
| GEPA rejects `max_steps` kwarg | dspy ≥3.x dropped it for call budgets | Use `max_metric_calls` (CLI already does); metric must accept 5 args |
| `ModuleNotFoundError: optuna` on MIPROv2 fallback | GEPA path unavailable → MIPROv2 fallback needs optuna | `uv pip install "dspy[optuna]"` in the `.venv` |
| `update-check` prints `NO-GIT` | Script runs where SKILL.md + `.git` are not siblings | Run inside this repo root (skill source tree), or check the symlink resolves here |
| Layer-1 extension not in the live tool list | `~/.pi/agent/extensions/skill-memory.ts` is a stale copy or a broken symlink | The live file must be a symlink into `layer1/extension/`; fix the link, then `/reload` (or restart) — never keep a second copy |
| `bin/triage` shows every skill as `review` / usage 0 | No telemetry is being written: the extension predates the telemetry change, or `PI_SKILL_TELEMETRY=0` | `/reload` the extension, then confirm the JSONL exists at `PI_SKILL_TELEMETRY_PATH` (default `~/.local/share/skill-evolution/skill-usage.jsonl`); `skill_loaded` events need a `skill_search(action=load)` call, `skill_read` events need a `read` of a `SKILL.md` |
| Triage ranks a skill you never touched | The name was parsed from a path, not from a real skill dir | Check the raw events (`cat` the JSONL) — the reader is deliberately dumb about provenance, the writer is strict |
| Candidate beats the baseline but a command in it does not run | Lint passed; the proxy metric cannot execute anything | Run `bin/smoke-candidate <candidate>` (or the run with `--smoke`): a command like `date -d "2024-03-15" +%V / %G` fails with `extra operand '/'`. Reject the candidate |
| Candidate scores higher but only adds a ritual (a fixed opening sentence, "list the command and its output") and deletes worked examples | The keyword-overlap proxy is being gamed, not the skill being improved | Re-score the baseline, the candidate and a ritual-free version: if only the ritual version wins, the gain is an artifact — drop it and fix the metric |
| The day file has the same digest section twice | `session_shutdown` fired twice for one session | The extension guards with a per-session flag (fixed); if you see it, the extension predates the guard — `/reload` it |
| Baseline `size_limit: exceeded` (e.g. 25KB) | Target skill genuinely over the 15KB cap | Evolve anyway (shrink is the goal) but expect deploy-gate friction; or set `PI_EVOLUTION_MAX_SKILL` to size the target honestly |
| Evolved candidate fails `test_smoke.py` despite lint pass | Skill-forge §0.8 lesson: lint is necessary, not sufficient | Gate the candidate, not the delta — run the target skill's own structural tests on the evolve branch before merge-or-drop |
