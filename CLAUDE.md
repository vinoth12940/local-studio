# CLAUDE.md

Coding harness corrections: new Code chats bind and persist the selected workspace before sending.
`harness.coding_workflow` defaults to false; enable it under `harness` in STATE/data/settings.json for
workspace-first `search_files` and diagnosis/repair/retest guidance. `scope=workspace` searches directly
without the personal-file index; `scope=indexed` explicitly requests indexed documents.
Foreground Python/shell tools report nonzero exits as errors, retain bounded diagnostic tails, and terminate
their process group on timeout or chat Stop. These correctness fixes preserve existing approval gates.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Local Studio is a private, on-device AI studio and local-model Agent Harness for Apple Silicon Macs:
chat (MLX models with DFlash/DSpark speculative decoding, or GGUF on llama.cpp), multi-step workspace
coding/research agent harness, browser/macOS computer use, document/image attachments, on-device
semantic file search, and text-to-image via mflux. It ships as a self-contained macOS app
(`Local Studio.app`) and also runs from a source checkout. The source files:

- `app.py`: the entrypoint `uvicorn app:app` loads (23 lines: `APP_VERSION`, `from studio.main import app`, and a live
  `__getattr__` so the check suites can read any backend name as `app.X`)
- `studio/`: the FastAPI backend (~8,400 lines), split by concern; see Backend layout below
- `requirements-app.txt`: exact pins of the backend's Python packages
- `index.html`: the page's markup and CSS (~1,240 lines; `marked` from a CDN) and `<script type="module" src="frontend/main.mjs">`
- `frontend/`: the UI's code, vanilla JS ES modules by concern (see UI modules below); served read-only from `ROOT` at `/frontend/`
- `catalog.json`: curated chat and image models
- `search.py` + `search-requirements.txt`: the file-search service (~1,410 lines; EmbeddingGemma 2 on MLX) and its dependencies
- `setup.sh` / `install.sh` / `start.sh` / `stop.sh` / `uninstall.sh`: zsh lifecycle scripts
- `macos/`: native wrapper (`main.swift`, a WKWebView around the UI), PDF/Vision OCR reader (`document_reader.swift`), icon generator (`logos.py`), verification suites (`check_*.py`, `check_ui.js`), and build/release scripts

## Commands

```sh
./setup.sh --install      # install engines (uv, mlx-dspark, mflux, llama.cpp); no models
./setup.sh --check        # status report; exit 0 ready, 1 needs install, 2 unsupported Mac
./start.sh                # UI on http://127.0.0.1:7860 + engine for the active chat model
./start.sh --ui           # UI only (start the engine later from the UI)
./stop.sh                 # stop all engines, the UI and image jobs

macos/build.sh            # build build/Local Studio.app (also runs all pre-PR verification checks)
macos/build.sh --install  # build and copy to /Applications
macos/release.sh          # build + DMG in build/, signs/notarizes if a Developer ID is set up
```

`macos/build.sh` is the primary validation step before a PR or release: it runs `node macos/check_ui.js index.html`
(executes the UI script against a stub DOM so start-up errors such as a `const` used before its
declaration fail the build), `ast.parse`s `app.py` and every file under `studio/`, and runs the offline verification
suites (`check_search.py`, `check_app.py`, `check_agent.py`, `check_document.py`). Individual checks:

```sh
node macos/check_ui.js index.html
node macos/check_sanitizer.mjs     # the chat markdown sanitizer, under plain node
python3 -c "import ast,sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]" app.py studio/**/*.py
uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py
uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_app.py
TMPDIR=/tmp uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_agent.py [check_name ...]
uv run -q --no-project --python 3.12 --with pypdf python macos/check_document.py build/DocumentReader
uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --eval build/search_samples
```

`macos/check_agent.py` (~5,570 lines) verifies both the CuaDriver computer-use agent (gate table, view
trimmer, history folding, MCP client with a fake `cua-driver`, scripted runs) and the Chat Agent
Harness (`harness_argument_repair_and_normalization`, `harness_path_and_shell_safety_fences`,
`harness_tolerant_edit_matching`, `harness_backup_and_undo`, `harness_session_approval_and_reply`,
`harness_memory_and_todo`, plus multi-round tool loops, personal document searches, and OCR reads).
Pass check function names as arguments to run only those. The short `TMPDIR` matters: the Unix
domain socket path has a 104-byte limit on macOS. The search eval needs the model and a sample folder
built by `macos/make_search_samples.py build/search_samples`.

**Never test against the user's running Mac app.** Run a dev server on port 7861 with its own short
`LOCAL_STUDIO_HOME` (plus `LOCAL_STUDIO_SEARCH_PORT`/`LOCAL_STUDIO_SEARCH_ROOTS`, see Search service);
never run `stop.sh`, start an engine or start a CuaDriver daemon against the live app, because the
engine ports and process patterns are shared. `.claude/launch.json` defines such a server
(`agent-ui-dev`: port 7861, `LOCAL_STUDIO_HOME=build/agent-ui-state`, search port 8093). Its default
harness workspace is the checkout itself, so point the Code view at a scratch folder under
`build/agent-ui-state/` before testing Save, the terminal or agent edits.

