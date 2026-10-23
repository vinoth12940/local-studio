# Agent harness roadmap

Updated: **2026-10-23**. Implementation author: **Sol (`gpt-6.1-sol`)**. Architecture and acceptance advisor: **Astra (`gpt-6-astra`)**. These roles describe development of this repository; Local Studio's application runtime continues to use its configured local models.

**2026-10-23 continuation:** Claude (Sonnet 5.5) audited the uncommitted working tree against this tracker, finished the stalled milestones, and built M3, M8 and M9. **No Astra review was performed on any of that work**, so this document grants no independent acceptance: the accepted counts below stay at zero, and a separate tier records what is implemented and how well it was verified.

This tracker separates the delivered chat harness from the next stage of reliable coding-agent execution. The existing [Tier 1–3 plan](superpowers/plans/2026-10-17-ai-native-sdlc-agent-harness.md) remains the detailed historical specification for its eleven features. Its completion does not establish that the next-stage autonomous workflow is complete.

## Dashboard

| Scope | Status | Count | Text progress bar |
| --- | --- | --- | --- |
| Recorded Tier 1–3 baseline | Done in the existing tracker | 11/11 features | `[###########] 11/11` |
| Next-stage milestones, independently accepted | None: no independent review is recorded for any milestone | 0/10 milestones | `[----------] 0/10` |
| Next-stage criteria, independently accepted | None | 0/30 criteria | `[------------------------------] 0/30` |
| Criteria implemented, passing suite, criterion-to-test mapping **audited** (by Claude, 2026-10-23; not an independent review) | M1–M2, M4–M10 (3/3 each) and M3 (2/3) | 29/30 criteria | `[#############################-] 29/30` |
| Criteria not met | M3 criterion 3: measured baseline and thresholds (needs a real engine run) | 1/30 criteria | `[#-----------------------------] 1/30` |
| M1 initial per-turn gate slice | Done: implementation, review, browser, build | 6/6 slice items | `[######] 6/6` |

**What the tiers mean.** *Independently accepted* needs the review step in the validation workflow below, which has not happened for next-stage work. *Implemented, passing suite, mapping audited* means the criterion's behavior has a deterministic test that was read against the criterion text and the full build passed. The mapping audit read each criterion against the tests and fixed what it found missing (see the evidence log); it is the author's own check, not a second pair of eyes. None of these tiers is acceptance, and none measures model capability.

Bars count only explicitly checked items in their named scope; they do not estimate overall product readiness, difficulty, effort, or model capability. A milestone becomes Done only when all three of its acceptance criteria have evidence and review. Partial means implementation or acceptance has begun but the milestone is not accepted. Existing foundations are listed separately and earn no next-stage completion credit. The six initial-slice items below have their own fixed denominator and do not add to the thirty broad criteria or ten milestone completions.

The baseline definitions are carried forward from the existing tracker and checked against source locations. The final working-tree full build is recorded below; it does not establish next-stage milestone completion. Remaining next-stage scope is proposed work, not an approved delivery schedule.

## Delivered baseline: 11/11

- [x] **1.1** Workspace instruction files auto-load at the selected workspace root.
- [x] **1.2** Composer `@file` and `@folder` mentions expand workspace context.
- [x] **1.3** Post-edit syntax/lint diagnostics accompany supported file edits.
- [x] **1.4** Git status/diff tools and full-turn backup checkpoints support inspection and undo.
- [x] **1.5** A safe shell allowlist permits supported inspection and test commands without extra clicks.
- [x] **2.1** Custom MCP servers expose dynamic tools through the harness.
- [x] **2.2** `delegate_task` runs a worker with isolated conversation context.
- [x] **2.3** Explicit and automatic compaction preserve a conversation checkpoint.
- [x] **3.1** Background processes and live output are available through process management and SSE.
- [x] **3.2** Atomic multi-edit and symbol outline tools support code navigation and editing.
- [x] **3.3** Slash commands expand reusable prompts, including `/review` and `/test`.

Current implementation lives primarily in [harness.py](../studio/services/harness.py), [chat.py](../studio/api/chat.py), [workspace.py](../studio/api/workspace.py), [harness_gate.py](../studio/security/harness_gate.py), and [frontend/chat](../frontend/chat/). Historical references to an all-in-one `app.py` in the old plan must be mapped to these split modules when implementing changes.

## Existing foundations with remaining gaps

*This table records the 2026-10-21 position. Most gaps listed here have since been implemented behind default-off flags; the milestone index and sections below are current.*

