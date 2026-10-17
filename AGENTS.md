# AGENTS.md

This file provides repository instructions and architectural guidance for coding agents working in `local-studio`.

## Overview

**Local Studio** is a private, on-device AI studio and local-model **Agent Harness** for Apple Silicon Macs:
- **Chat & Speculative Decoding**: MLX models accelerated by DFlash/DSpark (`mlx-dspark`), vision via `mlx-vlm`, and GGUF models on `llama.cpp` (`llama-server`).
- **Chat Agent Harness**: 18 native tools across up to 12 rounds per turn, with step-by-step per-round collapsible thinking (`bot.rounds` / `.chround`), intermediate commentary, workspace file navigation/editing with diff previews & 1-click Undo, local Python/shell execution with interactive session approvals, live task plans (`todo_write`), persistent cross-chat memory (`memory_store`/`memory_recall`), clarifying questions (`ask_user`), web search/verification, and local semantic document search/OCR.
- **Computer-Use Agent**: Live browser and macOS app control via CuaDriver MCP (`use_computer`).
- **Multimodal Attachments & Images**: PDF/DOCX/XLSX/code attachments, scanned PDF OCR via `DocumentReader` (`PDFKit` + `Vision`), and text-to-image generation via `mflux`.

### Key Source Files

- `app.py`: Single-file FastAPI backend (~6,050 lines, organized by `# ----` section banners).
- `index.html`: Single-page vanilla JS + CSS frontend (~3,045 lines).
- `search.py` + `search-requirements.txt`: On-device semantic & lexical file search service (~1,410 lines, EmbeddingGemma 2 on MLX, port 8083).
- `catalog.json`: Curated MLX, GGUF, and mflux models.
- `setup.sh` / `install.sh` / `start.sh` / `stop.sh` / `uninstall.sh`: zsh lifecycle scripts.
- `macos/`:
  - `main.swift`: Native macOS WKWebView application wrapper.
  - `document_reader.swift`: Native PDFKit + Vision OCR reader compiled to `DocumentReader`.
  - `build.sh` / `release.sh`: Verification, app bundle compilation, code signing, and DMG packaging.
  - `check_ui.js`, `check_search.py`, `check_app.py`, `check_agent.py`, `check_document.py`: Offline verification suites.

---

## Build & Verification Commands

Always run verification checks before completing changes. `macos/build.sh` runs all syntax, UI, search, app, agent harness, and document OCR checks before building `build/Local Studio.app`:

```sh
# Run individual fast checks:
node macos/check_ui.js index.html
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" app.py
uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py
uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py
TMPDIR=/tmp uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_agent.py [check_name ...]
uv run -q --no-project --python 3.12 --with pypdf python macos/check_document.py build/DocumentReader

# Full build (runs all checks above + compiles Swift binaries + signs bundle):
./macos/build.sh

# Build and install to /Applications/Local Studio.app:
./macos/build.sh --install
```

> **Note on `TMPDIR`**: Always pass a short `TMPDIR` (such as `TMPDIR=/tmp`) when running `macos/check_agent.py` directly because macOS limits Unix domain socket paths (`cua.sock`) to 104 bytes.

---

## Architectural Invariants

### 1. Code (`ROOT`) vs. User Data (`STATE`)
- `ROOT` (where `app.py` resides) contains read-only application code. Inside `Local Studio.app`, `ROOT` is `Contents/Resources/app`.
- `STATE` (`$LOCAL_STUDIO_HOME`) holds all mutable user data:
  - `data/settings.json`, `data/chats/`, `data/search/index.db`
  - `data/agent/runs/`, `data/agent/memory.json`, `data/agent/backups/`
  - `gallery/`, `uploads/`, `logs/`
- **Rule**: Always resolve mutable paths from `STATE`, never `ROOT`.
- **Bytecode**: `start.sh` sets `PYTHONDONTWRITEBYTECODE=1` so Python never writes `__pycache__` into the signed `.app` bundle (which would break macOS code-signing seals).
- **Dependencies**: If you add a Python package dependency to `app.py`, add it to **both** `start.sh` and `APP_DEPS` in `setup.sh`. If you add a new runtime file, add it to the copy list in `macos/build.sh`.

### 2. Single Active Chat Engine
Only one heavy LLM engine process runs at a time (`ensure_engine(mode, model)` under `engine_lock`):
- `text` (port 8080): `mlx-dspark serve` (MLX + DFlash/DSpark speculative decoding)
- `vision` (port 8081): `python -m mlx_vlm server`
- `gguf` (port 8082): `llama-server`
- Process match patterns in `ENGINES` (`app.py`) and `stop.sh` must stay synchronized.
- Never stop or restart engines against the user's live `LOCAL_STUDIO_HOME` during tests; isolate dev servers on a separate port (e.g. `7861`) and temporary `LOCAL_STUDIO_HOME`.

### 3. Chat Agent Harness (`app.py` & `index.html`)
`POST /api/chat` implements a multi-round native tool loop (`CHAT_TOOLS`, up to `CHAT_TOOL_ROUNDS = 12`):
- **Read-Only Tools (`CHAT_READ_TOOLS`)**:
  - `get_current_datetime`, `web_search`, `read_webpage`, `search_files`, `read_file`, `list_directory`, `find_files`, `grep_files`, `todo_write`, `memory_store`, `memory_recall`.
  - `read_file` supports both indexed `file_id` (via `DocumentReader` PDFKit + Vision OCR, clamping `start_page >= 1` and `max_pages` in `1..4`) and workspace/local `path` (text/code with line numbers or PDF/DOCX/XLSX). If a model passes a candidate filename from `search_files` in `path`, `read_file` automatically resolves it to the candidate's `file_id`.
  - Personal documents (passports, visas, I-94s, EADs, tax files, receipts, notes) must always use `search_files` / `read_file` rather than `web_search`.