`check_ui.js` only loads the UI's module graph (`frontend/main.mjs` and its imports) against a stub DOM to catch
start-up errors (a temporal-dead-zone access, a missing export): a selector, `data-*` attribute or response field that
HTML, JS and the backend disagree on passes it silently, so check UI changes in the dev server. The stub `window` has
no `addEventListener` (use `window.on…` properties), and code that runs at start-up must not touch a `let` declared
further down (even `typeof` throws in its dead zone). The dev server shares engine ports with the running app: check
agent turns from the console with the `window.LS` handle (`LS.current.messages.push(...)`, then `LS.renderThread()`),
or stub `fetch` for `POST /api/chat` with a synthetic SSE stream; never send a real chat.

### UI modules (`frontend/`)

Split from `index.html`'s inline script in the refactor's P2 (`docs/refactor-progress.json`), moved, not redesigned:
`core/` (`util`: `$`, `esc`, `store`, `api`/`jpost`, `toast`; `markdown`: the allowlist sanitizer; `store`: `createStore`),
`ui/` (`theme`, `views`, `engine`), `chat/` (`list`, `thread`, `attachments`, `composer`, `live`, `computer`),
`images/`, `code/` (`editor`, `terminal`), `library/`, `settings/` (`settings`, `agent`), `search/`, `agent/`
(`helpers`: pure, plain node can import it; `state`; `agent`), and `main.mjs` (boot only).

- **Order.** Modules run in the order their code had: `frontend/modules.mjs` lists them that way. A module imports
  directly from modules that run before it, and through `modules.mjs` for anything later (a direct import would make
  that module run first and change the order of start-up statements). Functions can be called across modules at any
  time; a `let`/`const` from a later module is in its dead zone until that module has run.
- **Stores.** An ES module cannot assign a binding it imports, so a name more than one module assigns lives in its
  module's store (`createStore`, `core/store.mjs`): `listState` (`current`, `streaming`, `ctxWindow`, `codeWorkspace`,
  `chRun`), `threadState` (`stick`, `sysRam`), `attachmentsState` (`pending`), `libraryState` (`runningName`,
  `runningMode`), `searchState` (`noFilesOnce`). Write `listState.current = chat`, never a local copy. Stores notify
  subscribers on writes; nothing subscribes yet.
- **Globals.** Module code is not global: anything an inline handler or an `innerHTML` template calls by name must be
  set on `window` in `frontend/globals.mjs` (today only `showView`). Prefer delegated listeners over new inline handlers.

The look is a warm palette driven by the tokens in `:root` (light, `data-theme="dark"` and the
`prefers-color-scheme` copy must stay in sync): `--brand` (terracotta) marks primary actions, running state and
focus; `--accent` stays near-black/white for ordinary primary buttons; `--serif` is used for Chat replies and the
welcome greeting only (`#v-chat .turn.bot .content`), never in the Code view.

`AGENTS.md` is a shorter overlapping summary for other coding agents; when an invariant here changes,
update it there too.

The backend has no project/venv: `start.sh` runs it with
`uv run --no-project --python 3.12 --with-requirements requirements-app.txt uvicorn app:app`. `requirements-app.txt`
holds exact pins and is shared by `start.sh`, `APP_DEPS` in `setup.sh` and the check suites; add a Python dependency
there, never as a bare `--with`. `search-requirements.txt` is pinned the same way (its hash change re-runs Search setup). After editing
`app.py` or `studio/`, restart the UI server (`./stop.sh && ./start.sh --ui`); there is no auto-reload. Logs and
pid files go to `logs/` (`ui.log`, `llm.log`, `vlm.log`, `gguf.log`, `download-<id>.log`).

## Architecture

### Backend layout and the import rule

`studio/` was split out of the single-file `app.py` (refactor P1, `docs/refactor-progress.json`); each module keeps the
names it had there. Imports only flow down this list (no cycles); `studio/main.py` lists every module in this order
(`MODULES`) and builds the app:

- `config.py`: `ROOT`/`STATE` and the paths under them, ports, `ENGINES`, `CATALOG`, `agent_lock`
- `infra/`: `sse`, `system` (memory, pids, the clock tool), `text`, `edits` (edit matching, diff previews, post-edit
  diagnostics), `harness_args` (argument repair), `workspace` (the `_chat_workspace` ContextVar), `processes`
  (`run_shell`/`run_python` subprocesses), `logs` (opt-in redacting subprocess descriptors and Python formatters), `mcp_client` (stdio MCP client + the CuaDriver allowlist), `request_log`
- `security/`: `local_only`, `fences` (path fence, shell gate, read-only allowlist), `harness_gate`, `agent_gate`,
  `headers` (nosniff/CSP), `uploads` (magic signatures), `redact` (tokens and streamed PEM blocks), `workflow_policy`
  (Plan/Act/Auto policy, hooks, test protection; opt-in)