| Area | Current foundation | Next-stage gap/status |
| --- | --- | --- |
| Verification | Post-edit diagnostics, nonzero-exit reporting, repair hints, opt-in coding guidance; M1 per-turn gate/status UI accepted with checks, review, browser evidence, and full build | Partial foundation; no durable/content-fingerprint-bound completion gate |
| Continuity | Saved chats/backups, context checkpoints, opt-in chat SSE replay; computer-use `AgentRun` persists run/event history and can replay saved events | Partial foundation; [chat_streams.py](../studio/services/chat_streams.py) buffers are bounded and process-local. Computer-use [run storage](../studio/services/agent/loop.py) and [event replay](../studio/api/agent.py) preserve history, while `/resume` requires an active in-memory run. Restart-safe coding-chat continuation remains open |
| Planning | `todo_write`, conversation state, permission prompts | Partial foundation; no enforced Plan/Act/Auto state machine or versioned intent/spec/plan artifacts |
| Instructions and skills | Root workspace instruction loader and slash-prompt expansion | Partial foundation; no nested precedence resolver or discovered `load_skill` lifecycle |
| Review | `/review` prompt and context-isolated verifier delegation | Partial foundation; no structured `REVIEW.md` finding and resolution workflow |
| Safety and delegation | Path/command fences, permission gates, worker isolation, opt-in sandbox/resource limits | Partial foundation; no comprehensive test protection or coordinated write-worktree lifecycle |
| Evaluation and automation | Offline deterministic suites and API routes | Next-stage real-model eval suite and durable trigger/scheduling workflow not started |

Defaults are defined in [flags.py](../studio/storage/flags.py): `harness.coding_workflow`, `harness.verification_gate`, `streams.resume`, sandbox/resource-limit and listed security controls default to false; CSP defaults to `"off"`. Installed settings may override defaults. Prompt guidance, a default-off control, and an enforced runtime contract are different claims; each milestone must record its actual flag state and scope.

## Next-stage milestone index

Each milestone has exactly three broad acceptance criteria; all remain unchecked. M1's separate initial per-turn slice is accepted at 6/6, with checks, expanded review, actual-browser evidence, and full build. Dependencies below are architectural prerequisites, not a requirement to implement rows sequentially; evaluation fixtures can be prepared immediately.

| ID | Deliverable | Status (2026-10-23) | Implemented criteria | Dependencies |
| --- | --- | --- | --- | --- |
| M1 | Enforced verification and bounded repair | Implemented; mapping audited | `[###] 3/3` | M2 for durable evidence |
| M2 | Durable runs and restart recovery | Implemented; mapping audited (docs and retention added) | `[###] 3/3` | None |
| M3 | Real-model agent evaluations | Kit complete with offline proof; **no measured baseline** | `[##-] 2/3` | Fixtures first; M1/M2 for completion/recovery cases |
| M4 | Skill discovery and loading | Implemented; mapping audited | `[###] 3/3` | M10 |
| M5 | Tool hooks and test protection | Implemented; mapping audited (docs and ordering/authority tests added) | `[###] 3/3` | M10; M2 for durable audit |
| M6 | Plan/Act/Auto with project artifacts | Implemented; mapping audited (chat-level approval/Auto tests added) | `[###] 3/3` | M1, M2, M10 |
| M7 | Structured review and resolution | Implemented and test-backed (reviewer stubbed in tests) | `[###] 3/3` | M1, M2, M6 |
| M8 | Triggers and scheduling | Implemented and test-backed, **queue-only** | `[###] 3/3` | M1, M2, M5, M6 |
| M9 | Coordinated delegation and worktrees | Implemented and test-backed; workers are not model-spawned | `[###] 3/3` | M2, M5; M1/M7 for integration |
| M10 | Nested workspace instructions | Implemented; mapping audited (provenance now shown in the UI) | `[###] 3/3` | None |

### M1 — Enforced verification and bounded repair

**Status (2026-10-23):** Implemented: the working tree also holds fingerprint-bound, durable, structured check outcomes (`harness.verification_fingerprints`, `check_outcomes` in [verification.py](../studio/services/verification.py), persisted by the run store) with a passing suite ([check_workflow_runs.py](../macos/check_workflow_runs.py)); the criteria's mapping to tests was not re-audited and no independent review exists. Earlier status follows.

**Status (2026-10-21):** Partial. Sol's initial opt-in per-turn gate and status UI are accepted at 6/6 after targeted checks, Astra's expanded review, isolated browser verification, and a successful full build. This initial slice does not by itself satisfy durable evidence, filesystem-revision/content-fingerprint invalidation, or the entire milestone; broad acceptance below remains unchecked. Invalidation after observed tool mutations is a narrower capability than detecting external file changes.

