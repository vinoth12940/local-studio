# Manual test plan for the agent-harness work

Automated suites cover the logic (every one passes in `./macos/build.sh`). This plan is only what **a person has to try**:
the real UI feel, and everything that needs your actual model. Every new feature is **off by default**; your installed app
currently has only `coding_workflow` on, so nothing below is active until you switch it on.

## Where things stand

| Area | State |
| --- | --- |
| Tasks, Plan/Act/Auto, durable runs and recovery, verification gate, hooks and test protection, nested instructions, skills, reviews, triggers, isolated workers, eval kit | Built, behind default-off flags, suites pass, full build passes |
| Real-model behavior of any of the above | **Not tested by me** (no engine was started). This is what Part B is for |
| Measured eval baseline | **Pending, needs you** (Part B, B9) |
| Unattended trigger execution | **Not built on purpose.** It is a policy decision |
| Independent review of this work | **Pending**; the roadmap's accepted count is 0/30 |
| Not committed | 56 changed files sit uncommitted on `codex/p6-search-release` |

## Where the build is

`build/Local Studio.app` (1.5.0, build 132, signed) is current with the source. It is **not installed**; your installed app is
untouched. Installing replaces it and is your call (`./macos/build.sh --install`).

---

## Part A: no model needed, safe (isolated demo, never touches your app)

This uses a throwaway state under `build/agent-ui-state`, a stubbed reviewer and a small Git repo. **Do not press "Start with
approvals" here.** That sends a real chat, and the dev server shares engine ports with your running app.

1. Seed and start (two terminal commands, from the repo root):

   ```bash
   LOCAL_STUDIO_HOME=build/agent-ui-state uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/seed_workflow_demo.py
   ```

   ```bash
   LOCAL_STUDIO_HOME=build/agent-ui-state LOCAL_STUDIO_SEARCH_PORT=8093 uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt uvicorn app:app --host 127.0.0.1 --port 7861
   ```

2. Open <http://127.0.0.1:7861>, go to **Code**, set the workspace to the `workspace` path the seeder printed, then click
   **Workflow**. If the workspace picker is awkward in a browser, paste this in the browser console (fill in the two values the
   seeder printed):

   ```js
   LS.showView('code'); LS.codeWorkspace = '<workspace>';
   LS.current = {id:'a1b2c3d4e5f60718', title:'Demo', workspace:'<workspace>', workflow_task_id:'<task>', messages:[]};
   await LS.refreshWorkflow(); await LS.openWorkflow();
   ```

