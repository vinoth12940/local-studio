# Agent-harness evaluations

Real-model tasks that measure whether a local model, driven through Local Studio's chat agent harness, actually finishes
coding, repair, refusal/approval and long-context work. The offline suites prove the harness *mechanics*; this measures the
*model*. Everything here is deterministic to score (no model-graded results) and isolated from your app.

## What it will and will not do

- It runs the harness **in-process** against a **throwaway `LOCAL_STUDIO_HOME`** and fresh copies of each fixture. It refuses
  to start against the live app state.
- It **never starts, stops or switches an engine.** `--attach` sets `LOCAL_STUDIO_EVAL_ATTACH=1`, under which
  `ensure_engine` only checks that the engine you already loaded is ready. Engine processes are matched machine-wide, so
  anything else would take the running app's model away. If no engine is ready, tasks are reported **blocked**, not failed.
- The model is shared. Load it in Local Studio first and leave the app idle while an evaluation runs.

## Commands

```sh
uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/evals/run_evals.py --validate
uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/evals/run_evals.py --scripted
uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/evals/run_evals.py \
  --attach --model <chat model id> --repeat 3 --out build/evals/baseline
```

`--scripted` replaces the model with each task's reference script. It exists to prove the tasks are solvable and the checks
discriminate (`macos/check_evals.py` also runs every task's *negative* script and requires it to fail). A scripted report
measures no model and can never be a baseline or compared against one. A scripted turn may carry `"delay"` (seconds before
its reply) or `"until_interrupted": true` (recovery tasks: hold the reply until the run is cancelled, then serve it again
after the resume), so an interruption lands between tool calls on any machine rather than racing a timer.

Add `--baseline build/evals/baseline/report.json --max-drop 0.15` to fail on regressions, `--task <id>` to select tasks,
`--allow-model-mismatch` to proceed when the engine reports a different model than `--model` (by default that **refuses**, because results would be scored under the wrong name; an unverifiable model is warned about and recorded as `model_check` in the report), and `--seed N` / `--temperature T` to forward sampling settings in the chat body (an engine that ignores a seed is not
detected; repeats are treated as measuring natural variance).

## Tasks

`tasks/<id>.json` (schema version 1, `revision` bumped whenever a task or fixture changes) names a `fixtures/` directory
that is copied for every run, a prompt, an approval policy (`allow_all`, `deny_all`, `edits_only`), budgets
(`max_tool_calls`, `timeout_seconds`), harness `flags`, and declarative **outcome checks** (`command`, `file_contains`,
`file_unchanged`, `final_text_excludes`, `max_tool_calls`, …). A run passes only if every check passes and the tool budget
holds. A turn that raised an error or produced no output is never scored as a pass.

| Category | Tasks |
| --- | --- |
| coding | `coding-add-function` |
| repair | `repair-failing-test` (fix the code, tests stay untouched), `repair-invalid-json` |
| refusal_approval | `refusal-credential-file`, `approval-denied-shell`, `approval-edit-declined` |
| compaction | `compaction-recall` (an early fact must survive context eviction; revision 2 checks only the recalled answer, since revision 1 also required writing a file and that conflated recall with choosing the right directory) |
| recovery | `recovery-resume-without-replay` (a real durable run is interrupted after a completed step, resumed through the resume API, and must finish without replaying side effects) |

## Reports

`report.json`, `report.md` and `runs/<n>-<task>-<status>/` (events, scored workspace, run record). A report records the model
id and catalog entry, the model the engine says it serves, app version and git commit (and whether the tree was dirty),
Python and package versions, hardware, harness flags, sampling, task-set revision (a hash of every task and fixture), latency
and tool-failure counts per task. Per-task **pass counts** are over measured runs only; `blocked` and `skipped` runs are listed
with their reasons and excluded from success.

## Status

The kit and its offline proof are complete, including a recovery task and engine runtime versions in every report.

A first real baseline exists for **LFM2.5 24B-A2B (4-bit MLX), 3 runs per task, 20 of 24 passing (83%)**, committed at
`baselines/lfm2.5-24b-a2b.json`. Compare a later run against it with
`run_evals.py --attach --model <id> --baseline macos/evals/baselines/lfm2.5-24b-a2b.json --max-drop 0.15`. The 0.15 tolerance is the
agreed regression threshold (agreed by the owner on 2026-10-23), and a baseline measured under a different task revision is refused as not comparable. With three
runs per task and a sampling model, one run flipping is within noise; repeat more before treating a single drop as a regression.
Reports from `--scripted` runs never count as a baseline.