- [ ] Persist structured check outcomes (`passed`, `failed`, `skipped`, `timed_out`) with commands, exit status, evidence, and workspace revision/content fingerprint; later edits invalidate affected results.
- [ ] Require successful applicable checks before `verified_complete`; failed, skipped, missing, stale, or timed-out required checks produce an explicit incomplete/needs-user outcome, including when the model's prose claims success.
- [ ] Bound diagnosis/repair/retest attempts and elapsed budget; deterministic tests cover repair success, repeated failure, cancellation, stale checks, and budget exhaustion without endless retries.

**Proposed modules:** [harness.py](../studio/services/harness.py), [chat.py](../studio/api/chat.py), [flags.py](../studio/storage/flags.py), a new verification-state service, chat result UI. **Evidence:** command transcripts and completion-state assertions tied to the same revision. Define applicable checks and the authority to waive them before implementation.

#### M1 initial implementation slice — per-turn gate

**Status:** Done; `[######] 6/6` accepted slice items. These items describe the bounded first delivery, not completion of M1's three broader criteria. See [verification-gate.md](verification-gate.md) for configuration, exact tool behavior, targeted commands, and limitations. The initial backend implementation is in [verification.py](../studio/services/verification.py) and [chat.py](../studio/api/chat.py).

- [x] **S1 — Trusted configuration:** Default-off `harness.verification_gate`, workspace-scoped command configuration snapshotted from application settings, and exact foreground command/canonical-workspace matching; existing execution approvals remain required.
- [x] **S2 — Turn-local evidence:** Backend execution results establish successful check evidence, observed edits/Python/MCP and all unconfigured shell executions invalidate it conservatively, and syntax failures/background work prevent certification.
- [x] **S3 — Completion enforcement:** Buffer model prose (including intermediate commentary) and `reply` answers until the gate allows release, while reasoning/tool progress can stream; permit at most two completion-repair retries within the existing twelve tool rounds, then return an explicit unverified outcome without substituting an unsupported success claim.
- [x] **S4 — Deterministic coverage:** Passed targeted regression checks for disabled behavior, actual/wrong/stale/failed checks, syntax failure, repair, `reply`, refusal, mutation invalidation, background work, per-turn/configuration isolation, timeout, and Stop in both stream modes.
- [x] **S5 — Implementation review:** Astra accepted the bounded backend plus Sol's frontend status handling/persistence/display, with matching contract fixtures and no test weakening; no broad M1 acceptance granted.
- [x] **S6 — Browser and repository build:** Parent passed isolated actual-browser checks using synthetic turns and the final `./macos/build.sh`, exit 0; evidence is recorded below.

**Explicit limits:** This slice observes one turn only; it has no durable coding-run evidence, filesystem content fingerprint, external-edit detection, cross-turn/restart guarantees, general test-file protection, or real-model evaluation claim. Configured verifier script/content is trusted and not protected from edits. Observed mutation generations are not Git revisions. A native-edit syntax failure requires passing native-edit diagnostics to clear it; shell repair alone remains conservatively unverified. Completion retries are bounded, but the full M1 elapsed repair budget and durable structured outcome contract remain open.

### M2 — Durable runs and restart recovery

**Status (2026-10-23):** Implemented behind `harness.durable_runs` (default false); mapping audited. See [workflow_runs.py](../studio/services/workflow_runs.py), [its API](../studio/api/workflow_runs.py), the `/api/chat` wiring in [chat.py](../studio/api/chat.py) and [check_workflow_runs.py](../macos/check_workflow_runs.py), which crashes real child processes before/after dispatch, effect and result, and covers duplicate clients, corrupt state, cancellation, stale workspaces and the new prune rules. Unknown outcomes are never replayed automatically. Storage, migration and retention rules are documented in [workflow-automation.md](workflow-automation.md); retention is an explicit user-invoked prune, never automatic. The original proposal below is retained.

- [ ] Store versioned run/tool state, monotonic event sequence, pending approvals, workspace fingerprint, and terminal outcome under `STATE`, using atomic writes; document migration and retention rules.
- [ ] Recover after crash/restart with explicit interrupted/unknown-outcome handling: an unresolved shell, MCP, or external side effect is not automatically reexecuted; approvals and workspace changes are revalidated.
- [ ] Pass crash injection before/after tool dispatch and result persistence, restart replay, duplicate-client, corrupt-state, cancellation, and stale-workspace checks without duplicate side effects.

**Proposed modules:** new durable run storage/service, [atomic.py](../studio/storage/atomic.py), [chat.py](../studio/api/chat.py), [chat_streams.py](../studio/services/chat_streams.py), frontend reattachment. **Evidence:** restart traces and exactly-once-or-explicit-unknown assertions; never promise exactly-once behavior for arbitrary external tools.

### M3 — Real-model agent evaluations

