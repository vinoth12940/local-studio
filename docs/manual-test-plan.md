# Manual test plan for the agent-harness work

Automated suites cover the logic (every one passes in `./macos/build.sh`). This plan is only what **a person has to try**:
the real UI feel, and everything that needs your actual model. Every new feature is **off by default**; your installed app
currently has only `coding_workflow` on, so nothing below is active until you switch it on.

## Where things stand

| Area | State |
| --- | --- |
| Tasks, Plan/Act/Auto, durable runs and recovery, verification gate, hooks and test protection, nested instructions, skills, reviews, triggers, isolated workers, eval kit | Built, behind default-off flags, suites pass, full build passes |
| Real-model behavior of any of the above | **Tested once by me** against LFM2.5 24B-A2B in the installed app (results below); worth repeating by you with the model you actually use |
| Measured eval baseline | **Measured once** (LFM2.5 24B-A2B, 3 runs per task, 20/24 pass); committed at `macos/evals/baselines/lfm2.5-24b-a2b.json`, with the 0.15 regression threshold agreed |
| Unattended trigger execution | **Not built on purpose.** It is a policy decision |
| Independent review of this work | **Pending**; the roadmap's accepted count is 0/30 |

## Start here: what to test in the installed app (about 30 minutes)

Everything below was already verified by me through the app's HTTP API with a real model, but **not through the app window itself**.
These are the checks that need your eyes. Open `/Applications/Local Studio.app` normally. All new features are off until you switch
them on; to undo anything, untick it again.

Make a throwaway repo first (do not use a project you care about):

```bash
mkdir -p ~/ls-scratch && cd ~/ls-scratch && git init -q && printf 'x = 1\n' > value.py && printf "from pathlib import Path\nassert Path('value.py').read_text().strip() == 'x = 2'\nprint('check passed')\n" > verify.py && git add . && git -c user.name=t -c user.email=t@example.com commit -qm base
```

