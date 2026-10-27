# Workflow tasks, reviews, triggers and isolated workers

All of this is opt-in in `$LOCAL_STUDIO_HOME/data/settings.json` under `harness`, or from the **Workflow** dialog in
the Code view. Every flag defaults to `false` and none of it changes ordinary chat. Nothing here grants edit, shell, MCP
or network permission: every action still passes the existing approval gates, path fences and test protection.

| Flag | Needs | What it turns on |
| --- | --- | --- |
| `workflow` | | Durable coding tasks with Plan / Act / Auto modes and versioned `intent.md`, `spec.md`, `plan.md` |
| `durable_runs` | `workflow` | Restart-safe run journal; unknown tool outcomes need a person ([below](#durable-runs-harnessdurable_runs)) |
| `hooks` | | Configured `PreToolUse` / `PostToolUse` executables ([below](#tool-hooks-and-test-protection)) |
| `protect_tests` | | Refuse tool edits to test files and all opaque writes ([below](#tool-hooks-and-test-protection)) |
| `review` | `workflow` | Independent review of a task's diff, with explicit finding dispositions |
| `triggers` | `workflow`, `durable_runs` | Schedules and authenticated external calls that **queue** work |
| `worktrees` | `workflow` | Isolated Git checkouts for scoped write workers |

`verification_gate`, `verification_fingerprints`, `nested_instructions` and `skills` are documented in
[verification-gate.md](verification-gate.md) and [harness-resources.md](harness-resources.md).

Tasks and modes live in `studio/services/workflow.py`. This page documents durable runs, hooks and test protection, and then
reviews, triggers and workers, which are driven by explicit local-user controls only. **None of those three is a model tool.**

## Durable runs (`harness.durable_runs`)

With the flag on (or whenever a chat turn belongs to a workflow task), each coding turn is journaled under
`$LOCAL_STUDIO_HOME/data/workflow/runs/<run-id>.json`, so a crash, restart or lost connection leaves an inspectable record.

- **Storage.** One JSON file per run, written atomically, guarded by a per-run lock and an exclusive `.<id>.active` lease so
  only one executor owns a run. A record holds the schema version, a monotonic event `seq`, the messages, each tool step
  (`dispatched` → `completed` / `failed` / `unknown_outcome` / `reconciled`), any pending approval, the workspace
  fingerprint, the trusted-policy digest, the verification snapshot and the terminal outcome. A run's id is also its
  idempotency anchor: a `workflow_request_key` maps to exactly one run.
- **Workspace fingerprint.** A bounded content hash. In a git checkout it covers the tracked and not-ignored files (so `.gitignore`
  decides what is source); elsewhere it skips well-known build folders (`node_modules`, `__pycache__`, caches, virtualenvs found by
  `pyvenv.cfg`, `.DS_Store`). Symlinks are hashed by target and never followed; one pointing outside the workspace fails closed with an
  error that names it (exclude it with trusted `fingerprint_excludes`). Sockets, pipes and devices are skipped. Budgets still fail closed.
  Hashing runs off the event loop, and a cancelled run is seen by the executing turn through an in-memory flag, not by re-reading its record.
- **Limits.** A record is capped at 16 MB (past that, no further tool may execute) and keeps at most the newest 4,000 events;
  `first_seq` advances and a replay cursor older than that gets an explicit "expired" error rather than a silent gap.
- **Recovery.** A run found `running` or `waiting_approval` with no live lease becomes `interrupted`, or
  `needs_reconciliation` if a tool was dispatched without a recorded result. **Nothing is re-executed automatically.** An
  unresolved shell, MCP or external action must be reconciled by a person (`executed`, `not_executed` or `abandon`, with the
  evidence they inspected). Resuming requires the expected `seq`, the same trusted-configuration digest and the same workspace
  fingerprint (or an explicit digest the person accepts), and every approval is asked again.
- **Migration.** Only `schema_version` 1 is understood. A record with another version, or any malformed field, is
  **quarantined**: renamed `<id>.corrupt-<8 hex>` and reported as a conflict. It is never migrated or silently recreated. A future schema
  must add an explicit migration and bump the version; until then old files simply remain readable.
- **Retention.** Nothing is deleted automatically. `POST /api/workflow/runs/prune` with `{"older_than_days": 1..3650,
  "keep_latest": 0..10000}` removes finished runs (`verified_complete`, `complete`, `planned`, `cancelled`, `abandoned`,
  `failed`, `incomplete`) older than the cutoff, beyond the newest `keep_latest`, and policy audit files
  (`data/workflow/audit/`) older than the cutoff. It **never** removes `needs_user`, `needs_reconciliation`, `interrupted`, live
  or quarantined runs, and it does not touch tasks, reviews, triggers or worker records.
- **Audit.** Each policy session also writes `data/workflow/audit/<id>.json` (at most 1,000 events: tool allowed/denied, hook
  outcomes), mirrored into the run journal when one exists. With `security.redact` on, payloads are redacted first.

## Tool hooks and test protection

Both are configured in `$LOCAL_STUDIO_HOME/data/settings.json` under `harness.workspace_policies`, keyed by the workspace's
absolute, canonical path, and switched on by `harness.hooks` / `harness.protect_tests`. The request and the model cannot
define or change them.

```json
{
  "harness": {
    "hooks": true,
    "protect_tests": true,
    "workspace_policies": {
      "/absolute/path/to/project": {
        "hooks": {"PreToolUse": [{"id": "lint-gate", "argv": ["/usr/local/bin/my-hook", "--strict"],
                                  "tools": ["write_file", "edit_file"], "timeout_seconds": 5}]},
        "protected_tests": ["tests/**", "**/test_*.py"],
        "test_exceptions": {"tests/fixtures/golden.json": "Regenerated by the release script"}
      }
    }
  }
}
```

**Hooks.** At most 16 per phase; each needs a distinct `id` and an absolute executable path (credential paths are refused);
`tools` is a list of name patterns (default every tool); `timeout_seconds` is 1–10 (default 5). A hook receives one JSON event in
`LOCAL_STUDIO_HOOK_EVENT` (phase, tool, arguments, the result for `PostToolUse`, workspace, policy digest; at most 20,000
characters) and a minimal environment, runs with the project directory read-only where the kernel sandbox is available, and answers with
a JSON object on stdout: `{"allow": true|false, "reason": "..."}`.

- **Order and failure.** Hooks run in declared order and the first denial stops the chain. A non-zero exit, invalid output, a
  timeout or a launch error **fails closed** and is audited.
- **Authority.** `PreToolUse` hooks run after the application's own checks (path fence, workspace boundary, task scope, test
  protection), so a hook can only add restrictions, never lift one, and it cannot grant an approval. `PostToolUse` runs after the
  action: if it refuses, the result carries `post_hook_failed` and `operation_executed` so nobody mistakes the action for undone.
- **Plan mode.** Executable hooks never run in Plan mode.

**Test protection.** Native `write_file`, `edit_file` and `multi_edit_file` refuse any path matching `protected_tests` (default:
`tests/`, `test_*.py`, `*_test.py`, `*.test.*`, `*.spec.*`, `macos/check_*`) unless the exact relative path has an entry in
`test_exceptions` with a recorded reason. Patterns use `fnmatch` rules, so `*` also matches `/`. Because a shell, Python snippet,
MCP tool, computer-use run or delegated write could change a test file in ways a path check cannot see, **all opaque tools are
refused outright** while protection is on, except configured foreground verification commands (see
[verification-gate.md](verification-gate.md)), which run with the project directory read-only.

Honest boundaries:

- Protection covers *tool actions taken by the agent*. It does not stop you or another process editing files.
- The read-only guarantee for verification commands and hooks relies on the macOS sandbox (`sandbox-exec`). Where it is
  unavailable those commands are refused (fail closed) rather than run unprotected.
- Hook and verification executables are trusted code. Keep them outside the workspace if the agent must not be able to edit them.
- A verification command can still read the whole project and write outside it (for example to a scratch directory).

## Independent reviews (`harness.review`)

`POST /api/workflow/tasks/{id}/review` with `{expected_version, verification_run_id}` runs one review of the task's
actual `git diff HEAD` against its intent/spec/plan, using the already-loaded local model in a fresh conversation with no
tools. It never loads or switches a model: if the selected model is not running, the review is recorded as incomplete. The reviewer gets all the context window leaves, up to 14,000 tokens, because a reasoning model spends many of them thinking before it answers; a reply cut off by that budget is refused with an explicit reason, never recorded as a review.

- **Evidence is pinned.** The review binds the source fingerprint, diff digest, instruction fingerprint and a *backend*
  verification snapshot taken from a finished run of the same task. A failed, skipped, stale or foreign verification run
  is recorded as an incomplete review and no model call is made.
- **Strict output.** One JSON object: `complete`, `summary`, `findings[]` (`id`, `title`, `severity`, `path`, `line`, `side`,
  `evidence`). Paths and lines must exist in the supplied sources. The reviewer cannot assign a disposition, accept risk or
  grant a permission; extra fields are rejected. An empty `findings` list is valid only with `complete: true` and a summary.
- **Dispositions are the user's.** `PATCH /api/workflow/tasks/{id}/review/findings/{finding}` records `open`,
  `fix_requested`, `rejected` or `accepted_risk` with a required reason. `critical`, `high` and `medium` findings block
  completion until they are `rejected` or `accepted_risk`; `fix_requested` still blocks.
- **`REVIEW.md` is a projection.** Authoritative state lives in the task record. Editing the markdown waives nothing and
  makes the task's artifacts read as changed until the review is regenerated.
- **Completion.** A task created with `review_required` completes only when its review is complete, current for the
  workspace, diff and instructions, has no unresolved blockers, and the completion turn's own verification is current.
  Changing the source after a review makes it stale. Starting a review while the task runs is refused.

Limits: the reviewer is the same local model that may have written the code, in a clean context; that is isolation of
context, not an independent party. Reviews are only as good as that model, and the stub-backed tests do not measure that.

## Triggers and schedules (`harness.triggers`)

A trigger belongs to one existing task and one workspace and carries an instruction. It **queues** an invocation;
**it never runs it.** Nothing executes until a person presses *Start with approvals*, which opens an ordinary chat turn
on the task, keeping its mode, scope and budgets and every approval. A trigger can neither widen permissions nor act while
nobody is there to approve. Unattended execution is a policy decision this feature deliberately does not make; it would
need an explicit design and approval policy first.

- **Schedules.** `null` (external calls only), `{"type":"interval","every_seconds":60..604800}` or
  `{"type":"daily","at":"HH:MM"}` in the Mac's local time. There is no background thread: schedules are evaluated whenever
  the app asks (opening the dialog, polling for notices), which is sufficient because a queued invocation matters only when
  someone is there to start it.
- **Missed runs.** `skip` (default) fires only a slot reached within two minutes of its time; `queue_one` queues a single
  catch-up for the newest missed slot. Counters record every miss. Pausing and re-enabling never replays missed slots.
- **Overlap.** At most one invocation per trigger is `pending` or `started`; a slot arriving meanwhile is recorded as
  `skipped` (overlap), never stacked.
- **Idempotency.** Each slot has a stable key; external calls take a caller-supplied `idempotency_key` (8–128 characters).
  Redelivery returns the original invocation. A started invocation also reuses the key as the durable run's request key.
- **Clock changes.** If the clock moves backwards the schedule re-anchors and fires nothing. A large forward jump follows the
  missed-run policy.
- **Restart.** A start that never produced a recorded run within ten minutes becomes `interrupted` with a notice. It is never
  re-dispatched automatically. Run outcomes (`completed`, `failed`, `needs_user`, `cancelled`) are read from the durable run
  record, so a declined approval, exhausted budget or unknown tool outcome shows as **needs your attention**.
- **External calls.** `POST /api/agent/trigger` with `Authorization: Bearer <token>` and
  `{"trigger_id", "idempotency_key"}` queues one invocation and replies `runs_unattended: false`. The per-trigger token is
  shown once at creation and only its SHA-256 is stored. Unknown ids and wrong tokens give the same 401. Local-only routing
  and `security.api_token` apply on top.
- **Controls.** Create, pause/resume, delete (refused while a started run is active), start, cancel and dismiss notices are
  all in the dialog. Cancelling a started invocation also cancels its durable run.

Limits: no OS notification or menu-bar alert is sent; notices appear in the dialog and are returned by
`GET /api/workflow/notifications`. There is no cron syntax.

## Isolated write workers (`harness.worktrees`)

A worker is a separate Git worktree and branch of the task's repository, checked out under `$LOCAL_STUDIO_HOME/data/worktrees/<id>/tree` (deliberately not under `data/workflow`, the run journal's own folder, which cannot be a fingerprinted workspace). Your workspace is not written until you review
the exact patch and integrate it by digest. **No model tool creates or drives a worker**; the worker is an ordinary Code chat
that you open on its checkout path, so it keeps its own approvals, fences, budgets and task scope. `delegate_write` remains
an opaque, blocked tool under test protection.

- **No privilege gain.** A worker's file scope must lie inside the parent task's scope; its budgets must fit the parent's
  remaining steps and seconds, summed over active workers; the task's `max_children` caps how many are active.
- **Write ownership.** Active workers on a workspace must have disjoint scopes. Glob intersection cannot be decided in
  general, so overlap and containment use a conservative probe that is exact for directory-style scopes (`src/**`).
- **Review then integrate.** `GET …/integration` snapshots the worker's changes on its own branch and returns the patch,
  its digest and every blocking problem. `POST …/integrate` with `expected_digest` applies *that* patch to the working tree
  as **uncommitted changes**; it never commits. A changed worker, a digest mismatch, an out-of-scope file, a symlink or
  `.git` path, uncommitted user changes to any touched file, or a moved base that touches the same files all refuse.
  A moved base that touches different files is allowed if the patch applies cleanly.
- **After integration, completion is gated.** Integration records it on the parent task. A task that has had a worker integrated
  completes only when the completion turn's configured checks ran *after* the integration and against exactly the current
  workspace content (their evidence is fingerprint-bound; without durable runs or `harness.verification_fingerprints` the
  evidence cannot be bound and completion is refused). A `review_required` task also needs a fresh review, because the
  review goes stale when the content changes.
- **Cancellation.** Pausing or cancelling the parent task cancels every active worker and any durable run in its checkout, and
  keeps the work as a saved patch.
- **Cleanup is recoverable.** Cleanup refuses an active worker and a dirty checkout without `force`, always saves the patch
  first, and keeps the branch unless `delete_branch` is set. Missing checkouts and unregistered directories are reported.
- **Runs and budgets.** Runs recorded in a worker's checkout are listed under that worker. Their tool steps are charged to the
  parent task once they finish, and a worker that used more steps than it was allocated cannot be integrated. Accounting is after
  the fact: a worker chat has no task of its own, so overspending is reported and blocks integration but is not interrupted mid-run.

Limits: workers are not spawned by the model (you open a Code chat on the worker's path), and the glob-based scope checks are
conservative approximations for anything beyond directory-style patterns.

## Verification

Each suite uses throwaway state and repositories under `/tmp`, injected clocks and stub models, and never contacts an
engine. Run them with the pinned backend dependencies:

```sh
TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 uv run -q --no-project --python 3.12 \
  --with-requirements requirements-app.txt python macos/check_workflow_reviews.py
# likewise: check_workflow_triggers.py, check_workflow_worktrees.py, check_evals.py
```

`macos/build.sh` runs all of them. See [macos/evals/README.md](../macos/evals/README.md) for the real-model evaluation kit.