**Status (2026-10-23):** The kit is complete; one criterion is open. [macos/evals](../macos/evals/README.md) has 8 versioned tasks over coding, repair, refusal/approval, compaction and **recovery** (a real durable run is interrupted after a completed step, resumed through the resume API, and must finish without replaying side effects; an exact replay is refused by the harness and scored as a failure), isolated fixtures, declarative objective checks, tool/permission budgets, an attach-only runner that can never stop or start your engine and refuses to score results under a model the engine is not serving, and reports with environment metadata (model entry, served model, engine runtime versions read from its own environment, app/git state, hardware, flags, sampling, task-set revision), per-task pass counts and baseline comparison. [check_evals.py](../macos/check_evals.py) proves every task is solvable and every check discriminates, and that blocked/skipped runs are excluded from success. **Open:** criterion 3, a measured baseline and agreed thresholds, which need a real run (`run_evals.py --attach --model <id> --repeat 3`) against an engine you have loaded.

- [ ] Add a versioned `macos/evals/` task set with isolated fixtures, objective outcomes, tool/permission budgets, and coding, repair, refusal/approval, compaction, and recovery cases.
- [ ] Record model identifier/quantization, engine/runtime versions, hardware, flags, task revision, seed when supported, latency, tool failures, and scored artifacts for repeated runs.
- [ ] Establish a measured baseline and agreed regression thresholds; publish per-task pass counts and failures, with blocked/skipped runs excluded from success and reported explicitly.

**Proposed modules:** new `macos/evals/` runner/fixtures/reports, harness event export. **Evidence:** reproducible report plus saved task artifacts. Use a dedicated state directory and explicitly coordinated engine access; do not interrupt the user's active engine to run evaluations.

### M4 — Skill discovery and `load_skill`

**Status (2026-10-23):** Implemented behind `harness.skills` (default false); suite passes, mapping not re-audited. See [skills.py](../studio/services/skills.py), [harness-resources.md](harness-resources.md) and [check_harness_resources.py](../macos/check_harness_resources.py). Skill text cannot grant permissions; `permissions` frontmatter is rejected.

- [ ] Define skill locations, metadata/schema, discovery limits, precedence, trust boundary, and version/content identity; expose a bounded catalog without loading every skill body.
- [ ] Implement `load_skill` with fenced supporting-resource reads, provenance, context budgeting, and clear missing/invalid-skill errors; skill text cannot silently grant tool permissions.
- [ ] Verify selection/loading on real fixtures, nested-instruction interaction, malformed metadata, symlink/path escape, conflicting skills, and context-budget exhaustion.

**Proposed modules:** new skill discovery/loading service, [harness.py](../studio/services/harness.py), workspace settings/UI. **Evidence:** catalog/load contracts and fixture transcripts. Runtime skill files must enter the app build copy list when packaged.

### M5 — Tool hooks and test protection

**Status (2026-10-23):** Implemented behind `harness.hooks` and `harness.protect_tests` (default false); mapping audited. See [workflow_policy.py](../studio/security/workflow_policy.py), [execution.py](../studio/security/execution.py) and [check_workflow_policy.py](../macos/check_workflow_policy.py), which now also asserts hook ordering, first-denial-stops-the-chain, that a permissive hook cannot lift a fence or test protection, and that hooks never run in Plan mode. Contract, configuration and cooperative-only boundaries are documented in [workflow-automation.md](workflow-automation.md). `delegate_write` and `use_computer` stay blocked as opaque under test protection.

- [ ] Specify and implement `PreToolUse`/`PostToolUse` contracts with bounded execution, timeouts, deterministic ordering, failure policy, and durable audit; hooks cannot bypass existing approvals or fences.
- [ ] Define protected test paths and explicit exceptions, then enforce the stated protection scope across direct edits, shell/Python execution, MCP, and delegated writes; document any cooperative-only boundaries instead of claiming arbitrary-shell protection from path checks.
- [ ] Test allowed/denied edits, rename/symlink/path escapes, hook failure/timeout, shell/MCP/delegate bypass attempts, explicit exceptions, and unchanged behavior with the feature disabled.

**Proposed modules:** new hooks/policy services, [harness_gate.py](../studio/security/harness_gate.py), [execution.py](../studio/security/execution.py), [harness.py](../studio/services/harness.py), MCP dispatch. **Evidence:** enforcement matrix by execution path, with unsupported cases visible.

### M6 — Plan/Act/Auto and versioned artifacts

**Status (2026-10-23):** Implemented behind `harness.workflow` (default false); mapping audited. See [workflow.py](../studio/services/workflow.py), [its API](../studio/api/workflow.py), the Workflow dialog in [workflow.mjs](../frontend/chat/workflow.mjs), [check_workflow_policy.py](../macos/check_workflow_policy.py) and the chat-level `TaskChat` tests in [check_workflow_runs.py](../macos/check_workflow_runs.py): a declined approval ends `needs_user` with no success claim, Auto requires a task and ends `verified_complete` or `incomplete`, and Plan mode cannot edit and ends `planned`. The audit also fixed a defect: with the global verification gate on, a *blocked* edit attempt in Plan mode made the turn `needs_user`; Plan mode is now outside the completion gate.

