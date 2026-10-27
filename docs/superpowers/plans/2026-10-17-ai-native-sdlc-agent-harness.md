# Local Studio — Agent Harness Tier 1, Tier 2 & Tier 3 Implementation Plan

**Target Codebase**:
- `app.py` (FastAPI backend, Chat Agent Harness, MCP client, tool definitions & execution)
- `index.html` (Single-page frontend, composer `@` mentions, `/` slash commands, turn checkpoints, MCP settings)
- `macos/check_agent.py` (Deterministic verification suite for all harness features)
- `AGENTS.md` / `CLAUDE.md` (Repository instructions for coding agents)

> **Handoff & Continuity Note for Claude Code / Coding Agents**:
> This document is the authoritative implementation plan and progress tracker for Tier 1, Tier 2, and Tier 3 Agent Harness features.
> - Each feature below includes its **exact function signatures**, **API routes**, **UI selectors**, and **verification test names** in `macos/check_agent.py`.
> - Check the **Progress Tracker** below to see which items are already marked `[x]` (implemented & verified) and which items are `[ ]` (remaining).
> - Always run the verification commands in the **Verification Suite** section after completing any task.

---

## Progress Tracker

This tracker covers the eleven delivered Tier 1–3 features. The [Agent harness roadmap](../../agent-harness-roadmap.md) tracks next-stage milestones, acceptance evidence, and current progress separately; those next-stage capabilities are not implied by this checklist's completion.

### Tier 1: High-Impact Features for Local Models & Coding Workflows
- [x] **Feature 1.1**: Workspace Project Instructions (`AGENTS.md` / `CLAUDE.md` / `.localstudio.md` / `.cursorrules` Auto-Loading)
- [x] **Feature 1.2**: `@file` and `@folder` Mentions in the Chat Composer (`GET /api/workspace/files` + UI popover + prompt expansion)
- [x] **Feature 1.3**: Auto-Verification / Lint & Diagnostics Hook After File Edits (`_post_edit_diagnostics` on `.py`, `.json`, `.js`, `.swift`)
- [x] **Feature 1.4**: Git-Aware Tools (`git_status`, `git_diff`) & Turn Checkpoints ("Revert Entire Turn" `POST /api/agent/undo-turn`)
- [x] **Feature 1.5**: Safe Read-Only Shell Allowlist (Zero-Click Inspection & Test Commands in `run_shell`)

### Tier 2: Context, Sub-Agents & External Tooling
- [x] **Feature 2.1**: MCP (Model Context Protocol) Client Support for Custom Tools (`settings.json` `mcp_servers`, `/api/mcp/servers`, dynamic tool routing)
- [x] **Feature 2.2**: Sub-Agent Delegation (`delegate_task` / `run_subagent` Context-Isolated Worker Loop)
- [x] **Feature 2.3**: Conversation Compaction (`POST /api/chat/compact`, `/compact` command, and summary-preserving `_chat_fit_context`)

### Tier 3: Developer & Power-User UX Polish
- [x] **Feature 3.1**: Background / Long-Running Terminal Processes & Live Streaming Output (`x_tool_output` SSE + `background=True` + `/api/processes`)
- [x] **Feature 3.2**: Multi-Edit (`multi_edit_file`) & Symbol Outline (`code_outline`) Tools
- [x] **Feature 3.3**: Slash Commands / Reusable Prompt Skills (`/review`, `/test`, `/commit`, `/simplify`, `/init`, `/compact`)

---

## Detailed Implementation Specifications

### Tier 1: High-Impact Features for Local Models & Coding Workflows

#### Feature 1.1: Workspace Project Instructions (`AGENTS.md` / `CLAUDE.md` / `.localstudio.md` / `.cursorrules` Auto-Loading)