- `storage/`: `settings`, `flags`, `memory`, `backups`, `chats`, `atomic`
- `services/`: `models`, `engines`, `attachments`, `images`, `search`, `terminal`, `web`, `mcp_custom`, `agent/{cua,views,loop}`,
  `harness` (the chat agent loop's tools, sessions, sub-agents and compaction), `updates`, and the opt-in workflow stack:
  `instructions`, `skills`, `workflow` (durable tasks/artifacts), `workflow_runs` (restart-safe run journal), `verification`
  (completion gate), `reviews` (fresh-context review + dispositions), `triggers` (queue-only schedules), `worktrees` (isolated
  write workers); see [docs/workflow-automation.md](docs/workflow-automation.md)
- `api/`: one `APIRouter` per area (`system`, `models`, `engines`, `chat`, `memory`, `undo`, `processes`, `terminal`, `mcp`,
  `workspace`, `attachments`, `chats`, `images`, `search`, `agent`, `updates`, `storage`, `workflow`, `workflow_runs`, `reviews`,
  `triggers`, `worktrees`), included by `studio/main.py`

Two exceptions to "down only": `security/harness_gate.py` reads custom MCP trust flags from `services/mcp_custom.py`, and
`security`/`infra` modules use small `infra`/`storage` helpers. Keep each module's locks and global dicts in that module, and
keep sync handlers sync (they run in the threadpool and take `threading.Lock`s).

**Names rebound at run time are read through their owner.** A check suite's `X = fake` patch, or a `global X`, rebinds the
name in the module that defines it. Other modules must read it as `owner.X` (`engines.ensure_engine(...)`,
`config.CONTEXT`), never via `from owner import X`, which takes a copy the rebind never reaches. Which names these are:
`macos/fixtures/patch_points.json` plus the `global`s (`active_chats`, `_agent_cli`, `_agent_current`, `_agent_last_use`,
`_agent_reaper`, `_cua_grant`); `check_app.py rebound_names_are_read_through_their_owner` enforces it.

**Patching in the suites.** Patch the module that defines the name: `studio.services.engines.ensure_engine = fake`, not
`app.ensure_engine`. The suites install `macos/patch_guard.py`, which raises on `app.X = ...` for anything `app.py` does not
define; save/restore loops use `patch_guard.owner(app, name)`. Reading `app.X` is fine (it forwards to the owner, live).

**Contracts.** `check_app.py` compares the routes (`macos/fixtures/routes.json`; routes that could answer the same request
must keep their order; the full ordered snapshot also rejects independent-route reordering), the SSE events and their fields plus the UI's handlers (`sse_contract.json`), and the patch points
(`patch_points.json`). A change that is meant updates the fixture in the same commit:
`LOCAL_STUDIO_UPDATE_FIXTURES=1 uv run ... python macos/check_app.py route_contract_is_unchanged`.

**Flags.** Behavior changes from the refactor sit behind flags in `data/settings.json` that default to the old behavior
(`studio/storage/flags.py`, `FLAGS`): `logging.json_requests` (off) writes slow, failing and non-GET requests to
`logs/requests.jsonl`. P3 adds `security.redact` (false), `security.upload_sniff` (false),
`security.mcp_approval` (false), `security.api_token` (false), `harness.sandbox` (false),
`harness.rlimits` (false), and `security.csp` ("off"; opt in with "report-only").
See [docs/security.md](docs/security.md) for every flag, configuration examples, coverage and remaining P3 work.
The user authorized completing the plan, including isolated enforcement validation. Production defaults remain unchanged. P4 adds `performance.isolated_io`, `performance.chat_index`, and
`streams.resume`, and `search.incremental` (all false); see the same document for opt-in settings and replay retention/stop behavior.
The workflow stack adds `harness.verification_gate`, `durable_runs`, `verification_fingerprints`, `nested_instructions`, `skills`,
`hooks`, `protect_tests`, `workflow`, `review`, `triggers` and `worktrees` (all false); each is documented in
[docs/workflow-automation.md](docs/workflow-automation.md), [docs/harness-resources.md](docs/harness-resources.md) or
[docs/verification-gate.md](docs/verification-gate.md). `LOCAL_STUDIO_EVAL_ATTACH=1` makes `ensure_engine` attach-only (never stop or start).

### Standalone search package

`search.py` is a thin compatibility entrypoint and live re-export surface. `studio/search/` owns
`config`, `chunking`, `crawler`, `store`, `ranking`, `extractors`, `embedder`, `indexer`, `preview`, `api`,
`auth`, and `cli`. Heavy dependencies remain lazy. Launch remains `python search.py --port 8083`
with `cwd=ROOT`; ROOT still points to the directory containing search.py. The existing `studio/`
bundle copy includes this package. Search patch owners are `config.HOME`/`DOCUMENT_READER`,
`extractors.extract`, `crawler._dataless`, and `store.EXTRACT_VERSION`/`REDO_KINDS`; the same owner.attr
rule applies. `check_search_contract.py` verifies its independent HTTP routes and authentication.
`macos/release.sh --dry-run` performs both dependency audits and strict signature verification without publishing.

### Code vs. user data

`ROOT` (where `app.py` lives; `studio/config.py` computes it as the parent of `studio/`) holds code; `STATE` = `$LOCAL_STUDIO_HOME` holds user data
(`gallery/`, `uploads/`, `logs/`, `data/settings.json`, `data/chats/`, `data/search/`,
`data/agent/runs/`, `data/agent/memory.json`, `data/agent/backups/`). From a checkout `STATE`
defaults to the checkout itself (those folders are gitignored). The Mac app bundles the code into
`Contents/Resources/app` and sets `LOCAL_STUDIO_HOME` to `~/Library/Application Support/Local Studio`.
Always resolve user-data paths from `STATE`, never `ROOT`. Models live in the shared Hugging Face
cache (`HF_HUB_CACHE`, default `~/.cache/huggingface/hub`), not in the project.
`start.sh` exports `PYTHONDONTWRITEBYTECODE=1`: runtime imports must not create `__pycache__`
inside the signed app bundle, which would invalidate its resource seal after launch.

### One engine at a time

Chat goes through one of three external engine processes, and only one runs at once (each can
hold ~16 GB+). `ENGINES` in `studio/config.py` defines them (the rest is `studio/services/engines.py`):

| mode | process | port |
|---|---|---|
| `text` | `mlx-dspark serve` (MLX + DFlash/DSpark drafter) | 8080 |
| `vision` | `python -m mlx_vlm server` from the mlx-dspark uv tool env, same MLX weights | 8081 |
| `gguf` | `llama-server` (with `--mmproj` when the model has one) | 8082 |

`ensure_engine(mode, model)` (under `engine_lock`) stops any other engine, starts the requested one
and blocks until its readiness endpoint answers. The running engine is detected by `pgrep -f` on the
`pattern` regex, and `logs/engine.json` records `{mode, model_id}`. These process patterns are
duplicated in `stop.sh`; keep them in sync.

`POST /api/chat` picks the mode per request: GGUF models → `gguf`; MLX models → `vision` if any
message carries an image (including scanned-PDF pages), else `text`. If the right engine isn't
running it emits `{"x_engine": {...status: "switching"|"ready"}}` SSE events while switching. It
then proxies the OpenAI-style stream. `text` mode passes the raw mlx-dspark stream through, which
already includes the `x_mlx_dspark` stats event. For `vision`/`gguf` the backend parses the stream
and synthesizes an `x_mlx_dspark` stats event itself, because the UI only reads stats from that
event. It also clamps `max_tokens` to fit `config.CONTEXT` given the prompt size. `CONTEXT` is the context-window setting: 131072 by default, 100,000 to 262,144 (Settings → Context window, `context_window` in `data/settings.json`; an older or out-of-range value reads as the default). It is a ceiling, not a promise: `models.effective_context(m)` is `min(CONTEXT, native_context(m))`, where the native limit is the catalog/custom `context` or `max_position_embeddings` from the cached `config.json` (`text_config` first), and unknown (GGUF without a config) means no clamp. Engines are started with, budgets (`_chat_fit_context(body, window)`, the `max_tokens` clamp, reviews, sub-agents) use, and the UI meter shows the effective window. `engine.json` records it (`context_window`; absent means the old fixed `LEGACY_CONTEXT = 32768`), so `ensure_engine` and the chat route restart the engine on the next message after a change. The fitter counts replayed `reasoning_content` (each tool round's reasoning is capped to its tail by `_replay_reasoning`, and older rounds' reasoning is dropped before tool output is masked); `GET /api/context-window` adds `effective`, `native_max` and an advisory `memory_estimate_gb` note (never a limit).

Engines also expose OpenAI/Anthropic-compatible APIs directly (e.g. `127.0.0.1:8080/v1`) for
external clients. Everything binds to `127.0.0.1` only.

### Search service

`search.py` is a fourth process on port 8083, separate from the chat engines and exempt from
the one-at-a-time rule (it holds ~2 GB). `studio/services/search.py` is its only launcher (`_start_search`), always
with relative file names and `cwd=ROOT`, because the bundled path contains a space and
`stop.sh`/`_stop_search` find it by the literal `python search.py --port 8083`. Keep those
patterns in sync. Its dependencies live in `search-requirements.txt` and are installed by
`setup.sh --search` when the user turns Search on, never by `--install`. The index is
`data/search/index.db` under `STATE`. Module level in `search.py` imports only the standard
library and numpy so `macos/check_search.py` can run without a model. For development set
`LOCAL_STUDIO_SEARCH_ROOTS` to a sample folder so the real default folders are never crawled, and
`LOCAL_STUDIO_SEARCH_PORT` (for example 8093) so a development server gets its own search
service and can never reach or stop the one in the app the user is running.
Because these routes return the text of the user's files, `LocalOnly` in `studio/security/local_only.py` (now a guard on
every route and static file) rejects requests with a non-loopback `Host`, a foreign `Origin`, or
`Sec-Fetch-Site: cross-site|same-site`; the service itself refuses any request that carries an
`Origin` or is cross-site (`_local_request`).