| # | Where | Do this | You should see |
| --- | --- | --- | --- |
| 1 | Chat | Open an existing chat and send a message | Works exactly as before; the context meter under the composer shows the default 131,072 (or the model's own limit if lower) |
| 2 | Settings → Context window | Pick 196,608 (the minimum is 100,000), Save, then send a message | A toast, a summary saying the engine "restarts with the new size on your next message" (and "for the active model" if the model's own limit is lower), a model reload pause, then the meter shows the effective size. Reset returns it to 131,072 |
| 3 | Code, workspace `~/ls-scratch` | Switch to **Accept edits** (Shift+Tab) and ask: "create a.txt here, and also create ../outside-test.txt" | `a.txt` is created with no prompt. The outside file asks you, saying "outside the workspace, so Accept edits does not apply". Decline it |
| 4 | Code → Workflow | Turn on **Required checks** and **Save feature settings**; add the check `tests: python3 verify.py` and Save. Ask: "change x to 2 in value.py and run python3 verify.py" | The reply shows "Configured checks passed for this turn". Then ask for a change but decline the check: it must say **Verification incomplete**, never "done" |
| 5 | Workflow | Turn on **Plan / Act / Auto**, create a task in **Plan**, ask for a change | You get a written plan, the file is unchanged, and the task ends "planned". Switch to **Act** and ask again |
| 6 | Workflow | Tick "Require an independent review", run the task to verified, click **Review** | Findings or an explicit "no findings" (about 30 s with LFM2.5). You must record a reason for each blocking finding. Editing the code afterwards makes the review "not current" |
| 7 | Workflow | Turn on **Scheduled tasks**, create an "every 1 minute" one, wait | Within about a minute the status line shows "1 scheduled ready" without opening the dialog. **Start with approvals** runs it and still asks for each approval |
| 8 | Workflow | Turn on **Isolated write workers**, create a worker (scope `NOTES.md`), point a Code chat at its checkout path, ask for `NOTES.md`, then **Review changes** and Apply | Your repo is untouched until Apply; applied as an uncommitted file; no commit is made |
| 9 | Workflow → Restart recovery | Start a multi-step task and force-quit the app mid-run, reopen, open "Saved coding runs" | The run shows interrupted; "Continue with fresh approvals" finishes without repeating completed steps |

Optional and advanced: hooks and test protection are configured in `settings.json` (see `docs/workflow-automation.md`). To re-measure
the model, run `macos/evals/run_evals.py` with `--baseline macos/evals/baselines/lfm2.5-24b-a2b.json` (the 0.15 threshold is agreed).

Things that look odd but are expected:
- A worker's own chat can end "Unverified: no trusted verification commands" when the global gate is on, because its checkout is not a configured workspace. It does not affect integration.
- With a nearly full context window, a thinking model may end with "The reply was cut off before it produced any text". Raise the window or run `/compact`.
- Part B below shows what I already ran and what it found.

## Where the build is

Build and install the current source with `./macos/build.sh --install` (quit Local Studio first). The version and build
number are in Settings and in `CHANGELOG.md`.

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

### What I ran in Part B (2026-10-23, installed build, LFM2.5 24B-A2B, scratch repos)

The real model found **eight bugs that every offline suite had missed**; all are fixed, tested, and re-verified with the real model.

| # | Result | What the real run showed |
| --- | --- | --- |
| B1 | Pass after a fix | The gate never accepted a false "done". But the model always wrote `cd <workspace> && python3 verify.py`, which the gate did not count, so it could never reach "verified". A narrow normalizer now accepts exactly that prefix (and nothing chained after it) |
| B2 | Pass after a fix | Act and Auto reach `verified_complete`; Auto without a task is refused. Plan mode blocked every edit but the model retried ~10 times and returned an empty answer; the guidance now says what Plan mode means, and it returned a real plan with 0 blocked attempts |
| B3 | Pass | `kill -9` mid-run, restart: run `interrupted`, resume re-asked approvals, only the remaining steps ran, no duplicates |
| B4 | Pass after two fixes | The review called the engine's base URL (404), then ran out of its fixed token budget while reasoning. It now sizes the budget to the context window; a real review completed in about 30 s and the task reached `verified_complete` only with a current review, then went stale on a source edit |
| B5 | Pass | The schedule fired about 60 s after creation and queued without touching the repo; Start carried no pre-approval; the run synced to `completed` with a notice |
| B6 | Pass after a fix | Worker checkouts lived where the fingerprint guard rejects workspaces, so no chat could start in one. Moved to `data/worktrees`. Then: edit in checkout, parent untouched, integrate uncommitted, 4 steps charged, completion gated on fresh checks |
| B7 | Pass after a fix | Protected test edits, shell writes and hook-denied names were all refused with reasons. Test protection had the same `cd` prefix problem as B1 (the model could not run its check); fixed together |
| B8 | Pass after a fix | Rule provenance shown per tool step and the nearer rule followed. `load_skill` rejected a first-load digest with a misleading error and the model gave up; the error and tool description are now actionable and the model recovered |
| B9 | Measured | See below |

**Eval baseline (LFM2.5 24B-A2B, 8 tasks x 3 runs).** First measurement 13/24 (54%). The measurement itself exposed three harness bugs, now fixed: declining an action never told the model to stop (denied-shell went 0/3 to 3/3); a long chat left about 512 tokens for the reply so thinking models answered nothing; and eviction left no room for the checkpoint summary, so an early fact vanished without a trace (compaction went 0/3 to 3/3). Final: **20/24 (83%)**. Still failing, which is real signal about the model: `approval-edit-declined` 1/3 and `repair-invalid-json` 1/3 (it keeps calling tools instead of stopping or finishing).

**Fixed afterwards: accept-edits no longer approves writes outside the workspace.** `write_file` was not confined to the workspace (`../x` and absolute paths worked) and accept-edits auto-approved them. Now accept-edits applies only to files inside the workspace (symlinks out and `../` included); anything else still asks, with the reason shown. Your explicit "allow edits for this chat" is unchanged.

**New: Settings → Context window.** Choose the maximum context window (100,000 to 262,144 tokens; default 131,072). A model with a smaller native limit runs at its own limit and Settings says so. The engine restarts with the new size on your next message. To test: set 196,608, send a message, and confirm the context meter under the composer shows the effective size after the engine reloads; then Reset.

**Fixed after the "tools and inference are worse" report (each has an automated regression test):**
- Long thinking turns no longer overflow the window: replayed reasoning is counted, trimmed per round, and dropped for older rounds first. Test: in Code, ask for a multi-step task on a thinking model and confirm it finishes instead of stopping with an empty or cut-off reply.
- Window defaults to 128k with a 100k floor and is clamped per model (a 131,072-token model is never started at 262,144).
- Durable runs and the verification gate now work in this repo's own checkout (the fingerprint follows `.gitignore`, hashes inside symlinks by target, skips sockets) and no longer freeze the stream while hashing.
- A request that fails before it starts (for example Auto with no task) no longer leaves a permanent "Thinking…" turn.
- With `protect_tests`/`hooks` on, a chat that has no workspace is no longer treated as a coding session rooted at home or the checkout.
- The Workflow task dropdown no longer closes while you are choosing (it is rewritten only when its contents change).
- Image generation checks memory first: with a big chat model loaded it asks "Stop model and generate?" instead of swapping. To test: load a large model, open Images, generate; you should get that prompt, and declining leaves the model loaded.

### What I could not test
- Any behavior with a real model: review quality, whether the model follows resume/Plan/Auto/rule instructions, and how often
  it passes the eval tasks.
- Hooks and test protection under the macOS sandbox on your machine beyond the automated suites.
- OS-level notifications (none exist), and a crash of the real app during a real run.
- The WKWebView app UI. I checked the same UI in a browser against the isolated server.

Anything surprising: note the exact steps, then check `logs/ui.log` and the run record under
`data/workflow/runs/` in your state folder. Those files hold what the harness believed happened.