- **Problem**: When a user selects a workspace folder in the composer (`#wsPill`), the harness does not automatically read the project's `AGENTS.md`, `CLAUDE.md`, `.localstudio.md`, or `.cursorrules` and inject it into the system prompt.
- **Why it matters**: Local models (8B–35B) save 2–4 tool rounds of exploration when they immediately know the repo's build commands, test commands, and architecture rules.
- **Backend (`app.py`)**:
  1. Define `_WORKSPACE_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".localstudio.md", ".claude/CLAUDE.md", ".cursorrules")`.
  2. Implement `_load_workspace_instructions(workspace=None) -> list[dict]`:
     - Resolve workspace root via `workspace` or `_chat_workspace.get()`. If no workspace is set, return `[]`.
     - Check each candidate filename in `_WORKSPACE_INSTRUCTION_FILES`.
     - Validate each candidate path with `_harness_path_fence(cand, write=False)` and ensure it is a regular file (not a symlink outside workspace).
     - Read up to `4,000` characters per file and cap total combined instructions at `6,000` characters.
     - Return `[{"name": rel_name, "path": str(p), "content": text}]`.
  3. Update `_with_agent_note(messages, timezone=None, use_files=False, workspace=None)`:
     - When `workspace` is provided (or active in `_chat_workspace`), call `_load_workspace_instructions(workspace)`.
     - Append to `note`:
       ```text
       Workspace project instructions (from AGENTS.md / CLAUDE.md — follow these repo rules, commands, and conventions):
       <<<WORKSPACE_INSTRUCTIONS name="AGENTS.md"
       ...
       WORKSPACE_INSTRUCTIONS>>>
       ```
- **Verification (`macos/check_agent.py`)**:
  - `harness_workspace_instructions_autoload`: Create a temporary workspace containing `AGENTS.md` and `CLAUDE.md`, call `_with_agent_note(messages, workspace=str(ws))`, and assert that both instruction files are injected into the leading system message and capped appropriately.

---

#### Feature 1.2: `@file` and `@folder` Mentions in the Chat Composer

- **Problem**: Typing `@` in the chat input cannot fuzzy-search or pin workspace files/folders directly into the prompt context.
- **Backend (`app.py`)**:
  1. Add endpoint `GET /api/workspace/files`:
     - Query params: `workspace: str | None = None`, `q: str = ""`, `limit: int = 25`.
     - Validate `workspace` with `_harness_path_fence(workspace, write=False)`.
     - Walk workspace skipping `_HARNESS_SKIP_DIRS`, `_HARNESS_SENSITIVE_NAMES`, hidden directories, and `_HARNESS_SECRET_GLOBS`.
     - Collect relative file paths and folder paths (with trailing `/` for folders).
     - Score matches against `q.lower()` (exact basename prefix > substring in basename > substring in relative path > fuzzy character sequence match).
     - Return `{"workspace": str(base), "items": [{"path": str(full), "rel_path": rel, "name": name, "type": "file"|"dir", "size": size}]}`.
  2. Implement `_expand_workspace_mentions(messages: list[dict], workspace: str | None) -> tuple[list[dict], list[dict]]`:
     - Inspect the latest user message text for `@([A-Za-z0-9_./-]+)` tokens (ignoring email addresses like `user@example.com` where `@` is preceded by an alphanumeric char).
     - For each matched relative path inside `workspace` (up to 5 mentions per turn):
       - Resolve via `_in_workspace(workspace, _harness_path_fence, token_path, False)`.
       - If it is a regular file, read via `_read_local_path(str(resolved), start_line=1, max_lines=200)` and format a `<workspace_file path="rel_path">\n...\n</workspace_file>` block.
       - If it is a directory, list entries via `_chat_read_tool("list_directory", {"path": str(resolved)})` and format a `<workspace_folder path="rel_path">\n...\n</workspace_folder>` block.
     - Prepend the expanded blocks to the latest user message in `expanded_messages` and return `mentioned_sources` so the UI displays file chips.
- **Frontend (`index.html`)**:
  1. Add `#mentionMenu` popup container inside `#composerWrap` above `.composer`.
  2. On `input` / `keydown` in `#input`:
     - Detect an active `@query` token before the cursor (when `current?.workspace` is set, or using default repo workspace).
     - Fetch `/api/workspace/files?workspace=...&q=...` (debounced 80ms) and render matching files/folders with keyboard navigation (`ArrowUp`, `ArrowDown`, `Enter`/`Tab` to complete `@rel_path `, `Escape` to close).

---

#### Feature 1.3: Auto-Verification / Lint & Diagnostics Hook After File Edits