- [ ] Define mode transitions and write/execute permissions; Plan permits only designated planning-artifact writes and cannot mutate other project files through supported execution paths, Act follows selected scope, and Auto obeys bounded budgets and approval requirements.
- [ ] Create/update versioned `intent.md`, `spec.md`, and `plan.md` at an explicit workspace location, with provenance, acceptance criteria, selected workspace, and links to durable run state.
- [ ] Verify pause/resume, scope changes, rejected approval, user edits to artifacts, stale plans, cancellation, and Auto completion/failure; UI and backend expose the same mode and outcome.

**Proposed modules:** new workflow state service, [chat.py](../studio/api/chat.py), [harness_gate.py](../studio/security/harness_gate.py), composer/agent UI. **Evidence:** state-transition tests and browser checks using synthetic turns; no synthetic test may invoke the live model engine.

### M7 — Structured `REVIEW.md` workflow

**Status (2026-10-23):** Implemented and test-backed behind `harness.review` (default false). The service existed but was unregistered and untested, and completion called it with the wrong arguments; those and two further defects were fixed and the UI was completed (see the evidence log). See [reviews.py](../studio/services/reviews.py), [its API](../studio/api/reviews.py), [check_workflow_reviews.py](../macos/check_workflow_reviews.py) and [workflow-automation.md](workflow-automation.md). **Limit:** the tests stub the reviewer model; review *quality* with a real local model is unmeasured, and the reviewer is the same model in a clean context, not an independent party.

- [ ] Define review scope/revision and a `REVIEW.md` schema for evidence, severity, file/line, disposition, fix ownership, and unresolved findings; absence of findings is an explicit reviewed result.
- [ ] Run a review pass separate from implementation context, track fix/review cycles, and invalidate stale review evidence after relevant changes; expose disagreements without silently dismissing them.
- [ ] Gate reviewed completion on applicable verification and disposition of required findings; test empty reviews, disputed findings, stale revisions, exhausted repair budget, and outstanding blockers.

**Proposed modules:** new review service/artifact parser, [harness.py](../studio/services/harness.py), workflow UI. **Evidence:** review artifact, reviewed diff identity, dispositions, and check results. A review role label alone does not prove an independent review occurred.

### M8 — Durable triggers and scheduling

**Status (2026-10-23):** Implemented and test-backed behind `harness.triggers` (default false), as a **queue-only** design: schedules and `POST /api/agent/trigger` queue invocations that a person must start, so no action runs unattended and every approval still applies. Unattended execution was deliberately not built because this section requires the scheduling policy to be agreed first. See [triggers.py](../studio/services/triggers.py), [its API](../studio/api/triggers.py), [check_workflow_triggers.py](../macos/check_workflow_triggers.py) (injected clock; no sleeping) and [workflow-automation.md](workflow-automation.md). Notices appear in the app only (the Workflow status line shows ready/attention counts and refreshes every 60 s while triggers are on); no OS notification is sent.

- [ ] Define authenticated local trigger/schedule contracts with explicit workspace, task scope, permissions, budgets, idempotency key, and user-visible enable/pause/delete controls.
- [ ] Dispatch through durable workflow state with overlap limits, missed-run/restart policy, cancellation, and deduplication; preserve approval requirements for unattended actions.
- [ ] Verify duplicate delivery, overlapping runs, clock changes, restart during dispatch, denied approval, exhausted budget, and meaningful completion/failure notification behavior.

**Proposed modules:** new trigger/scheduler service and API router, durable run store, settings UI. **Evidence:** deterministic clock-driven scheduling tests and restart traces. Scheduling policy must be agreed before enabling unattended execution.

### M9 — Coordinated delegation and worktrees

**Status (2026-10-23):** Implemented and test-backed behind `harness.worktrees` (default false). Isolated Git worktrees; scope containment and budget limits against the parent task; disjoint write ownership; digest-bound user-reviewed integration as uncommitted changes; refusal over user changes, moved bases, out-of-scope files and symlinks; cancellation propagation to workers and their runs; recoverable cleanup; runs in a worker's checkout linked to it and their steps charged to the parent (a worker over its allocation cannot be integrated); and completion after an integration enforced to use checks that ran afterwards against exactly the integrated content. See [worktrees.py](../studio/services/worktrees.py), [its API](../studio/api/worktrees.py) and [check_workflow_worktrees.py](../macos/check_workflow_worktrees.py). **Limits:** workers are not spawned by the model (a worker is a Code chat you open on its path); step accounting is after the fact, not mid-run; glob scope checks are conservative approximations outside directory-style patterns.

