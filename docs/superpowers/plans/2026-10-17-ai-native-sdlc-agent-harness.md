# Local Studio — AI-Native SDLC & Agent Harness Phase 2 Playbook

**Reference**: [Anthropic — The AI-native SDLC Playbook](https://claude.com/resources/articles/the-ai-native-sdlc-playbook)
**Target Codebase**: `app.py`, `index.html`, `macos/check_agent.py`, `CLAUDE.md`, `AGENTS.md`

This document translates Anthropic's **6-Stage AI-Native SDLC Playbook** (*Plan → Design → Build → Test → Deploy → Maintain*) and missing production Agent Harness capabilities into concrete, file-level implementation specifications for **Local Studio** so that Claude Code or any coding agent can implement them directly.

---

## Summary Matrix: Playbook Stage $\rightarrow$ Local Studio Feature

| SDLC Stage | Playbook Concept | Current State in Local Studio | Phase 2 Implementation Task |
|---|---|---|---|
| **1. Plan** | `intent.md` capture | Freeform chat only | **Task 1**: `/intent` & `/spec` slash commands + artifact generator (`intent.md`, `spec.md`) |
| **2. Design** | Structured `spec.md` & clarifying Qs | `ask_user` tool exists | **Task 1**: Structured spec templates + `@file` / `@folder` context mentions in composer |
| **3. Build** | Plan Mode (`plan.md`) vs. Auto Mode | `todo_write` checklist + per-category session approvals | **Task 2**: Explicit **Plan Mode / Act Mode / Auto Mode** toggle in composer + `plan.md` approval gate |
| **3. Build** | `CLAUDE.md` / `AGENTS.md` auto-context | Static `AGENT_CHAT_NOTE` only | **Task 3**: Auto-load workspace `CLAUDE.md` / `AGENTS.md` into `_with_agent_note()` |
| **3. Build** | **Skills** (`.claude/skills/<name>/SKILL.md` or `.agents/skills/`) | Not implemented | **Task 4**: Workspace & user Skills discovery + `use_skill(name)` tool / `/` skill invocation |
| **3. Build** | **Hooks** (`PreToolUse`, `PostToolUse`) | Hardcoded `_harness_path_fence` & `_gate_shell_command` | **Task 5**: Configurable `PreToolUse` / `PostToolUse` hooks + automatic post-edit syntax/lint diagnostics |
| **3. Build** | **Subagents** (`.claude/agents/*.md`) & Worktrees | Single 12-round loop sharing 32k context | **Task 6**: `run_subagent(agent, task)` tool with isolated 32k context window (e.g. `researcher`, `verifier`, `simplifier`) + Turn-level Git Checkpoint / Restore |
| **4. Test** | Feedback Loop & Test-File Protection | Manual `run_shell` (requires approval every time) | **Task 7**: Safe Read-Only & Test Command Allowlist (`git status`, `git diff`, `pytest`, `npm test`) + Test-file write protection mode during bug-fix tasks |
| **4. Test** | Continuous Agent Evals | `macos/check_agent.py` (scripted unit checks) | **Task 8**: JSON-driven Agent Harness Eval Runner (`macos/evals/*.json`) for regression-testing prompts, skills, and local models |
| **5. Deploy** | Multi-Pass `REVIEW.md` Code Review | None | **Task 9**: Built-in `/review` workflow reading workspace `REVIEW.md` (3 passes: *Bugs*, *Security*, *Compliance*, capped nits) |
| **6. Maintain** | Closing the Loop (Deterministic Trigger $\rightarrow$ `intent.md`) | None | **Task 10**: Local Watch / Scheduled Trigger (`POST /api/agent/trigger`) that runs a deterministic check and writes `intent.md` on breach |

---

## Detailed Implementation Tasks (For Coding Agents)

### Task 1: Workspace `CLAUDE.md` / `AGENTS.md` Auto-Loading (Stage 3: Build)

**Goal**: Just as Claude Code reads `CLAUDE.md` at the start of every session so the agent knows build/test/lint commands, architecture, and "things the model gets wrong", Local Studio must automatically inject workspace instructions when a workspace is active.

#### Backend (`app.py`)
1. Write helper `_load_workspace_instructions() -> str`:
   - Check `_active_workspace_dir()` (resolved via `_chat_workspace` `ContextVar`).
   - Look for instruction files in priority order:
     1. `AGENTS.md`
     2. `CLAUDE.md`
     3. `.claude/CLAUDE.md`
     4. `.cursorrules`
   - Read up to `4,000` characters per file (max `6,000` chars total) so it stays within the 32k context budget ("Keep it under a page, because the model reads all of it at the start of a session").
   - Redact or skip if `_harness_path_fence(p, write=False)` rejects the path.
2. In `_with_agent_note(messages, tz_name)`:
   - Append a fenced block when workspace instructions exist:
     ```text
     Workspace instructions from <filename> (follow these repo conventions and verification commands):
     <content>
     ```
3. Add `/init` support: when the user sends `/init`, prompt the model to inspect the active workspace (`list_directory`, `read_file` on `package.json` / `pyproject.toml` / `Makefile` / `README.md`) and draft a concise `<1 page` `AGENTS.md` / `CLAUDE.md` covering `## Commands`, `## Conventions`, `## Architecture`, and `## Things to avoid`.

#### Verification (`macos/check_agent.py`)
- Add `check_harness_workspace_instructions_autoload()`: create a temp workspace with `CLAUDE.md`, call `_with_agent_note()`, and assert the instructions appear in the system note.

---

### Task 2: Plan Mode, Act Mode, and Auto Mode + `intent.md` / `spec.md` / `plan.md` (Stages 1, 2, 3)

**Goal**: Implement the Playbook's artifact-driven workflow:
- **Stage 1 (Plan)**: Capture problem, desired outcome, scope, and constraints as `intent.md`.
- **Stage 2 (Design)**: Expand `intent.md` into `spec.md` (requirements, acceptance criteria, non-goals, architectural design).
- **Stage 3 (Build)**: Start in **Plan Mode** (`plan.md` naming exact files to modify, new types/functions, and tests to run) before switching to **Act** or **Auto Mode**.

#### Backend (`app.py`)
1. Accept `harness_mode: Literal["plan", "act", "auto"] = "act"` in `POST /api/chat` payload:
   - **`plan` mode**:
     - Restrict `CHAT_TOOLS` to `CHAT_READ_TOOLS + [ASK_USER_TOOL, REPLY_TOOL]` (plus allow `write_file`/`edit_file` *only* on `intent.md`, `spec.md`, or `plan.md`).
     - Append to `AGENT_CHAT_NOTE`:
       `"PLAN MODE ACTIVE: Do not modify source code or run destructive commands. Explore the codebase using read-only tools, ask clarifying questions with ask_user if requirements are ambiguous, and produce a concrete implementation plan (files to change, functions/types, and verification steps)."`
   - **`act` mode** (default):
     - Full 18 tools; `write_file`, `edit_file`, `run_python`, `run_shell` ask for interactive approval unless pre-approved by safe allowlist or session toggle.
   - **`auto` mode**:
     - Pre-populate `_CHAT_SESSION_APPROVALS[chat_id] = {"edit", "exec"}` for the session so approved plans execute without per-edit approval prompts, while still enforcing `_harness_path_fence()`, `_gate_shell_command()`, and `PreToolUse` hooks.

#### Frontend (`index.html`)
1. Add a 3-state mode pill next to `#wsPill` in the chat composer:
   - `Plan` (read-only exploration + `plan.md` generation)
   - `Act` (standard interactive approvals)
   - `Auto` (autonomous execution within safety fences)
2. When a turn finishes in `Plan` mode, render an **"Approve Plan & Build"** button at the bottom of the assistant message that switches the mode to `Auto` (or `Act`) and sends `"Proceed with implementing the approved plan and run verification checks."`

---

### Task 3: Built-in Post-Edit Verification Loop & Configurable Hooks (Stages 3, 4, 5)

**Goal**: Implement the Playbook's **Hooks as build-time guardrails** (`PreToolUse`, `PostToolUse`) and **Give the agent a feedback loop** so the agent immediately sees syntax/lint errors and cannot weaken tests during bug fixes.

#### Backend (`app.py`)
1. **Built-in Post-Edit Diagnostics (`_post_edit_diagnostics(path: Path) -> dict`)**:
   - Automatically run fast (<1.5s) deterministic syntax checks immediately after `write_file` or `edit_file`:
     - `.py`: `ast.parse(source, filename=str(path))` $\rightarrow$ returns `{syntax_ok: True}` or `{syntax_ok: False, error: "SyntaxError: line X: ..."}`.
     - `.json`: `json.loads(source)` $\rightarrow$ returns `{syntax_ok: True/False, error: ...}`.
     - `.js` / `.mjs`: `node --check <path>` if `node` is on PATH.
   - Include `diagnostics` in the JSON tool result returned to the model by `_tool_write_file` and `_tool_edit_file` so the model fixes syntax errors in the very next round before reporting done.
2. **Test-File Protection Guard (`protect_tests: bool`)**:
   - From Stage 4 ("Give Claude a feedback loop"): *"An agent fixing code must not be able to weaken the check on that code. A hook that blocks edits to test files during a fix task does this."*
   - Provide a `_is_test_file(path: Path) -> bool` helper (`test_*.py`, `*_test.py`, `*.spec.js`, `*.test.ts`, `macos/check_*.py`).
   - When `protect_tests` is enabled on the session/workspace, block `write_file` and `edit_file` on test files with:
     `"Blocked by test-protection guardrail: fix the implementation code rather than modifying or weakening the test file."`
3. **Workspace Hooks Configuration (`.localstudio/hooks.json` or `.claude/settings.json`)**:
   - Support optional `PreToolUse` and `PostToolUse` command hooks in workspace `.localstudio/hooks.json`:
     - Pipe JSON `{"tool_name": name, "tool_input": args, "workspace": str(ws)}` to the hook script's `stdin`.
     - Exit code `0` = allow; exit code `2` = block and return `stderr` to the model as the block reason (matching Claude Code's hook protocol).

---

### Task 4: Safe Read-Only Shell Allowlist (Stages 3, 4, 5)

**Goal**: Implement the Playbook's `permissions.allow` inner-loop principle: *"permissions.allow pre-approves the safe inner loop so the deny list doesn't turn into prompt fatigue."*

#### Backend (`app.py`)
1. Add `_is_safe_readonly_shell(cmd: str) -> bool`:
   - Reject any command containing shell chaining/redirection operators (`;`, `&&`, `||`, `|`, `>`, `<`, `` ` ``, `$(`, `\n`).
   - Parse with `shlex.split(cmd)` and allow known read-only inspection & verification commands when run inside the workspace:
     - `git status`, `git diff`, `git log`, `git branch`, `git show`
     - `pytest`, `python -m pytest`, `python -m unittest`, `npm test`, `cargo test`, `cargo check`, `go test`
     - `ls`, `pwd`, `wc`, `head`, `tail`, `cat`, `stat`, `which`
2. In `_run_chat_harness`:
   - If `tname == "run_shell"` and `_is_safe_readonly_shell(args.get("command", ""))` is `True` (and `_gate_shell_command` is `"safe"`), execute without pausing for an `x_approval` card.

---

### Task 5: Skills as Institutional Knowledge (`.claude/skills/<name>/SKILL.md` & `.agents/skills/`) (Stage 3: Build)

**Goal**: Implement the Playbook's **Skills** architecture: folders containing `SKILL.md` with YAML frontmatter (`name`, `description`) and markdown instructions that load on demand when relevant.

#### Backend (`app.py`)
1. Write `_discover_skills() -> list[dict]`:
   - Scan both `<workspace>/.claude/skills/*/SKILL.md`, `<workspace>/.agents/skills/*/SKILL.md`, and user-global `STATE/data/agent/skills/*/SKILL.md`.
   - Parse YAML frontmatter (`name`, `description`) and file path.
2. In `_with_agent_note()`:
   - If skills exist, list their `name` and `description` in the system note (~1 line per skill).
3. Add read-only tool `load_skill(name: str)` to `CHAT_READ_TOOLS`:
   - Returns the full body of `SKILL.md` (and lists any helper scripts in `scripts/`) so the model only pays the token cost when a skill is triggered.

---

### Task 6: Subagents (`.claude/agents/<name>.md`) & Turn-Level Git Checkpoints (Stage 3: Build)

**Goal**: Implement the Playbook's **Subagents** (`verifier`, `researcher`, `simplifier`) running in a fresh, isolated context window so exploration or final verification does not pollute or bias the main 32k context window, plus turn-level rollback.

#### Backend (`app.py`)
1. Add `run_subagent(role: str, task: str)` to `CHAT_READ_TOOLS`:
   - Built-in roles (plus custom `.claude/agents/<role>.md` definitions):
     - `researcher`: Read-only tools only (`search_files`, `read_file`, `list_directory`, `find_files`, `grep_files`, `web_search`, `read_webpage`). Explores up to 5 rounds in a fresh message list and returns a concise findings summary (<800 tokens) to the parent agent.
     - `verifier`: Fresh-context verification agent ("runs in a fresh context window once the session believes the work is done, so the verdict is not colored by the assumptions that produced the code"). Has read tools + safe test execution; reports what it ran, what it observed, and any mismatch with the user's request or `plan.md`.
     - `simplifier`: Reviews modified files from the current turn and suggests complexity reductions.
2. **Turn-Level Checkpoint & Rollback (`turn_id` backups)**:
   - Group all `_save_file_backup()` records created during a single `/api/chat` request under a `turn_id` (or `session_id`) and emit `x_checkpoint {turn_id, files_count}`.
   - Add `POST /api/agent/undo-turn` that restores all backups from that turn in reverse chronological order so a multi-file turn can be reverted in 1 click.

---

### Task 7: Multi-Pass Code Review via `REVIEW.md` & `/review` Command (Stage 5: Deploy)

**Goal**: Implement the Playbook's **AI in the PR review loop** (`REVIEW.md`):
- Run three structured passes tagged by category:
  1. `[Bugs]`: logic errors, broken edge cases, subtle regressions.
  2. `[Security]`: injection risks, path traversal, auth gaps, secrets/PII in logs.
  3. `[Compliance]`: alignment with `spec.md`, `plan.md`, and `CLAUDE.md` / `AGENTS.md`.
- Distinguish **Important** (breaks behavior, leaks data, breaches policy) from **Nit** (style/naming, capped at $\le 5$ nits).

#### Backend & UI (`app.py` & `index.html`)
1. Support slash commands in the chat composer (`/review`, `/init`, `/plan`, `/compact`):
   - Typing `/review` automatically gathers `git status` and `git diff` from the active workspace, loads `REVIEW.md` (if present in the workspace, otherwise uses the Playbook's default 3-pass review rubric), and runs a structured 3-pass review.
2. Add a **"Save correction to CLAUDE.md / AGENTS.md"** quick action when a review flags a repeated convention issue ("When a review flags a mistake for the second time, the correction goes into `CLAUDE.md`").

---

### Task 8: `@file` Mentions & Conversation Compaction (`/compact`)

1. **`GET /api/workspace/files?q=...`**:
   - Returns up to 20 matching workspace relative paths for fast `@file` autocomplete in the composer (`index.html`).
   - When a message contains `@path/to/file.py`, `app.py` automatically attaches a fenced file preview or resolves the path in the prompt.
2. **`POST /api/chat/compact` (`/compact`)**:
   - Summarizes older turns of a long conversation into a structured state checkpoint (`Goal`, `Completed Changes`, `Active Files`, `Open Decisions`) so sessions can continue past the 32k context window without losing architectural context.

---

### Task 9: Continuous Agent Evals (`macos/eval_harness.py`) & Closing the Loop (Stages 4 & 6)

1. **Declarative Eval Suite (`macos/evals/*.json`)**:
   - Each JSON file defines `{id, prompt, workspace_fixture, allowed_tools, assertions}` (e.g., `file_contains`, `syntax_valid`, `tool_called`, `test_exit_zero`).
   - `macos/check_agent.py` runs offline deterministic evals on every `macos/build.sh` invocation so changes to `AGENT_CHAT_NOTE`, skills, or tool schemas are regression-tested before release.
2. **Closing the Loop (`POST /api/agent/trigger`)**:
   - Accepts a deterministic metric or check failure event (e.g., failed test command or healthcheck), runs the local model in read-only diagnostic mode (`2sigma` tier), and writes a structured `intent.md` (Anomaly, Evidence, Proposed Outcome, Affected Files, Open Questions) into `STATE/data/agent/intents/` for human triage.

---

## Verification Checklist for Implementing Agents

Before marking any task in this playbook complete, run the full verification suite (matching Stage 4's "Verifying your work" rule):

```sh
node macos/check_ui.js index.html
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" app.py
uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py
uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py
TMPDIR=/tmp uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_agent.py
uv run -q --no-project --python 3.12 --with pypdf python macos/check_document.py build/DocumentReader
./macos/build.sh
```
- **Rule**: If a test fails, fix the implementation code, never weaken or delete the test.
