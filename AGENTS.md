# AGENTS.md

This file provides repository instructions and architectural guidance for coding agents working in `local-studio`.

## Overview

**Local Studio** is a private, on-device AI studio and local-model **Agent Harness** for Apple Silicon Macs:
- **Chat & Speculative Decoding**: MLX models accelerated by DFlash/DSpark (`mlx-dspark`), vision via `mlx-vlm`, and GGUF models on `llama.cpp` (`llama-server`).
- **Chat Agent Harness**: 24 native tools across up to 12 rounds per turn (plus dynamic custom MCP tools), with step-by-step per-round collapsible thinking (`bot.rounds` / `.chround`), intermediate commentary, workspace `AGENTS.md`/`CLAUDE.md` auto-loading, `@file`/`@folder` composer mentions, `/` slash commands (`/review`, `/test`, `/commit`, `/simplify`, `/init`, `/compact`), workspace file navigation/editing with diff previews, post-edit syntax/lint diagnostics (`_post_edit_diagnostics`), single-step & full-turn 1-click Undo (`POST /api/agent/undo-turn`), zero-click safe read-only shell allowlist, background process management & live stdout streaming (`x_tool_output`), context-isolated sub-agent delegation (`delegate_task`), conversation compaction (`POST /api/chat/compact`), live task plans (`todo_write`), persistent cross-chat memory (`memory_store`/`memory_recall`), clarifying questions (`ask_user`), web search/verification, and local semantic document search/OCR.
- **Computer-Use Agent**: Live browser and macOS app control via CuaDriver MCP (`use_computer`).
- **Multimodal Attachments & Images**: PDF/DOCX/XLSX/code attachments, scanned PDF OCR via `DocumentReader` (`PDFKit` + `Vision`), and text-to-image generation via `mflux`.

### Key Source Files

- `app.py`: the entrypoint `uvicorn app:app` loads (`APP_VERSION`, `from studio.main import app`, a live `__getattr__` for the suites).
- `studio/`: the FastAPI backend split by concern: `config.py`, `infra/`, `security/` (LocalOnly, fences, gates), `storage/` (settings, flags, memory, backups, chats), `services/` (models, engines, harness, agent, search, terminal, ...), `api/` (one APIRouter per area), `main.py` (builds the app). Imports flow down that list only. See CLAUDE.md, Backend layout.
- `index.html`: the page's markup and CSS, loading `frontend/main.mjs` as a module.
- `frontend/`: the UI's vanilla JS ES modules by concern (`core/`, `ui/`, `chat/`, `code/`, `images/`, `library/`, `settings/`, `search/`, `agent/`, boot in `main.mjs`). Modules import directly from modules that run before them and through `frontend/modules.mjs` for later ones (keeps the start-up order); names several modules assign live in `createStore` stores (`listState.current`, ...); names inline handlers call are set on `window` in `frontend/globals.mjs`. See CLAUDE.md, UI modules. `frontend/vendor/` bundles the fonts, marked and xterm.js (`VENDOR.json`: source, license, sha384 = the integrity attribute); the UI loads nothing from another host, and `check_app.py ui_loads_nothing_from_another_host` enforces it.
- `search.py` + `studio/search/` + `search-requirements.txt`: On-device semantic & lexical file search service (EmbeddingGemma 2 on MLX, port 8083); thin CLI/re-exports, same launch pattern and cwd. Every route but `/health` needs its own token (`STATE/data/search.token`, passed as `LOCAL_STUDIO_SEARCH_TOKEN` at launch), whatever `security.api_token` says.
- `voice.py` + `studio/voice/` + `voice-requirements.txt`: Voice service (port 8084, flag `voice.enabled`, off): echo-cancelled mic/speaker via AVAudioEngine voice processing, Parakeet STT, Phonon 31M TTS (`ptts`, never PyPI `phonon`). Same launch pattern as search (`studio/services/voice.py`, relative names, `cwd=ROOT`, stop.sh patterns `python voice.py --port 8084`); token in `data/voice.token`; refuses any Origin; backend never imports `studio.voice`. The conversation loop is `frontend/voice/` (the dark Voice view `#v-voice` with the ring display, controls, character picker and transcript; the mic button opens it; turn-taking, pause-first interruption, characters); spoken turns are normal chat turns via `send(text)` with `voice: true` (stripped by `harness.apply_voice_mode`). See CLAUDE.md, Voice service.
- `catalog.json`: Curated MLX, GGUF, and mflux models.
- `setup.sh` / `install.sh` / `start.sh` / `stop.sh` / `uninstall.sh`: zsh lifecycle scripts.
- `macos/`:
  - `main.swift`: Native macOS WKWebView application wrapper.
  - `document_reader.swift`: Native PDFKit + Vision OCR reader compiled to `DocumentReader`.
  - `build.sh` / `release.sh`: Verification, app bundle compilation, code signing, and DMG packaging. `release.sh --publish` (main only) publishes the notarized DMG as GitHub release v<version>, also as `Local-Studio.dmg` for the README's latest-download link; it refuses non-notarized DMGs, undated CHANGELOG entries and existing tags.
  - `check_ui.js`, `check_search.py`, `check_app.py`, `check_agent.py`, `check_document.py`: Offline verification suites.