### Voice service

`voice.py` (thin entrypoint) + `studio/voice/` is a fifth process on port 8084, behind the `voice.enabled` flag (off): the
echo-cancelled microphone and speaker (`bridge.py`: one AVAudioEngine with Apple voice processing, PyObjC), Parakeet TDT
0.6B v3 speech recognition (`stt.py`, parakeet-mlx) and Phonon 31M speech (`tts.py`, Gradium's `ptts`; PyPI `phonon` is
an unrelated package). It mirrors the search service: `studio/services/voice.py` is its only launcher (`_start_voice`,
relative names, `cwd=ROOT`), `stop.sh` and `_stop_voice` find it by `python voice.py --port 8084` (keep in sync; it is
also in stop.sh's pid list), its pinned deps are `voice-requirements.txt`, installed by `setup.sh --voice` when the user
turns Voice on (`data/voice-deps.ok`). The UI backend never imports `studio.voice`. Every route but `/health` needs the
token in `STATE/data/voice.token` (0600), and the service refuses any request with an `Origin` or `Sec-Fetch-Site`
(only the backend talks to it) or a non-loopback `Host`. The microphone stays closed until `/mic/open`; speech for the
conversation plays through the same engine (`/play`) so it is cancelled. Backend routes: `/api/voice/status|enable|
disable|voices|say|transcribe` (404 while the flag is off; disable always works). The app's Info.plist carries
`NSMicrophoneUsageDescription` and codesign adds `macos/LocalStudio.entitlements` (audio-input only). For development
set `LOCAL_STUDIO_VOICE_PORT` (for example 8094). Checks: `macos/check_voice.py` (offline, fake models and microphone).
Plan and measurements: [docs/voice-assistant-design.md](docs/voice-assistant-design.md).

### Chat Agent Harness

`POST /api/chat` runs a bounded native tool-calling loop for local models: `CHAT_TOOLS` (24 native tools plus dynamic custom MCP tools `mcp__<server_id>__<tool>`, schemas defined inline in `studio/services/harness.py`; the loop's route is `studio/api/chat.py`), up to `CHAT_TOOL_ROUNDS = 12` rounds per turn.

- **Tool groups.** `CHAT_READ_TOOLS` run without approval (clock, `web_search`/`read_webpage`,
  `search_files`/`read_file`, directory/find/grep, `code_outline`, `git_status`/`git_diff`,
  `delegate_task`, `manage_process`, `todo_write` → `x_plan`, `memory_store`/`memory_recall`
  in `STATE/data/agent/memory.json`). `CHAT_ACTION_TOOLS` (`write_file`, `edit_file`, `multi_edit_file`,
  `run_python`, `run_shell`, `ask_user`, plus custom MCP server tools) go through `harness_gate()` →
  safe/risky/blocked, executed by `_chat_action_tool()`. Plus `use_computer` (escalates to a CuaDriver
  run, `x_agent {run_id}`) and `reply` (for models that always call a tool). The system note is
  `AGENT_CHAT_NOTE`, applied in `_with_agent_note()` together with recalled memories and auto-loaded
  workspace instructions (`AGENTS.md`, `CLAUDE.md`, `.localstudio.md`, `.cursorrules`); it routes
  personal documents (passports, visas, I-94s, tax forms…) to `search_files`, never `web_search`.
- **`read_file`** reads an indexed document by `file_id` (DocumentReader, `max_pages` clamped 1–4) or
  any file by `path`; a `search_files` filename passed as `path` is resolved back to its `file_id`.
  Web reads go through `_public_web_target()`/`_fetch_public_web()` (public-IP pinning).
- **Edits & diagnostics.** `_match_edit_block()` matches exact → line-number prefixes stripped → trailing whitespace
  normalized. `_make_diff_preview()` produces the approval diff; `_post_edit_diagnostics()` runs automatic syntax/lint
  verification (`.py` via `ast.parse`, `.json` via `json.loads`, `.js` via `node --check`, `.swift` via `swiftc -parse`)
  and returns `syntax_check` + `syntax_ok` in the tool output; `_backup_file()` snapshots into
  `STATE/data/agent/backups/<id>.json` for per-step **Undo** (`POST /api/agent/undo`) and turn-level **Revert turn**
  (`POST /api/agent/undo-turn` via `x_checkpoint`).
- **Exec, safe read-only allowlist & background processes.** `run_python` (20 s, `python -I`) and `run_shell` (30 s, zsh)
  run in the workspace with a minimal hand-built env and `start_new_session=True`, streaming live stdout/stderr via
  `x_tool_output` SSE events. `_is_safe_readonly_shell()` pre-approves strict read-only inspection and test commands
  (`git status`, `git diff`, `git log`, `pytest`, `ls`, `node --check`, etc.) with zero confirmation clicks.
  `run_shell(background=True)` starts background processes managed via `manage_process` and `GET /api/processes`.
- **Fences.** `_harness_path_fence(path, write=...)` resolves symlinks and blocks credential paths
  (`~/.ssh`, `~/.aws`, `~/.gnupg`, Keychains, `.env*` except example/sample/template, `*.pem`/`*.key`/
  `*.p12`/`*.pfx`) and, for writes, system dirs and `STATE/data/agent`. `_gate_shell_command()` blocks
  sudo/su, `rm -rf /`/`~`, mkfs, `dd if=`, pipe-to-shell, launchctl, keychain dumps, osascript
  keystrokes and fork bombs. Tests: `harness_path_and_shell_safety_fences` in `check_agent.py`.
- **Approvals.** `_ChatSession` emits `x_approval`/`x_question` (carrying `session_id`) and awaits
  `POST /api/chat/sessions/{sid}/reply` (15-min timeout, keepalive comments). `allow_session` caches
  approval per `chat_id` for the `edit`, `exec`, or `mcp` category (`_chat_session_perms`).
- **Local-model resilience, sub-agents & compaction.** `_parse_harness_args()` repairs fenced/trailing-comma/Python-literal
  arguments; `_normalize_harness_args()` maps aliases (`filepath`→`path`, `old_str`→`old_text`,
  `cmd`→`command`, `run_subagent`→`delegate_task`…). `delegate_task` spins up a context-isolated read-only
  sub-agent (`researcher`, `verifier`, `simplifier`). `_chat_fit_context()` masks older rounds' tool outputs
  and preserves a `<conversation_checkpoint>` summary when evicting older turns; `POST /api/chat/compact`
  and `/compact` compact long conversations on demand. The workspace is a `contextvars.ContextVar`
  (`_chat_workspace`, `_in_workspace()`), never `os.chdir`.
- **UI & Composer (`frontend/chat/`).** Keeps each completed round in `bot.rounds` (`{thinking, thoughtMs, text, tools}`)
  and renders `.chround` blocks in order (thinking → `.chround-note` → `.chattoolsteps` of
  `<details class="chattoolstep">`, with Undo when a `backup_id` is present and "Revert turn (N)" when `bot.checkpoint`
  is present), then the final answer. A reply keeps running while the user opens other chats or views: `liveGen`
  (`{chat, bot, attach}`) registers it, `turnHTML` draws it as `liveSkeletonHTML`, and `renderThread` calls
  `liveGen.attach()` so the stream re-binds and repaints from `bot`; the live chat is reopened from memory, not the
  server, and chats are saved at send so they are in the sidebar at once. `ctxUsage()`/`updateCtx()` show context used
  and free (last reply's measured tokens plus a chars/3.2 estimate since) under the composer and in the Code status line. Composer supports `@file`/`@folder` fuzzy autocomplete (`GET /api/workspace/files`)
  and `/` slash commands (`GET /api/chat/commands`: `/review`, `/test`, `/commit`, `/simplify`, `/init`, `/compact`),
  plus custom MCP server management in Settings (`#mcpSet`, `GET/POST/DELETE /api/mcp/servers`).
- **Code view (`#v-code`).** A three-pane workbench: explorer (Files / Changes / Outline), a textarea editor
  with Code/Diff modes and a terminal, and the agent chat. There is one chat UI: `syncChatDock()` moves
  `#chatScroll`, `.welcome` and `#composerWrap` between `#v-chat` and `#codeChatDock` on `showView()`, so
  chat code must not assume those live in `#v-chat` (use `chatHost()`). The workspace is
  `current.workspace || codeWorkspace` (`activeCodeWs()`). Routes: `GET /api/workspace/info|file|outline`,
  `POST /api/workspace/file` (Save, goes through `write_file` with backup and diagnostics),
  `POST /api/workspace/settings`, `GET /api/processes`, `POST /api/processes/{id}/stop`
  (`POST /api/workspace/exec` still exists but the UI no longer uses it). Refreshes after agent tool events
  (`CODE_MUTATING`) never overwrite an editor with unsaved changes (`codeDirty()`).
- **Terminal (`studio/services/terminal.py`, routes in `studio/api/terminal.py`).** A login `/bin/zsh -l` on a pty per `_Terminal`, started through a
  tiny `python -I -c` wrapper (`_TERM_CTTY`) that clears the signal mask and takes the pty as controlling terminal
  (job control, and SIGHUP when the server dies). `_term_env()` drops the app's uv/Python/`LOCAL_STUDIO_*`
  variables and resets `PATH` so the user's login shell rebuilds it. Routes: `GET/POST /api/terminals`,
  `GET /api/terminals/{id}/stream?offset=` (SSE of base64 chunks with absolute offsets, so a reloaded page
  reattaches and replays the scrollback; an idle stream ends after 3 s so a stopping server never waits on an open
  page, and the page reconnects from its offset), `POST …/input`, `POST …/resize`, `DELETE …`. At most 8; all closed at
  exit (`atexit`). The UI draws it with xterm.js 5.5 + addon-fit loaded on first use from jsDelivr with SRI
  (`XTERM_FILES`); bump the hashes with the versions.
- **Permission prompts.** `permission_mode: "accept_edits"` in the `/api/chat` body grants the `edit` scope for that
  request only (`harness_gate(..., extra_perms)`) and **only for files inside the workspace**: a write outside it (`../x`, an absolute path,
  a symlink out) still asks, with the reason "outside the workspace, so Accept edits does not apply". Commands still ask, and an explicit
  "allow edits for this chat" from the user still applies everywhere. The UI never persists that mode: every load starts
  in "Ask before edits". A declined approval may carry `feedback`,
  which goes back to the model in the tool result. The UI shows numbered options (1/2/3 keys, Esc declines),
  Shift+Tab toggles the mode, and Esc stops a running reply.

### Computer-Use Agent (CuaDriver)

`studio/services/agent/` (`cua.py` daemon and permissions, `views.py` snapshots, `loop.py` runs) also lets a local model drive an isolated Chrome browser and Mac apps
through CuaDriver (`com.trycua.driver`, https://github.com/trycua/cua, >= 0.28.2), used as an MCP server over
stdio (`cua-driver mcp --socket ...`, hand-written JSON-RPC client, no dependency). It is off by
default (`data/settings.json` → `agent`) and independent of the chat engine rule except that a run
holds `agent_lock`, which `/api/chat`, `/api/image`, `/api/engine/*` and `/api/busy` respect.

- **Daemon.** Local Studio starts its own, never Hermes's or the default one:
  `open -n -g -a CuaDriver --args serve --socket <STATE>/data/agent/cua.sock --permission-mode standard`
  (LaunchServices, so CuaDriver's own Accessibility/Screen Recording grants apply; no manifest). The
  stop pattern `cua-driver serve --socket <re.escape(socket path)>` is duplicated in `stop.sh` and
  `agent_daemon_pattern()`; keep them in sync. It has no `^` anchor because the bundled path has
  spaces. The socket path must stay under 104 bytes. The daemon idles out after 10 minutes.
- **Fence.** `AGENT_CUA_TOOLS` is the MCP client's allowlist: `McpClient.call` refuses any other
  CuaDriver tool before sending. The gate (safe / risky / blocked) decides from facts about the target
  (role, DOM input type and autocomplete, label, URL, app tier), never from the model's claims; page and
  app text is untrusted. Per-app tiers (`APP_TIERS_DEFAULT`, `APP_TIERS_BUNDLE_DEFAULT`, user
  overrides in `agent.tiers`) are read-only / ask / allowed, the stricter match wins. Pages served by
  this Mac (loopback) are never opened or read.
- **Runs.** One MCP connection and one session label per run (the browser closes with the session).
  Snapshot order: `dom_refs_v1` first, `semantic_v2` last, and no new snapshot between observe and
  execute, or the refs go stale. A long page comes as continuation pages of one snapshot (scrolling
  does not work in the background). Runs and screenshots are saved in `STATE/data/agent/runs/`; a run
  left active by a dead process is marked `interrupted` at import. Approvals and `ask_user` are bound
  to the step number; Take over/Resume and Stop are flags the loop reads.
- **App input.** Pass the bound `pid` and `window_id` alongside `element_token` for clicks,
  typing and value changes. CuaDriver 0.28.2 rejects token-only clicks with a missing-pid error.
  A `same_pid_keyboard_ambiguity` refusal needs Take over/Resume; never redirect input to a
  sibling window or force foreground delivery automatically.
- **Browser profile.** CuaDriver's isolated profile: `~/Library/Application Support/CuaDriver/BrowserProfiles/local-studio`
  for the Mac app's `STATE`, `local-studio-<8 hex of the STATE path>` for any other, so a dev server never
  touches the app's sign-ins. Before each launch (no Chrome using the profile) the crash flag is repaired
  and downloads are set to prompt, into `STATE/data/agent/downloads`.
- **Routes.** `/api/agent/*` (status, enable, settings, permissions, telemetry/disable, run, runs/{id}/
  events|reply|stop|takeover|resume, runs, shots, profile/reset, memory, undo). `LocalOnly` guards them with every other
  route. Inline task cards and Settings → Computer access are in `frontend/chat/computer.mjs` and `frontend/settings/agent.mjs`; the old Agent navigation entry stays hidden.
- **Document reads.** My files requests search/read tools rather than rewriting the user's message
  with a fixed excerpt set. `/text/{file_id}` reads the actual indexed PDF/image through bundled
  `Contents/MacOS/DocumentReader` (`macos/document_reader.swift`), using PDFKit and on-device Vision
  OCR for scanned pages and filled forms. It supports `start_page`/`max_pages` (1-4), reports remaining
  pages and errors rather than treating missing text as a blank form. The source checkout uses
  `build/DocumentReader`. `macos/build.sh` compiles/signs it and runs `macos/check_document.py`
  against synthetic scanned, filled and multipage PDFs. No cloud OCR, model switch or full reindex.
- **Dev isolation.** Test on port 7861 with its own `LOCAL_STUDIO_HOME` (and a short path): it gets its own
  daemon, socket, stop pattern and browser profile. Never run `stop.sh`, start an engine or a daemon against
  the user's running Mac app.

### Models

A model entry (from `catalog.json` or a user-added Hugging Face repo stored in
`data/settings.json` under `custom`) has `backend: "mlx"` (with an optional `drafter` repo and
`mode` dflash/dspark) or `backend: "gguf"` (with `file` and an optional `mmproj`). Image entries
name the mflux CLI to run in `command`. `model_parts()` turns an entry into the repos/files it needs. Downloads
shell out to the `hf` CLI from the mlx-dspark tool env (`HF_CLI`) in background threads tracked
in the in-memory `downloads` dict. Install state is read from the HF cache layout
(`models--org--name/snapshots/*`, `*.incomplete`). The first model of a kind that finishes
downloading becomes the active one.

### Attachments, chats, images

- Attachments are processed once on upload (`/api/attachments`): images are downscaled to
  1536 px, documents are converted to text (pypdf / python-docx / openpyxl, capped at ~80k chars),
  and scanned PDFs become up to 4 page images. Chats store only attachment ids; `build_messages()`
  expands them into OpenAI content parts at send time.
- Chats are JSON files in `data/chats/<id>.json`, and the UI saves them with `PUT /api/chats/{id}`.
- Image generation runs the mflux CLI under `/usr/bin/time -l` (for peak memory), one job at a
  time (`image_lock`), always with `--low-ram`. Progress is parsed from the CLI's `n/total [` output
  and polled by the UI via `GET /api/image/{job_id}`. Output PNGs and a sidecar `.json` of the
  settings used go to `gallery/`. Editing mode only works with `EDIT_MODEL` set to a full checkpoint.

### Mac app and setup protocol

`macos/main.swift` runs `setup.sh --check`, offers `setup.sh --install` on a first-run screen,
runs `setup.sh --migrate` (copies data from `~/local-studio`), then `start.sh`, and loads
`http://127.0.0.1:7860` in a WKWebView. On quit it runs `stop.sh` unless "Keep Running After Quit"
is on. `setup.sh` talks to the app through a line protocol on stdout (`@check`, `@step`, `@log`,
`@done`, `@error`), so keep that format intact when editing it. `macos/build.sh` copies a fixed list
of files into the bundle (`app.py index.html catalog.json requirements-app.txt search.py search-requirements.txt setup.sh start.sh stop.sh uninstall.sh`)
plus `frontend/*.mjs` and `studio/` (rsync, never `__pycache__`; the build fails if the bundle contains bytecode).
Any new runtime file must be added to that list.

## Versioning and releases

The version lives in `APP_VERSION` in `app.py` (a bare literal on its own line; `studio/config.py` parses it from there). `build.sh`/`release.sh` read it from there for
the bundle and the DMG name, and the build number is the git commit count. The README also shows
the version (badge and DMG filename), so update those together. Commit messages follow the pattern
`Local Studio X.Y.Z: <summary>` with a bulleted body for releases.

## Verifying your work

Run all checks before reporting any task complete, and include the verification output:
- UI check: `node macos/check_ui.js index.html` (must exit 0)
- Syntax check: `python3 -c "import ast,sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]" app.py studio/**/*.py` (must exit 0)
- Search check: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py`
- App check: `uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_app.py`
- Agent Harness check: `TMPDIR=/tmp uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_agent.py`
- Document OCR check: `uv run -q --no-project --python 3.12 --with pypdf python macos/check_document.py build/DocumentReader`
- Voice service: `uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt --with numpy python macos/check_voice.py`
- Workflow stack: `macos/check_harness_resources.py`, `check_workflow_policy.py`, `check_workflow_runs.py`, `check_workflow_reviews.py`,
  `check_workflow_triggers.py`, `check_workflow_worktrees.py` and `check_evals.py`, each run as the App check above (`TMPDIR=/tmp`, pinned requirements)
- Full app build: `./macos/build.sh`

If a test fails, fix the implementation code — never skip, weaken, or delete a failing test.

## Things Claude gets wrong

- Never resolve mutable data paths (`gallery/`, `uploads/`, `logs/`, `data/`) from `ROOT`; always resolve from `STATE` (`$LOCAL_STUDIO_HOME`), because `ROOT` is read-only inside the signed `Local Studio.app` bundle.
- Never run `stop.sh` or start/stop an engine against the user's live `LOCAL_STUDIO_HOME` during tests; always isolate test runs with a temporary `LOCAL_STUDIO_HOME` and a short `TMPDIR=/tmp` (macOS limits `cua.sock` paths to 104 bytes).
- Never mutate process-wide `os.getcwd()` in the backend; always use the `_chat_workspace` `ContextVar` (`_in_workspace(path)`).
- When adding a Python dependency, add an exact pin to `requirements-app.txt` (read by `start.sh`, `APP_DEPS` in `setup.sh` and the checks). When adding a runtime file, add it to the copy list in `macos/build.sh`.

## Agent Harness roadmap

`docs/superpowers/plans/2026-10-17-ai-native-sdlc-agent-harness.md` is the spec and progress tracker for the
harness features (Tier 1–3, all marked done). The next-stage milestones (M1–M10: verification, durable runs, evals, skills,
hooks/test protection, Plan/Act/Auto, reviews, triggers, workers, nested instructions) are tracked in
[docs/agent-harness-roadmap.md](docs/agent-harness-roadmap.md) with their evidence and open limits. All are implemented behind
default-off flags. A first real baseline exists (`macos/evals/baselines/`, LFM2.5 24B-A2B 20/24); its thresholds are not agreed. Still open: unattended trigger execution (deliberately not built: it is a policy decision). Workers are not model-spawned. Read the roadmap
before claiming any of these delivered.