| # | Do this | You should see |
| --- | --- | --- |
| A1 | Open the dialog | Feature switches (all on in the demo), required check, task artifacts, then review, scheduled tasks, isolated workers, runs |
| A2 | Under the review, click **Record decision** on F1 with no reason | A red error asking for a supported disposition and a reason; nothing changes |
| A3 | Choose "Fix requested", type a reason, **Record decision** | F1 shows `fix_requested` and your reason. It still says **blocks completion** |
| A4 | Choose "Accept risk" with a reason | F1 shows `accepted_risk` and **blocks completion** disappears: a decision of yours, recorded with its reason |
| A5 | Click **Pause** on "Nightly dependency check" | It reads "paused" and the button becomes **Resume** |
| A6 | See "From CI" | It shows **ready to start** with Start/Cancel. Click **Cancel** | It becomes "cancelled" and disappears from "ready" |
| A7 | Deliver a trigger from outside (commands below) | `ci-delivery-0001` (the seeded key) returns the existing **pending** invocation (redelivery is harmless); any new key while one is pending is recorded **skipped (overlap)**, and every reply says `runs_unattended: false` |
| A8 | Under Isolated workers click **Review changes** | **Blocked**: "The worker made no changes"; the Apply button is disabled |
| A9 | Create `NOTES.md` in the worker's checkout (command below), click **Review changes** again | The patch preview and file list (`NOTES.md`) appear; Apply is enabled. (The demo's uncommitted `src.py` is a different file, so it does not conflict) |
| A10 | Click **Apply this patch to my workspace (uncommitted)** | A toast says applied, not committed. `git -C <workspace> status` shows `?? NOTES.md` (plus the demo's own ` M src.py`) and `git log` still has one commit |
| A11 | Re-run the seeder, then create `NOTES.md` in **both** the worker's checkout and the main workspace by hand *before* reviewing | **Blocked**: "Uncommitted changes in the main workspace would be overwritten". Your own file is untouched |
| A12 | Stop the server with Ctrl-C, start it again | Everything above is still there (durable state). The worker, trigger and decisions persist |

Commands for A7, A9, and cleanup:

```bash
curl -s http://127.0.0.1:7861/api/workflow/triggers | python3 -m json.tool | grep -E '"id"|"name"'
curl -s -X POST http://127.0.0.1:7861/api/agent/trigger -H 'Content-Type: application/json' -H 'Authorization: Bearer <token printed by the seeder>' -d '{"trigger_id":"<id of From CI>","idempotency_key":"ci-delivery-0001"}'
echo '# Notes from the worker' > "<worker_checkout printed by the seeder>/NOTES.md"
curl -s -X POST http://127.0.0.1:7861/api/workflow/runs/prune -H 'Content-Type: application/json' -d '{"older_than_days":30,"keep_latest":50}'
```

The last command should report nothing removed (the demo run is fresh), which is itself the check that prune is conservative.
Re-run the seeder any time to reset the demo (it prints a new token each time). I walked A7 to A11 and the prune call through against
this exact setup before writing them down.

---

## Part B: needs your real model (do this in the real app, on a scratch repo, not a project you care about)

Turn features on in **Code → Workflow → feature switches → Save**. Turn them off the same way to roll back; they are all
off by default and nothing else changes. Create a throwaway Git repo with a small Python file and one passing check, and
add the check under "Required checks" (`tests: python3 -m unittest -q`).

| # | Feature (switch) | Do this | Pass looks like |
| --- | --- | --- | --- |
| B1 | Required checks (`verification_gate`) | Ask the model to change a file and say "just tell me it's done". Decline running the check | The reply is **Unverified**, never "done"; badge reads "Verification incomplete". Accept the check next time: "Configured checks passed" |
| B2 | Plan / Act / Auto (`workflow`) | Create a task, set **Plan**, ask for a change | It plans, cannot edit anything, and the task ends `planned`. Switch to **Act**; edits now ask approval. **Auto** needs a task and ends verified or incomplete, never an unsupported success |
| B3 | Restart recovery (`durable_runs`) | Start a multi-step task, force-quit the app mid-run, reopen, open **Saved coding runs** | The run shows as interrupted. "Continue with fresh approvals" resumes **without repeating** finished steps; an unknown step asks you to inspect and decide |
| B4 | Independent reviews (`review`) | On a `review_required` task: run checks to verified, click **Review** with your model loaded | Findings appear with file/line; high/medium block until you decide. No model loaded gives an incomplete review, not a fake one. **This is the first real-model review ever run** |
| B5 | Scheduled tasks (`triggers`) | Create an every-1-minute task; wait; watch the status line | "1 scheduled ready" appears without opening the dialog. **Start with approvals** opens a normal chat that still asks for every approval |
| B6 | Isolated workers (`worktrees`) | Create a worker, open a Code chat on its path, let the model edit, review and apply | Your workspace is untouched until Apply; applied as uncommitted; completion then demands fresh checks |
| B7 | Hooks and test protection (`hooks`, `protect_tests`) | Configure per `docs/workflow-automation.md`; ask the model to edit a test file, then to do it via a shell command | Both refused with a clear reason; a denying hook blocks the write; the configured check still runs |
| B8 | Scoped instructions and skills (`nested_instructions`, `skills`) | Put a different rule in `src/CLAUDE.md`; have the model edit under `src/` | The tool step has **Project rules applied** listing both files, nearest scope last. A skill under `.localstudio/skills/<name>/SKILL.md` is listed and loadable |
| B9 | **Eval baseline** | With the model loaded and the app idle: `run_evals.py --attach --model <id> --repeat 3 --out build/evals/baseline` (see `macos/evals/README.md`) | A report with per-task pass counts. Look at which tasks fail: that is real signal on your model. Then pick thresholds |

### What I could not test
- Any behavior with a real model: review quality, whether the model follows resume/Plan/Auto/rule instructions, and how often
  it passes the eval tasks.
- Hooks and test protection under the macOS sandbox on your machine beyond the automated suites.
- OS-level notifications (none exist), and a crash of the real app during a real run.
- The WKWebView app UI. I checked the same UI in a browser against the isolated server.

Anything surprising: note the exact steps, then check `logs/ui.log` and the run record under
`data/workflow/runs/` in your state folder. Those files hold what the harness believed happened.