---

## Build & Verification Commands

Always run verification checks before completing changes. `macos/build.sh` runs all syntax, UI, search, app, agent harness, and document OCR checks before building `build/Local Studio.app`:

```sh
# Run individual fast checks:
node macos/check_ui.js index.html
python3 -c "import ast,sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]" app.py studio/**/*.py
uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py
uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_app.py
TMPDIR=/tmp uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_agent.py [check_name ...]
uv run -q --no-project --python 3.12 --with pypdf python macos/check_document.py build/DocumentReader
uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt --with numpy python macos/check_voice.py
node macos/check_voice_ui.mjs
TMPDIR=/tmp uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_templates.py   # every request x every installed chat template
TMPDIR=/tmp uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_voice_e2e.py   # real voice path, attach-only to the loaded model

# Full build (runs all checks above + compiles Swift binaries + signs bundle):
./macos/build.sh

# Build and install to /Applications/Local Studio.app:
./macos/build.sh --install
```

> **Note on `TMPDIR`**: Always pass a short `TMPDIR` (such as `TMPDIR=/tmp`) when running `macos/check_agent.py` directly because macOS limits Unix domain socket paths (`cua.sock`) to 104 bytes.

---

## Architectural Invariants

### 1. Code (`ROOT`) vs. User Data (`STATE`)
- `ROOT` (where `app.py` resides; `studio/config.py` computes it as the parent of `studio/`) contains read-only application code. Inside `Local Studio.app`, `ROOT` is `Contents/Resources/app`.
- `STATE` (`$LOCAL_STUDIO_HOME`) holds all mutable user data:
  - `data/settings.json`, `data/chats/`, `data/search/index.db`
  - `data/agent/runs/`, `data/agent/memory.json`, `data/agent/backups/`
  - `gallery/`, `uploads/`, `logs/`
- **Rule**: Always resolve mutable paths from `STATE`, never `ROOT`.
- **Bytecode**: `start.sh` sets `PYTHONDONTWRITEBYTECODE=1` so Python never writes `__pycache__` into the signed `.app` bundle (which would break macOS code-signing seals).
- **Dependencies**: If you add a Python package dependency to the backend, add an exact pin to `requirements-app.txt`, which `start.sh`, `APP_DEPS` in `setup.sh` and the checks all read. If you add a new runtime file, add it to the copy list in `macos/build.sh`.

### 2. Single Active Chat Engine
Only one heavy LLM engine process runs at a time (`ensure_engine(mode, model)` under `engine_lock`):
- `text` (port 8080): `mlx-dspark serve` (MLX + DFlash/DSpark speculative decoding)
- `vision` (port 8081): `python -m mlx_vlm server`
- `gguf` (port 8082): `llama-server`
- Process match patterns in `ENGINES` (`studio/config.py`) and `stop.sh` must stay synchronized.
- Never stop or restart engines against the user's live `LOCAL_STUDIO_HOME` during tests; isolate dev servers on a separate port (e.g. `7861`) and temporary `LOCAL_STUDIO_HOME`.

### 3. Chat Agent Harness (`studio/services/harness.py`, `studio/security/`, `studio/api/chat.py` & `frontend/chat/`)
`POST /api/chat` implements a multi-round native tool loop (`CHAT_TOOLS`, up to `CHAT_TOOL_ROUNDS = 12`):
- **Read-Only Tools (`CHAT_READ_TOOLS`)**:
  - `get_current_datetime`, `web_search`, `read_webpage`, `search_files`, `read_file`, `list_directory`, `find_files`, `grep_files`, `code_outline`, `git_status`, `git_diff`, `delegate_task`, `manage_process`, `todo_write`, `memory_store`, `memory_recall`.
  - `read_file` supports both indexed `file_id` (via `DocumentReader` PDFKit + Vision OCR, clamping `start_page >= 1` and `max_pages` in `1..4`) and workspace/local `path` (text/code with line numbers or PDF/DOCX/XLSX). If a model passes a candidate filename from `search_files` in `path`, `read_file` automatically resolves it to the candidate's `file_id`.
  - Personal documents (passports, visas, I-94s, EADs, tax files, receipts, notes) must always use `search_files` / `read_file` rather than `web_search`.