- **Gated Action & Interaction Tools (`CHAT_ACTION_TOOLS`)**:
  - `write_file`, `edit_file`, `run_python`, `run_shell`, `ask_user`.
  - `write_file` and `edit_file` generate unified diff previews (`_make_diff_preview`), use 3-tier tolerant block matching (`_match_edit_block`: exact → stripped line-number prefixes → normalized whitespace), save pre-edit backups in `STATE/data/agent/backups/<backup_id>.json`, and expose `backup_id` for 1-click Undo (`POST /api/agent/undo`).
  - `_ChatSession` pauses the SSE stream on `x_approval` or `x_question` and waits for `POST /api/chat/sessions/{sid}/reply` (`allow`, `allow_session` per `chat_id` and category `edit`/`exec`, or `deny`).
- **Safety Fences**:
  - `_harness_path_fence(path, write=...)` blocks sensitive credentials/keys (`~/.ssh`, `~/.aws`, `~/.gnupg`, `Keychains`, `.env` except `.env.example`/`.sample`/`.template`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.keychain*`) and system directories on write.
  - `_gate_shell_command(cmd)` blocks `sudo`, `rm -rf /` or `~`, `curl|sh`, `dd if=`, `mkfs`, `launchctl`, keychain dumps, and fork bombs.
- **Local-Model Resiliency**:
  - `_parse_harness_args()` and `_normalize_harness_args()` repair malformed JSON tool arguments and map common parameter aliases (`filepath` → `path`, `old_str` → `old_text`, `cmd` → `command`, etc.).
  - `_chat_fit_context()` masks older tool outputs in multi-round turns so the 32k context window does not overflow.
  - Workspace path isolation uses `contextvars.ContextVar` (`_chat_workspace`) rather than mutating process-wide `os.getcwd()`.
- **Step-by-Step Per-Round UI (`index.html`)**:
  - Multi-round turns record each completed tool round in `bot.rounds` (`{thinking, thoughtMs, text, tools}`) and render `.chround` blocks chronologically so each round shows its own collapsible `Thought for Xs · N words`, intermediate note (`.chround-note`), and expandable tool steps (`.chattoolsteps`) before the final answer.

---

## Things Coding Agents Get Wrong (Must Avoid)

- Never resolve mutable data paths (`gallery/`, `uploads/`, `logs/`, `data/`) from `ROOT`; always resolve from `STATE` (`$LOCAL_STUDIO_HOME`), because `ROOT` is read-only inside the signed `Local Studio.app` bundle.
- Never run `stop.sh` or start/stop an engine against the user's live `LOCAL_STUDIO_HOME` during tests; always isolate test runs with a temporary `LOCAL_STUDIO_HOME` and a short `TMPDIR=/tmp` (macOS limits `cua.sock` paths to 104 bytes).
- Never mutate process-wide `os.getcwd()` in `app.py`; always use the `_chat_workspace` `ContextVar` (`_in_workspace(path)`).
- Never skip, weaken, or delete a failing test in `macos/check_*.py`; fix the implementation code instead.

---

## AI-Native SDLC & Agent Harness Phase 2 Playbook

To implement the next set of Agent Harness capabilities modeled on [Anthropic's AI-Native SDLC Playbook](https://claude.com/resources/articles/the-ai-native-sdlc-playbook), follow the task-by-task implementation plan in:
- `docs/superpowers/plans/2026-10-17-ai-native-sdlc-agent-harness.md`

### Summary of Phase 2 Tasks in the Playbook:
1. **Task 1 — Workspace `CLAUDE.md` / `AGENTS.md` Auto-Loading & `/init`**: Auto-inject repo conventions and verification commands into `_with_agent_note()`.
2. **Task 2 — Plan Mode / Act Mode / Auto Mode (`intent.md` $\rightarrow$ `spec.md` $\rightarrow$ `plan.md`)**: 3-state composer toggle + 1-click "Approve Plan & Build".
3. **Task 3 — Post-Edit Verification Loop & Hooks (`PreToolUse` / `PostToolUse`)**: Automatic syntax/lint check inside `write_file`/`edit_file` results, test-file protection during bug fixes, and `.localstudio/hooks.json` support.
4. **Task 4 — Safe Read-Only Shell Allowlist**: Pre-approve safe inner-loop inspection/test commands (`git status`, `git diff`, `pytest`, etc.) without approval prompt fatigue.
5. **Task 5 — Skills (`.claude/skills/<name>/SKILL.md` & `.agents/skills/`)**: On-demand institutional knowledge discovery and `load_skill(name)` tool.
6. **Task 6 — Context-Isolated Subagents & Turn Checkpoints**: `run_subagent(role, task)` (`researcher`, `verifier`, `simplifier`) running in a fresh 32k context window, plus turn-level 1-click Undo (`POST /api/agent/undo-turn`).
7. **Task 7 — Multi-Pass `REVIEW.md` Code Review (`/review`)**: 3-pass review (`Bugs`, `Security`, `Compliance`) with capped nits and feedback into `CLAUDE.md`.
8. **Task 8 — `@file` Composer Mentions & `/compact` Conversation Summarization**.
9. **Task 9 — Continuous Declarative Evals (`macos/evals/*.json`) & Closing the Loop (`POST /api/agent/trigger` $\rightarrow$ `intent.md`)**.