- **Problem**: `write_file` and `edit_file` do not syntax-check the edited file in the tool return payload.
- **Backend (`app.py`)**:
  1. Implement `_post_edit_diagnostics(target: Path) -> dict`:
     - Check file extension `ext = target.suffix.lower()`:
       - `.py`: Parse with `ast.parse(target.read_text(encoding="utf-8", errors="replace"), filename=str(target))`.
         - On `SyntaxError as err`: return `{"syntax_check": f"SyntaxError at line {err.lineno}: {err.msg}", "syntax_ok": False, "line": err.lineno, "offset": err.offset, "error": f"SyntaxError: {err.msg} (line {err.lineno})"}`.
         - On success: return `{"syntax_check": "OK", "syntax_ok": True, "checker": "ast.parse"}`.
       - `.json`: Parse with `json.loads(...)`.
         - On ` json.JSONDecodeError as err`: return `{"syntax_check": f"JSONDecodeError at line {err.lineno}: {err.msg}", "syntax_ok": False, "line": err.lineno, "error": str(err)}`.
         - On success: return `{"syntax_check": "OK", "syntax_ok": True, "checker": "json.loads"}`.
       - `.js` / `.mjs` / `.cjs`: If `shutil.which("node")`: run `subprocess.run(["node", "--check", str(target)], capture_output=True, text=True, timeout=4)`.
         - Return `syntax_check: "OK"` if `returncode == 0`, else the first error line from `stderr`.
       - `.swift`: If `shutil.which("swiftc")`: run `subprocess.run(["swiftc", "-parse", str(target)], capture_output=True, text=True, timeout=5)`.
         - Return `syntax_check: "OK"` if `returncode == 0`, else `stderr`.
       - Other extensions: return `{"syntax_check": "skipped", "syntax_ok": True}`.
  2. In `_chat_action_tool` for `write_file`, `edit_file`, and `multi_edit_file`:
     - Call `diag = _post_edit_diagnostics(target)`.
     - Merge `"syntax_check": diag["syntax_check"], "syntax_ok": diag["syntax_ok"], "diagnostics": diag` into the returned dict.
     - If ` not diag["syntax_ok"]`, include `"repair_hint": "Fix the syntax error reported in syntax_check before finishing."`.
  3. In `_chat_tool_detail`:
     - Append `[Syntax check: OK]` or `[Syntax check failed: ...]` to the diff preview so the user and model both see it clearly.

---

#### Feature 1.4: Git-Aware Tools (`git_status`, `git_diff`) & Turn Checkpoints ("Restore Entire Turn")

- **Problem**:
  - Multi-file turns require undoing each step one-by-one.
  - Inspecting `git status` or `git diff` requires calling `run_shell` and clicking approve.
- **Backend (`app.py`)**:
  1. Add `git_status` and `git_diff` to `CHAT_READ_TOOLS`:
     - `git_status(path: str = None)`: Runs `git status --short --branch` in the target/workspace directory (`timeout=10`), returning `{branch, status, clean, files, raw}`.
     - `git_diff(path: str = None, staged: bool = False, commit: str = None, context_lines: int = 3)`: Runs `git diff` (with `--staged` if `staged=True`, and `-- <path>` if `path` is specified), returning `{diff, stats, truncated}`.
  2. **Turn Checkpoints (`turn_id` & `POST /api/agent/undo-turn`)**:
     - Update `_backup_file(target, turn_id=None)` to store `"turn_id": turn_id` inside `AGENT_BACKUPS_DIR / f"{bid}.json"`.
     - Track a `turn_id = uuid.uuid4().hex[:16]` per `/api/chat` request and pass it into `_chat_action_tool(..., turn_id=turn_id)` (or via a `ContextVar` `_chat_turn_id`).
     - Collect all `backup_id`s created during the turn into `turn_backups: list[dict]`.
     - Whenever a file-modifying tool completes during `/api/chat`, emit SSE event:
       `x_checkpoint: {"turn_id": turn_id, "backup_ids": [b["backup_id"] for b in turn_backups], "files": [b["name"] for b in turn_backups], "count": len(turn_backups)}`.
     - Add endpoint `POST /api/agent/undo-turn`:
       - Accepts `{"turn_id": str | None, "backup_ids": list[str] | None}`.
       - Finds all matching backup JSON files (by `backup_ids` or scanning `AGENT_BACKUPS_DIR` for `turn_id`), sorts them in **reverse chronological order** (newest backup first, so multi-step edits to the same file roll back cleanly to the initial pre-turn state), restores each file, and returns `{"ok": True, "turn_id": turn_id, "restored_count": len(restored), "files": restored}`.