- **Gated Action & Interaction Tools (`CHAT_ACTION_TOOLS`)**:
  - `write_file`, `edit_file`, `multi_edit_file`, `run_python`, `run_shell`, `ask_user` (plus custom MCP tools `mcp__<server_id>__<tool>`).
  - `write_file`, `edit_file`, and `multi_edit_file` generate unified diff previews (`_make_diff_preview`), use 3-tier tolerant block matching (`_match_edit_block`: exact → stripped line-number prefixes → normalized whitespace), run automatic syntax/lint verification (`_post_edit_diagnostics`), save pre-edit backups in `STATE/data/agent/backups/<backup_id>.json`, and expose both per-step `backup_id` (`POST /api/agent/undo`) and turn-level `turn_id` (`POST /api/agent/undo-turn`) for 1-click Undo.
  - Safe read-only shell commands (`_is_safe_readonly_shell`: `git status`, `git diff`, `git log`, `pytest`, `ls`, `node --check`, etc.) are auto-approved without confirmation clicks while mutating commands remain gated.
  - `_ChatSession` pauses the SSE stream on `x_approval` or `x_question` and waits for `POST /api/chat/sessions/{sid}/reply` (`allow`, `allow_session` per `chat_id` and category `edit`/`exec`/`mcp`, or `deny`).