- [ ] Define parent/child run IDs, scoped permissions, task budgets, cancellation propagation, artifact handoff, and workspace/write ownership; children cannot gain permissions beyond the parent.
- [ ] Add opt-in isolated Git worktrees or explicit file ownership for write workers, with conflict detection, reviewed integration, user-change preservation, and recoverable cleanup.
- [ ] Verify concurrent conflicting edits, child failure, parent cancellation/restart, dirty workspaces, stale integration bases, and cleanup; require checks/review on the integrated result.

**Proposed modules:** delegation in [harness.py](../studio/services/harness.py), new worktree coordinator, durable run storage, workflow UI. **Evidence:** multi-worker fixture traces and final integrated diff/check records. Avoid implying simultaneous heavy local engine processes; preserve the single-engine invariant.

### M10 — Nested instruction precedence

**Status (2026-10-23):** Implemented behind `harness.nested_instructions` (default false); mapping audited. See [instructions.py](../studio/services/instructions.py), [harness-resources.md](harness-resources.md) and [check_harness_resources.py](../macos/check_harness_resources.py). The rule files that applied to a tool's target (name, scope, short digest, truncation; never their content) now appear on the tool step in the UI, tested through the real chat route and the renderer, including escaping of repository-supplied names.

- [ ] Define root-to-target inheritance and precedence for supported instruction files, handling conflicts, truncation, multiple targets, and edits to instruction files with visible provenance.
- [ ] Resolve applicable instructions for each read/edit/delegated scope, staying inside the selected workspace and respecting symlink and sensitive-path fences; refresh stale instruction content.
- [ ] Verify nested overrides, sibling isolation, multi-file edits, deleted/changed rules, size limits, symlinks, and delegated workspaces; display which rules applied to the affected path.

**Proposed modules:** `_load_workspace_instructions` in [harness.py](../studio/services/harness.py), new instruction resolver, tool-context/UI provenance. **Evidence:** fixture matrix showing effective instructions per target; repository instructions do not override user authorization or application safety gates.

## Validation and acceptance workflow

1. **Sol implements** a bounded milestone slice and records its acceptance criteria, flag/default behavior, changed modules, and open limitations here. Proposed new modules above are design suggestions, not existing files.
2. Run targeted deterministic checks, then applicable repository verification from [AGENTS.md](../AGENTS.md). Always use `TMPDIR=/tmp` for `macos/check_agent.py`; isolate mutable test state with a temporary `LOCAL_STUDIO_HOME`. Do not weaken failing checks.
3. For UI changes, run `node macos/check_ui.js index.html` and check actual selectors/events in the isolated `agent-ui-dev` server, Code view pointed at a scratch workspace. Use synthetic messages or stubbed chat responses; stub DOM success alone is insufficient.
4. For runtime milestone acceptance, retain exact commands, outcomes, relevant logs/artifacts, flag state, and revision/content identity. Run the full `./macos/build.sh` before accepting a milestone that changes packaged runtime code; run real-model evals under the coordinated M3 protocol when applicable.
5. **Astra advises/reviews** the implementation and evidence against the criteria, reporting blockers or accepted scope. Record that review only after it occurs. Architecture advice for this document is not runtime verification or milestone acceptance.
6. Resolve review findings and rerun affected checks. Mark criteria accepted only when their evidence supports the claim; mark the milestone Done only after all three criteria and review are accepted.

## Progress update protocol

- This Markdown dashboard is **manually maintained**. No automatic updater exists as part of this documentation change.
- Update checkboxes, per-milestone `0/3` counts, index status/bars, aggregate `0/30` criteria count, and `0/10` milestone count together. Use `#` for each accepted item and `-` for each remaining item; keep each denominator fixed unless scope is explicitly revised.
- Maintain the separate M1 initial-slice `n/6` count and six-character bar from S1–S6 only. Completing that slice leaves broad M1 and aggregate milestone counts unchanged until their own criteria are accepted.
- Add a dated evidence entry naming milestone/criterion IDs, implementation revision, verification commands/results, review author/result, and unresolved limitations. Unchecked criteria may be marked Partial in prose without receiving completion credit.
- Reopen affected criteria when regressions or stale evidence invalidate acceptance. Record scope additions/removals explicitly before changing any denominator; do not combine baseline and future milestones into an overall readiness percentage.