- **Frontend (`index.html`)**:
  - Handle `j.x_checkpoint` in `generate()` and save `bot.checkpoint = j.x_checkpoint` (and include `checkpoint` in `saveChat`).
  - When `m.checkpoint && m.checkpoint.backup_ids?.length > 0`, render a **"Revert turn (N edits)"** button (`data-undo-turn`) in the turn header/actions that calls `POST /api/agent/undo-turn` and marks all steps in the turn as `Restored`.

---

#### Feature 1.5: Safe Read-Only Shell Allowlist (Zero-Click Inspection Commands)

- **Problem**: Every `run_shell` call pauses for user approval, even safe read-only inspection or test commands (`git status`, `git diff`, `pytest`, `ls`).
- **Backend (`app.py`)**:
  1. Implement `_is_safe_readonly_shell(cmd: str) -> bool`:
     - Must pass `_gate_shell_command(cmd)[0]`.
     - Reject any shell control, chaining, redirection, subshell, or background operators:
       `any(op in cmd for op in (";", "&&", "||", "|", ">", "<", "`", "$(", "\n", "\r", "&"))`.
     - Parse with `shlex.split(cmd)`. If empty or parsing fails, return `False`.
     - Check against safe read-only patterns:
       - `git`: subcommand in `("status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "blame")` and no dangerous flags (`--output`, `--ext-diff`, `-c`, `--exec`).
       - Inspection binaries: `("ls", "pwd", "which", "whoami", "uname", "date", "wc", "head", "tail", "cat", "stat", "file")` (rejecting flags that write or execute).
       - Verification/test runners:
         - `pytest` or `python -m pytest` / `python3 -m pytest` / `python -m unittest` / `python3 -m unittest`
         - `node --check <file>`
         - `python -m py_compile <file>` / `python3 -m py_compile <file>`
         - `cargo check`, `cargo test`
         - `go test`, `go vet`
         - `npm test`, `npm run test`, `npm run lint`, `npm run check`
  2. In `harness_gate("run_shell", args, chat_id)`:
     - Check `load_settings().get("agent", {}).get("auto_approve_readonly", True)`.
     - When enabled and `_is_safe_readonly_shell(cmd)` is `True`, return `"safe", None, {**meta, "readonly_allowed": True}` so `/api/chat` executes it with zero clicks while still showing the expandable tool step and output!

---

### Tier 2: Context, Sub-Agents & External Tooling

#### Feature 2.1: MCP (Model Context Protocol) Client Support for Custom Tools

- **Problem**: Users cannot configure custom stdio MCP servers in `settings.json` to extend Local Studio's chat harness.
- **Backend (`app.py`)**:
  1. Extend `load_settings()` with `s.setdefault("mcp_servers", [])`.
     - Each server entry has schema:
       `{"id": str, "name": str, "command": str, "args": list[str], "env": dict[str, str], "enabled": bool, "trusted": bool}`.
  2. Create a custom MCP manager (`_custom_mcp_clients: dict[str, McpClient]` and `_custom_mcp_tools: dict[str, list[dict]]`):
     - Enhance `McpClient.__init__` to accept `env: dict | None = None`, `cwd: str | None = None`, and `allowed: set | None = None` (`allowed=None` allows any tool exposed by that server).
     - Add `McpClient.list_tools(timeout=15) -> list[dict]` calling `self._request("tools/list", {}, timeout).get("tools", [])`.
     - Implement `_get_mcp_chat_tools() -> tuple[list[dict], dict[str, tuple[dict, dict]]]`:
       - For each enabled server in `load_settings()["mcp_servers"]`:
         - Connect or reuse cached tool list.
         - Convert each MCP tool `{name, description, inputSchema}` into an OpenAI function schema named `mcp__{server_id}__{tool_name}`.
  3. Add REST endpoints for managing MCP servers:
     - `GET /api/mcp/servers`: Returns configured servers and their cached tool counts / status.
     - `POST /api/mcp/servers`: Adds or updates an MCP server config in `settings.json` and discovers its tools.
     - `DELETE /api/mcp/servers/{server_id}`: Stops client and removes server from `settings.json`.
     - `POST /api/mcp/servers/{server_id}/test`: Starts the server, runs `list_tools()`, and returns `{ok: True, tools: [...]}`.
  4. Route `mcp__<server_id>__<tool_name>` calls in `/api/chat`:
     - In `harness_gate()`: if `server.get("trusted")` is `True` or `"mcp"` in `_chat_session_perms.get(chat_id, set())`, return `"safe"`; otherwise return `"risky"` with an approval card showing the MCP server name, tool name, and JSON arguments.
- **Frontend (`index.html`)**:
  - Add an **MCP Servers** section in Settings (`#mcpSet`) where users can add a server (`Name`, `Command`, `Arguments`), test connection & see discovered tools, toggle enabled/trusted, or remove it.

---

#### Feature 2.2: Sub-Agent Delegation (`delegate_task` / Context-Isolated Worker)

- **Problem**: All 12 tool rounds share the main chat's 32k context window. Heavy multi-file reading pollutes the parent KV cache.
- **Backend (`app.py`)**:
  1. Add `delegate_task` to `CHAT_READ_TOOLS` (with alias `run_subagent` in `_normalize_harness_args`):
     - Schema:
       - `task` (required `string`): Detailed description of the research, file exploration, or verification task.
       - `role` (optional `string`, enum `["researcher", "verifier", "simplifier"]`, default `"researcher"`).
  2. Implement `_run_subagent_task(task: str, role: str, workspace: str | None, timezone: str | None) -> dict`:
     - Uses an isolated message list starting with a role-specific system prompt and the active workspace instructions.
     - Provides safe read-only tools (`read_file`, `list_directory`, `find_files`, `grep_files`, `code_outline`, `git_status`, `git_diff`, `web_search`, `read_webpage`, `search_files`).
     - Runs up to `SUBAGENT_MAX_ROUNDS = 4` turns against the active chat engine (or `_subagent_engine_hook` when testing offline).
     - Returns `{"role": role, "task": task, "steps_taken": steps_summary, "summary": final_text[:4000], "files_inspected": files_list}` so the parent chat receives a compact, high-signal summary without the raw multi-file payloads!

---

#### Feature 2.3: Conversation Compaction (`/compact` & Summary-Preserving `_chat_fit_context`)

- **Problem**: Long coding sessions eventually exceed the 32k token window and `_chat_fit_context()` drops whole older turns without keeping a summary.
- **Backend (`app.py`)**:
  1. Implement `_summarize_messages_deterministic(messages: list[dict]) -> str`:
     - Extracts a structured markdown state summary from older turns:
       - **User Goals & Requests**: bullet list of user prompts.
       - **Files Touched / Inspected**: paths from `read_file`, `write_file`, `edit_file`, `multi_edit_file`.
       - **Key Outcomes & Decisions**: concise excerpts from assistant conclusions.
  2. Update `_chat_fit_context(body)`:
     - When `cost() > CONTEXT - 1024` and older turns (`messages[1:next_user]`) are evicted, generate a compact `<conversation_checkpoint>` summary block of the evicted turns and insert/merge it right after the system message so earlier context is summarized rather than lost!
  3. Add `POST /api/chat/compact`:
     - Accepts `{"chat_id": str | None, "messages": list[dict], "use_llm": bool = False}`.
     - Summarizes all but the last 2 turns into a single compact checkpoint message and returns `{"ok": True, "compacted_messages": new_messages, "summary": summary_text, "before_count": len(messages), "after_count": len(new_messages)}`.

---

### Tier 3: Developer & Power-User UX Polish

#### Feature 3.1: Background / Long-Running Terminal Processes & Streaming Output

- **Problem**: `run_shell` runs synchronously for up to 30s with no live output streaming and cannot start/inspect/stop background dev servers.
- **Backend (`app.py`)**:
  1. **Background Process Registry (`_bg_processes: dict[str, dict]`)**:
     - Update `run_shell` schema to include optional `background: boolean` and `timeout: integer` (5..120s).
     - Add `manage_process` tool to `CHAT_READ_TOOLS` (actions: `"list"`, `"status"`, `"logs"`, `"stop"`, `process_id: string`) so the model can inspect output or stop a background process it started.
     - Add REST endpoints `GET /api/processes` and `POST /api/processes/{proc_id}/stop`.
  2. **Live Streaming Output (`x_tool_output` SSE)**:
     - Implement `_stream_shell_or_python(name, args, workspace, on_chunk)` or async queue streaming in `/api/chat`:
     - While `run_shell` or `run_python` runs in the foreground, stream incremental stdout/stderr lines via SSE `{"x_tool_output": {"id": key, "chunk": text}}` so the frontend updates `.chtdetail` live line-by-line!

---

#### Feature 3.2: Multi-Edit (`multi_edit_file`) & Symbol Outline (`code_outline`) Tools

- **Problem**:
  - Applying 3 edits to a file takes 3 separate tool rounds.
  - Finding function/class line ranges in a 6,000-line file like `app.py` takes multiple `grep_files` calls.
- **Backend (`app.py`)**:
  1. Add `multi_edit_file` to `CHAT_ACTION_TOOLS`:
     - Schema: `{"path": string, "edits": [{"old_text": string, "new_text": string, "replace_all": boolean}]}`.
     - In `harness_gate("multi_edit_file", ...)`: validates all edits sequentially against the file content using `_match_edit_block`, generates a single unified diff preview across all edits, and gates with `"edit"` permission scope.
     - In `_chat_action_tool("multi_edit_file", ...)`: creates a single backup (`_backup_file`), writes the updated content atomically, and runs `_post_edit_diagnostics(target)`.
  2. Add `code_outline` to `CHAT_READ_TOOLS`:
     - Schema: `{"path": string, "kind": string (optional: "all"|"classes"|"functions")}`.
     - For `.py` files: uses `ast.parse` to extract top-level classes, methods, functions/async functions (with decorators, arguments, `lineno`, and `end_lineno`), plus `# ---` section banners!
     - For `.js`/`.ts`/`.swift`/`.go`/`.rs`/`.c`/`.cpp`: uses regex/line scanner to extract classes, structs, functions, and methods with line numbers.
     - Returns `{"path": str(p), "name": p.name, "total_lines": len(lines), "symbols": symbols_list, "outline": formatted_text}`.

---

#### Feature 3.3: Slash Commands / Reusable Prompt Skills (`/review`, `/test`, `/commit`, `/simplify`, `/init`, `/compact`)

- **Problem**: No quick `/` commands in the chat composer for common developer workflows.
- **Backend (`app.py`)**:
  1. Define `SLASH_COMMANDS` registry (`GET /api/chat/commands`):
     - `/review`: Reviews uncommitted `git status` & `git diff` across 3 structured passes (`[Bugs]`, `[Security]`, `[Compliance]`).
     - `/test`: Runs the project's test suite and diagnoses any failing checks.
     - `/commit`: Inspects `git status` & `git diff`, drafts a conventional commit message, and asks before committing.
     - `/simplify`: Reviews modified code for opportunities to simplify logic and remove duplication.
     - `/init`: Explores the workspace and drafts a concise `AGENTS.md` / `CLAUDE.md` project instruction file.
     - `/compact`: Compacts older conversation turns into a structured state checkpoint.
  2. Implement `_expand_slash_command(messages: list[dict], workspace: str | None) -> list[dict]`:
     - If the latest user message starts with `/review`, `/test`, `/commit`, `/simplify`, or `/init`, automatically attach live workspace `git status` / `git diff` (for `/review`, `/commit`, `/simplify`) and expand the prompt with the structured workflow instructions.
- **Frontend (`index.html`)**:
  - When the user types `/` at the start of `#input`, open the autocomplete popover listing `/review`, `/test`, `/commit`, `/simplify`, `/init`, `/compact` with descriptions.
  - Selecting `/compact` triggers `POST /api/chat/compact` directly and updates `current.messages` in place with a toast notification!

---

## Verification Suite

Run all checks below after implementing or modifying any feature:

```sh
node macos/check_ui.js index.html
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" app.py
uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py
uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py
TMPDIR=/tmp uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_agent.py
```