- **Safety Fences**:
  - `_harness_path_fence(path, write=...)` blocks sensitive credentials/keys (`~/.ssh`, `~/.aws`, `~/.gnupg`, `Keychains`, `.env` except `.env.example`/`.sample`/`.template`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.keychain*`) and system directories on write.
  - `_gate_shell_command(cmd)` blocks `sudo`, `rm -rf /` or `~`, `curl|sh`, `dd if=`, `mkfs`, `launchctl`, keychain dumps, and fork bombs.
- **Local-Model Resiliency**:
  - `_parse_harness_args()` and `_normalize_harness_args()` repair malformed JSON tool arguments and map common parameter aliases (`filepath` → `path`, `old_str` → `old_text`, `cmd` → `command`, `run_subagent` → `delegate_task`, etc.).
  - `_chat_fit_context()` masks older tool outputs in multi-round turns and preserves a structured `<conversation_checkpoint>` summary when evicting older turns so the context window (`config.CONTEXT`, 128k by default and at least 100k, configurable in Settings → Context window; per-model `models.effective_context()` clamps it to the model's native limit, and replayed reasoning counts toward the budget) does not lose earlier context. Fitting reserves room for the reply and for that summary.
  - Workspace path isolation uses `contextvars.ContextVar` (`_chat_workspace`) rather than mutating process-wide `os.getcwd()`.
- **Step-by-Step Per-Round UI (`frontend/chat/`)**:
  - Multi-round turns record each completed tool round in `bot.rounds` (`{thinking, thoughtMs, text, tools}`) and render `.chround` blocks chronologically so each round shows its own collapsible `Thought for Xs · N words`, intermediate note (`.chround-note`), live streaming output (`x_tool_output`), turn checkpoint rollback (`x_checkpoint`), and expandable tool steps (`.chattoolsteps`) before the final answer.
- **Code view (`#v-code`, `frontend/code/`)**:
  - Three panes: explorer (Files / Changes / Outline), editor with Code/Diff modes plus a terminal, and the agent chat. `syncChatDock()` moves the single chat UI (`#chatScroll`, `.welcome`, `#composerWrap`) between `#v-chat` and `#codeChatDock`; use `chatHost()` rather than assuming `#v-chat`.
  - Backed by `GET /api/workspace/info|file|outline`, `POST /api/workspace/file` (Save via `write_file`: backup + diagnostics), `POST /api/workspace/settings`, and `/api/processes`. Refreshes never overwrite unsaved editor changes (`codeDirty()`).
  - The terminal is a real login zsh on a pty (`_Terminal`, `/api/terminals*`: SSE output with absolute offsets so a reload reattaches; an idle stream ends after 3 s so shutdown never waits on it and the page reconnects; input/resize/delete; max 8; closed at exit), drawn with xterm.js loaded on first use from `frontend/vendor/xterm/` with SRI hashes (`XTERM_FILES`).
- **Replies outlive the view**: `liveGen` registers the running reply; `renderThread` re-attaches it (`liveGen.attach()`) after any redraw, the live chat reopens from memory, and chats are saved at send. Never rebuild `#thread` assuming nothing is streaming. Context used/free: `ctxUsage()`/`updateCtx()`.
- **Permission prompts**: `permission_mode: "accept_edits"` on `/api/chat` grants the `edit` scope for that request only, for files inside the workspace only (`harness_gate(..., extra_perms)`; a write outside it still asks) and is never persisted by the UI; a declined approval may carry `feedback` that is returned to the model. UI: numbered options (1/2/3, Esc declines), Shift+Tab toggles the mode, Esc stops a reply.

---

## Things Coding Agents Get Wrong (Must Avoid)

- Never resolve mutable data paths (`gallery/`, `uploads/`, `logs/`, `data/`) from `ROOT`; always resolve from `STATE` (`$LOCAL_STUDIO_HOME`), because `ROOT` is read-only inside the signed `Local Studio.app` bundle.
- Never run `stop.sh` or start/stop an engine against the user's live `LOCAL_STUDIO_HOME` during tests; always isolate test runs with a temporary `LOCAL_STUDIO_HOME` and a short `TMPDIR=/tmp` (macOS limits `cua.sock` paths to 104 bytes).
- Never mutate process-wide `os.getcwd()` in the backend; always use the `_chat_workspace` `ContextVar` (`_in_workspace(path)`).
- Never skip, weaken, or delete a failing test in `macos/check_*.py`; fix the implementation code instead.
- A name rebound at run time (a suite's patch or a `global`) is read as `owner.X` in other modules, never via `from owner import X`; suites patch the defining module (`studio.services.engines.ensure_engine = fake`), and `macos/patch_guard.py` rejects `app.X = ...` for names `app.py` does not define. `check_app.py` enforces this and compares route/SSE/patch-point contracts in `macos/fixtures/`.
- Behavior changes go behind flags in `data/settings.json` that default to the old behavior (`studio/storage/flags.py`).
- `node macos/check_ui.js` only loads the UI module graph against a stub DOM and catches start-up errors (TDZ, missing exports) (whose `window` has no `addEventListener`); mismatched selectors, `data-*` attributes or response fields between HTML, JS and the backend pass it silently. Check UI changes in the `agent-ui-dev` dev server from `.claude/launch.json` (port 7861, `LOCAL_STUDIO_HOME=build/agent-ui-state`), with the Code view pointed at a scratch folder, not the checkout. That server shares engine ports with the live app, so check agent turns with synthetic messages rendered client-side (`window.LS.current`, `LS.renderThread()`, or a stubbed `fetch` for `POST /api/chat`), never a real chat.
- Theme colors come only from the `:root` tokens (keep light, `data-theme="dark"` and the `prefers-color-scheme` copy in sync); `--brand` is the terracotta accent, `--serif` is for Chat replies and the greeting only.

---

## Tier 1, Tier 2 & Tier 3 Implementation Plan & Progress Tracker

See `docs/superpowers/plans/2026-10-17-ai-native-sdlc-agent-harness.md` for full architectural specifications, API routes, and verification tests for all Tier 1–3 Agent Harness features (all done). The next-stage features (Plan/Act/Auto tasks with `intent.md`/`spec.md`/`plan.md`, hooks and test protection, skills with `load_skill`, nested instructions, durable runs, structured reviews, triggers, isolated write workers, and the `macos/evals/` kit) exist behind default-off `harness.*` flags. Reference: [docs/workflow-automation.md](docs/workflow-automation.md), [docs/harness-resources.md](docs/harness-resources.md), [docs/verification-gate.md](docs/verification-gate.md), [macos/evals/README.md](macos/evals/README.md). Triggers only **queue** work (a person starts it); workers never touch the workspace until a reviewed patch is integrated; evals never start or stop an engine.

See [Agent harness roadmap](docs/agent-harness-roadmap.md) for the next-stage milestones, acceptance criteria, and progress. Its next-stage items are tracked separately from the completed Tier 1–3 baseline; consult their current status before claiming delivery.


## P3 security controls

See `docs/security.md` for the OWASP Agentic Top 10 control map, remaining exposures, and flag configuration.
`security.redact`, `security.upload_sniff`, and `security.mcp_approval` default to false; `security.csp` defaults to "off".
Enforcing CSP was authorized and verified in isolated development as part of completing the plan; the default remains off.
`security.api_token`, `harness.sandbox`, and `harness.rlimits` also default to false; see the security document for opt-in setup.
Subprocess logs use `studio/infra/logs.py::open_log` so redaction, when enabled, happens before disk writes.
New persistent JSON writes should use `studio/storage/atomic.py::write_text_atomic`.

CI (`.github/workflows/checks.yml`) runs `macos/build.sh` on Apple Silicon for every pull request and push to `main`; checks there have no models or engine environments and must skip, not fail, without them. Bump `APP_VERSION` for each behavior-changing release and add a `CHANGELOG.md` entry.

P4/P6 flags `performance.isolated_io`, `performance.chat_index`, `streams.resume`, and `search.incremental` default to false.
Search has its own route/auth contract check in `macos/check_search_contract.py`; its payloads are independent of backend SSE.

`harness.coding_workflow` defaults to false and enables workspace-first search plus repair/retest guidance.
Code chats persist the selected workspace before sending. Foreground execution uses bounded output and a
per-turn cancellation event; preserve process-group cleanup on Stop/timeout and nonzero-exit error reporting.
