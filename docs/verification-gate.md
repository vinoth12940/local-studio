# Coding completion verification gate

`harness.verification_gate` is an opt-in backend completion gate for chat turns
with a selected workspace. It defaults to `false`. Existing behavior and
execution approvals remain the default.

Configure trusted checks in **`$LOCAL_STUDIO_HOME/data/settings.json`**, merging
these keys with the existing settings. The workspace key must be its absolute,
canonical path (resolve symlinks). The chat request and tool arguments cannot
define required checks for the current turn; policy comes from the settings
snapshot taken at turn start. Settings are not immutable across future turns:
an approved general-purpose shell action could change the settings file.

```json
{
  "harness": {
    "verification_gate": true,
    "verification_workspaces": {
      "/absolute/path/to/project": {
        "commands": [
          {"id": "tests", "command": "python3 -m unittest discover -s tests"},
          {"id": "ui", "command": "node macos/check_ui.js index.html"}
        ]
      }
    }
  }
}
```

Use commands appropriate to that project. These are trusted declarations: choose
substantive checks that return nonzero on failure and finish in the foreground.
An `echo` configured by the user is a poor verifier; the gate does not evaluate
test quality or prevent changes to project test files. There must be 1–16 checks
with distinct, nonempty string IDs and nonempty commands of at most 4,000
characters. All checks are required. No trusted configuration means an observed
coding mutation ends explicitly **Unverified**.

A check passes only when an approved foreground `run_shell` executes the exact
configured command in that workspace and the backend result records that command,
canonical working directory, integer exit status zero, and no execution error.
Whitespace and shell wrappers are significant: a different command, different
directory, background process, `run_python` snippet, tool-provided claim, or model
prose cannot substitute for a check. No command is automatically executed by the
gate; the model must request it through the existing shell approval policy.

The gate tracks an observed mutation revision within the current turn:

- Attempted, approved `write_file`, `edit_file`, `multi_edit_file`, `run_python`,
  custom MCP calls, and **every unconfigured shell command** invalidate earlier
  passes, including execution errors that may follow partial writes. A shell
  inspection such as `ls` also conservatively invalidates passes; use native
  `list_directory`, `git_status`, `git_diff`, and other read tools for inspection.
  The approval allowlist includes test runners and is not proof of no writes.
- Each configured check preserves other check results at the current revision.
  A failed recheck removes its own previous pass. An execution timeout invalidates
  previous passes if the interrupted result cannot establish the configured
  command and directory. Later observed mutations require all checks again.
- Failed edit syntax diagnostics block completion until a later successful edit
  diagnostic for that path clears them. A passing shell check cannot override a
  known unresolved edit diagnostic.
- A started background shell makes the turn unverified, because it may continue
  writing after a check. Stopping it does not certify the same turn. Trusted
  configured checks must not leave background writers running.
- A declined, blocked, or unanswered coding action ends the gated turn explicitly
  unverified without requesting another approval. A refusal never counts as
  execution evidence.

Plain workspace discussion and native read-only tools have no verification
requirement. A requested configured check does activate the requirement even
without an edit, so a failing test-only turn cannot finish as verified. Python
and shell execution are treated conservatively as potentially mutating code.

Candidate answer content, native tool argument deltas (including `reply.text`),
and model finish reasons are held back until completion is allowed. Reasoning,
tool progress, actual tool output, and usage still stream. Both a normal answer
and the standalone `reply` tool pass through the gate. While checks are missing,
the backend supplies repair guidance for at most two attempted completions,
within the existing 12-round tool limit. Exhaustion produces a deterministic
unverified result with missing check IDs or unresolved diagnostics. An unsatisfied
gate also blocks a `use_computer` handoff from escaping the coding check.

The terminal SSE event `x_verification` reports `verified`, `unverified`, or
`not_required`, the observed `revision`, missing and failed check IDs, syntax
failures, and whether a background process was started. `not_required` is not
verification evidence. The UI persists the result on that assistant message and
shows “Configured checks passed for this turn” or “Verification incomplete”; it
shows no badge for `not_required`. Malformed or inconsistent status events cannot
render a success badge. A successful Undo clears that message's status. The
regular content event also carries any unverified reason for existing clients.
Stop and stream cancellation retain existing process-group cleanup and do not
start a repair round.

This first slice is deliberately **turn-local and tool-observed**. It does not
persist evidence across chat turns or restarts, detect external/concurrent edits,
provide tamper-proof project tests, verify computer-harness actions, or certify
the entire user task. An enabled workspace turn buffers answer text even when it
ultimately needs no checks. Durable workflow state, intent/spec acceptance checks,
and a task ledger remain later roadmap work.

Offline checks use isolated temporary workspaces and STATE, synthetic model
turns, and real subprocess execution; they never start a live model engine:

```sh
TMPDIR=/tmp uv run -q --no-project --python 3.12 \
  --with-requirements requirements-app.txt python macos/check_agent.py \
  chat_verification_gate_preserves_default_and_requires_actual_checks \
  chat_verification_gate_rejects_failed_stale_and_wrong_evidence \
  chat_verification_gate_repairs_and_handles_reply_tool \
  chat_verification_gate_keeps_execution_approval_and_refusal \
  chat_verification_gate_multiple_checks_and_timeout \
  chat_verification_gate_stop_cancels_check_in_both_stream_modes \
  verification_evidence_is_turn_local_conservative_and_snapshotted
```