| Date | Entry | Evidence and limits |
| --- | --- | --- |
| 2026-10-20 | Roadmap initialized by Sol with Astra's architecture/acceptance advice | Existing tracker and source inspection establish this inventory. Documentation/link/count checks only; no next-stage implementation, runtime acceptance, or implementation review claimed. |
| 2026-10-20 | M1 initial per-turn gate authorized; implementation in progress | No acceptance credit yet. Implementation evidence, Astra's review result, and build verification must be added after they occur. |
| 2026-10-21 | S5 implementation review accepted by Astra | Reviewed uncommitted working tree based on `bfcdd0814a6f98093ac3459cfa0905acc6d1f3d7` (`codex/p6-search-release`); that HEAD does not contain the gate. Corrected unsafe assumption that approved shell commands cannot mutate. Astra's independent state assertions passed for test-command invalidation, timeout, boolean exit status, background work, and default-off behavior. Combined targeted rerun and final build still pending. |
| 2026-10-21 | S1–S4 accepted for the bounded initial slice | Sol's final combined run passed all seven named checks in [verification-gate.md](verification-gate.md), exit 0, using `TMPDIR=/tmp`, `PYTHONDONTWRITEBYTECODE=1`, and cached pinned Python. Python AST, UI startup, and diff checks also passed. Parent confirmed acceptance against the uncommitted runtime snapshot; full build remains pending for S6. |
| 2026-10-21 | Initial build failed the SSE contract check; S5 reopened | `x_verification` needs frontend handling and the intended contract fixture update. S1–S4 retain bounded backend/test acceptance; frontend synthetic browser evidence, Astra's expanded review, and a successful final build are pending. |
| 2026-10-21 | S5 expanded implementation review accepted by Astra | Frontend validates/persists/displays verification status, hides `not_required`, escapes diagnostic IDs, labels historical passes, and clears status on native Undo. Astra independently passed all 15 `node macos/check_chat_resume.mjs` checks and accepted matching contract fixture updates. Parent's actual-browser evidence and final full build remain pending. |
| 2026-10-21 | S6 accepted; initial slice complete at 6/6 | Parent passed `TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 UV_OFFLINE=1 ./macos/build.sh`, exit 0, producing Apple Development signed Local Studio 1.5.0 build 132. [Build log](../build/verification-gate-build.log) and [runtime/test SHA-256 manifest](../build/verification-gate.sha256) identify the tested uncommitted working tree, based on the HEAD above; no gate-containing commit is claimed. |
| 2026-10-21 | Actual-browser evidence for S6 | On isolated port 7861 with separate state and scratch workspace, four synthetic streams covered verified/unverified/not-required/invalid status. Badge/hidden behavior, malformed-success rejection, escaped malicious check IDs, chat-save/GET persistence, redraw, Code dock, and light/dark token colors passed. [Screenshot](../build/verification-code-ui.png). No live model/chat-engine request was made. Broad M1 remains Partial 0/3, next-stage milestones 0/10, broad criteria 0/30. |
| 2026-10-23 | Audit of the uncommitted working tree against this tracker | The tree already held M2, M4, M5, M6 and M10 code with passing suites (`check_harness_resources.py` 18 tests, `check_workflow_policy.py` 5, `check_workflow_runs.py` 13, `check_agent.py`, `check_chat_resume.mjs`, `check_ui.js`), none recorded here; M7's service existed but was unregistered and untested; M3, M8 and M9 had only flags. `check_app.py` failed on the route-contract fixture, which had never been regenerated for the new routers. |
| 2026-10-23 | M7 defects found and fixed | (1) `workflow.complete_task` called `reviews.require_complete(task)` without the verification snapshot, so any `review_required` task would raise `TypeError` at completion; (2) `_run_verification` read `task_identity` from the top of the run record although the run store keeps it under `metadata`, rejecting every verification run as belonging to another task; (3) the task's own planning artifacts were counted as reviewed changed files; (4) `api/reviews.py` and `services/reviews.py` were never registered; (5) the Review button had no handler and findings/dispositions had no UI. New [check_workflow_reviews.py](../macos/check_workflow_reviews.py) has 9 tests (including exhausted-budget, background and syntax-failure snapshots), including the end-to-end `complete_task` path that would have caught (1). |
| 2026-10-23 | Contract fixtures | `routes.json`: 17 added routes before M8/M9 (workflow tasks/runs/reviews) and more after, 0 removed, existing order unchanged (verified by diff before regenerating). `sse_contract.json`: a ternary in `chat.py` had hidden the `x_agent` event from the contract extractor; the code was restructured so `x_agent` stays explicit and only payload counts changed. `daily`/`interval` added to the extractor's non-run-event list. |
| 2026-10-23 | M3 evaluation kit built | [macos/evals](../macos/evals/README.md), `check_evals.py` (11 tests, including a check that an attached engine's model matches the requested one): every task's reference script passes and negative script fails; fixtures are never modified; approval policies enforced; blocked/skipped excluded from success; attach mode never calls stop/start; sessions refuse the live STATE. The first run exposed that the scripted transport never reached the harness (a "model not downloaded" error was being scored as a pass); runs that raise an error event or produce no output are now never scored. A `.gitignore` rule for `build/` also silently excluded a fixture directory of that name. **No real-model run was made and no baseline exists.** |
| 2026-10-23 | M8 triggers built (queue-only) | [check_workflow_triggers.py](../macos/check_workflow_triggers.py), 13 tests with an injected clock: interval and daily slots, missed-run policies, overlap, clock regression (which exposed a re-anchoring gap, fixed), idempotent external delivery, authentication, start-time task revalidation, run-outcome notifications including declined/unknown outcomes, interrupted starts never replayed, cancel propagation, delete safety, HTTP contract. Unattended execution intentionally not built. |
| 2026-10-23 | M9 worktrees built | [check_workflow_worktrees.py](../macos/check_workflow_worktrees.py), 9 tests on throwaway Git repositories only: isolation, scope/budget/count/ownership limits, digest-bound integration that never commits, refusal over user changes/moved bases/out-of-scope files/symlinks, cancellation propagation, recoverable cleanup, orphan reporting, HTTP contract. |
| 2026-10-23 | Actual-browser evidence for M7/M8/M9 | Isolated `agent-ui-dev` server (port 7861, state under `build/agent-ui-state`, no engine started, `/api/chat` stubbed): review findings render; a disposition without a reason is rejected and with one is recorded; a trigger pauses; *Start with approvals* sends exactly one `/api/chat` carrying the trigger's idempotency key, task, workspace and mode, the instruction as the user message, and **no** `permission_mode`; a worker with no changes is blocked; a real change in its checkout is blocked while the parent has uncommitted edits to the file, then applied uncommitted (parent still one commit) after they were cleared. Only expected 503s (`/api/llm/health`, no engine) and one deliberate 409. |
| 2026-10-23 | Final full build and packaging | `TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 UV_OFFLINE=1 ./macos/build.sh`, `BUILD EXIT=0` read from the build log, producing the Apple Development signed Local Studio 1.5.0 build 132 from the uncommitted working tree (no commit contains it). It ran `check_ui.js`, the AST check, `check_search.py`, `check_search_contract.py`, `check_app.py`, `check_agent.py`, `check_harness_resources.py`, `check_workflow_policy.py`, `check_workflow_runs.py`, `check_workflow_reviews.py`, `check_evals.py`, `check_workflow_triggers.py` and `check_workflow_worktrees.py`. All new runtime files (`workflow.mjs`, `reviews`, `triggers`, `worktrees`, `workflow`, `workflow_runs`, `verification`, `skills`, `instructions`, `workflow_policy`, and the five new API modules) were confirmed present in the bundle, with no `__pycache__`. An earlier build in the same session failed on the SSE contract (schedule `type` values); fixed before this one. Independent review is still outstanding, so accepted counts stay 0/10 and 0/30. |
| 2026-10-23 | Follow-up: technical-debt pass and criterion-to-test audit of M1, M2, M4, M5, M6, M10 | Read each criterion against its tests. Gaps found and closed: no documentation of durable-run storage/migration/retention or of the hook contract and test-protection boundaries (now in [workflow-automation.md](workflow-automation.md)); run records and audit files had no retention (explicit `POST /api/workflow/runs/prune` added with a test that unresolved, recent and quarantined runs survive); no test for hook ordering, chain stop or hooks not lifting fences; no chat-level test for a declined approval or Auto outcomes in a task run; Plan mode wrongly subject to the completion gate (fixed); rule provenance never reached the UI (now shown, tested). |
| 2026-10-23 | Follow-up: M9 and M3 completed | M9: completion after an integration now requires checks that ran afterwards against exactly the integrated content; worker runs are listed under the worker and their steps charged to the parent, over-allocation blocks integration. M3: a recovery task (interrupt a real durable run, resume via the API, no replayed side effects; found that the harness already refuses an exact replay, now scored as a failure), engine runtime versions, a model-match check for attached runs. Still open: a measured baseline, which needs your engine. |
| 2026-10-23 | Follow-up: final full build | `TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 UV_OFFLINE=1 ./macos/build.sh`, `BUILD EXIT=0` read from the build log (not the background task's own status), signed Local Studio 1.5.0 build 132 from the uncommitted working tree. Ran every suite listed in the earlier build row with the additions above (reviews 9 tests, evals 12, worktrees 11, policy 6, runs/chat 18, UI/chat-resume checks). New runtime files confirmed in the bundle, no `__pycache__`. Still no independent review, so accepted counts remain 0/10 and 0/30. |
