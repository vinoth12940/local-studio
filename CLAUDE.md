# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Local Studio is a private, on-device AI studio and local-model Agent Harness for Apple Silicon Macs:
chat (MLX models with DFlash/DSpark speculative decoding, or GGUF on llama.cpp), multi-step workspace
coding/research agent harness, browser/macOS computer use, document/image attachments, on-device
semantic file search, and text-to-image via mflux. It ships as a self-contained macOS app
(`Local Studio.app`) and also runs from a source checkout. There are only a handful of source files:

- `app.py`: the entire FastAPI backend (one file, ~6,050 lines, organized by `# ----` section banners)
- `index.html`: the entire single-page UI (~3,045 lines; inline CSS + one inline `<script>`; vanilla JS, `marked` from a CDN)
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
declaration fail the build), `ast.parse`s `app.py` and `search.py`, and runs the offline verification
suites (`check_search.py`, `check_app.py`, `check_agent.py`, `check_document.py`). Individual checks:

```sh
node macos/check_ui.js index.html
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" app.py
uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py
uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py
uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_agent.py [check_name ...]
uv run -q --no-project --python 3.12 --with pypdf python macos/check_document.py build/DocumentReader
uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --eval build/search_samples
```

`macos/check_agent.py` (~5,350 lines) verifies both the CuaDriver computer-use agent (gate table, view
trimmer, history folding, MCP client with a fake `cua-driver`, scripted runs) and the Chat Agent
Harness (`harness_argument_repair_and_normalization`, `harness_path_and_shell_safety_fences`,
`harness_tolerant_edit_matching`, `harness_backup_and_undo`, `harness_session_approval_and_reply`,
`harness_memory_and_todo`, plus multi-round tool loops, personal document searches, and OCR reads).
When running `check_agent.py` directly, pass a short `TMPDIR` (e.g. `TMPDIR=/tmp`) because the Unix
domain socket path has a 104-byte limit on macOS. The search eval needs the model and a sample folder
built by `macos/make_search_samples.py build/search_samples`.

The backend has no project/venv: `start.sh` runs it with
`uv run --no-project --python 3.12 --with fastapi --with uvicorn ... uvicorn app:app`. If you add a
Python dependency, add it to **both** `start.sh` and `APP_DEPS` in `setup.sh`. After editing
`app.py`, restart the UI server (`./stop.sh && ./start.sh --ui`); there is no auto-reload. Logs and
pid files go to `logs/` (`ui.log`, `llm.log`, `vlm.log`, `gguf.log`, `download-<id>.log`).

## Architecture

### Code vs. user data

`ROOT` (where `app.py` lives) holds code; `STATE` = `$LOCAL_STUDIO_HOME` holds user data
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
hold ~16 GB+). `ENGINES` in `app.py` defines them:

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
event. It also clamps `max_tokens` to fit the fixed 32k `CONTEXT` given the prompt size.

Engines also expose OpenAI/Anthropic-compatible APIs directly (e.g. `127.0.0.1:8080/v1`) for
external clients. Everything binds to `127.0.0.1` only.

### Search service

`search.py` is a fourth process on port 8083, separate from the chat engines and exempt from
the one-at-a-time rule (it holds ~2 GB). `app.py` is its only launcher (`_start_search`), always
with relative file names and `cwd=ROOT`, because the bundled path contains a space and
`stop.sh`/`_stop_search` find it by the literal `python search.py --port 8083`. Keep those
patterns in sync. Its dependencies live in `search-requirements.txt` and are installed by
`setup.sh --search` when the user turns Search on, never by `--install`. The index is
`data/search/index.db` under `STATE`. Module level in `search.py` imports only the standard
library and numpy so `macos/check_search.py` can run without a model. For development set
`LOCAL_STUDIO_SEARCH_ROOTS` to a sample folder so the real default folders are never crawled, and
`LOCAL_STUDIO_SEARCH_PORT` (for example 8093) so a development server gets its own search
service and can never reach or stop the one in the app the user is running.
Because these routes return the text of the user's files, `LocalOnly` in `app.py` (now a guard on
every route and static file) rejects requests with a non-loopback `Host`, a foreign `Origin`, or
`Sec-Fetch-Site: cross-site|same-site`; the service itself refuses any request that carries an
`Origin` or is cross-site (`_local_request`).

### Chat Agent Harness

`POST /api/chat` runs a bounded multi-round native tool-calling harness (`CHAT_TOOLS`, up to
`CHAT_TOOL_ROUNDS = 12` rounds per turn) designed specifically for local models:

- **18 Native Tools (`CHAT_TOOLS`)**:
  - **Read-only (`CHAT_READ_TOOLS`, execute without approval)**:
    - `get_current_datetime(timezone?)`: fresh system clock in the user's IANA timezone.
    - `web_search(query)` & `read_webpage(url)`: organic DuckDuckGo HTML search and primary-source
      verification with public DNS pinning (`_safe_public_url`).
    - `search_files(query, sort="relevance"|"newest")`: queries the local search service (`search.py`),
      comparing labeled document dates rather than file copy times or I-94 expiry dates. Personal
      documents (passports, visas, I-94s, EADs, tax forms, receipts, notes) are explicitly routed to
      `search_files` in `AGENT_CHAT_NOTE` rather than `web_search`.
    - `read_file(file_id?, path?, start_page?, max_pages?, start_line?, max_lines?)`: dual-mode reader.
      Reads indexed documents by `file_id` (via `DocumentReader` with PDFKit + Vision OCR, clamping
      `start_page >= 1` and `max_pages` to `1..4`) or reads any workspace/local file by `path` (text/code
      with 1-indexed line numbers, plus PDF/DOCX/XLSX). If a model passes a candidate filename from
      `search_files` as `path` instead of `file_id`, `read_file` automatically resolves it to the matching
      indexed `file_id` before falling back to disk.
    - `list_directory(path?, show_hidden?)`, `find_files(pattern, path?)`, `grep_files(query, path?, glob?)`:
      workspace navigation, recursive glob/substring search (skipping `.git`, `node_modules`, `.venv`,
      `build`, etc.), and regex/substring content search.
    - `todo_write(todos)`: updates the live task plan checklist (`pending`, `in_progress`, `completed`)
      and emits `x_plan {todos}`.
    - `memory_store(key, value)` & `memory_recall(query?)`: persistent cross-chat key-value memory stored
      in `STATE/data/agent/memory.json` and automatically injected into `_with_agent_note()`.
  - **Gated workspace actions & interaction (`CHAT_ACTION_TOOLS`)**:
    - `write_file(path, content)` & `edit_file(path, old_text, new_text, replace_all?)`: atomic file
      creation/editing with unified diff preview (`_make_diff_preview`). `_match_edit_block()` implements
      3-tier tolerant matching (exact match $\rightarrow$ stripped `123: ` line-number prefixes $\rightarrow$
      trailing-whitespace-normalized line match) so small local models don't fail on minor formatting drift.
      Before modifying any file, `_save_file_backup()` snapshots the original into
      `STATE/data/agent/backups/<backup_id>.json` and returns `backup_id` so the UI can offer 1-click
      **Undo** (`POST /api/agent/undo`).
    - `run_python(code)` (20 s timeout) & `run_shell(command, cwd?)` (30 s timeout): executes Python 3 or
      `/bin/zsh` in the active workspace using `os.setsid` process groups for clean timeout termination
      and a sanitized environment (`_safe_exec_env()`).
    - `ask_user(question, options?)`: pauses the SSE stream and emits `x_question` so the user can click
      an option chip or type a custom clarification.
  - **Escalation & direct reply**:
    - `use_computer()`: starts a CuaDriver browser/macOS GUI run (`x_agent {run_id}`).
    - `reply(text)`: explicit Markdown reply tool for models that prefer calling a tool on every turn.
- **Local-Model Resiliency & Context Management**:
  - `_parse_harness_args()` repairs markdown code fences, trailing commas, and Python literal dicts in
    tool arguments; `_normalize_harness_args()` maps common parameter aliases (`file`/`filepath` $\rightarrow$
    `path`, `old_str`/`search` $\rightarrow$ `old_text`, `cmd` $\rightarrow$ `command`, `task`/`title` $\rightarrow$ `content`).
  - Duplicate tool-call loop detection (`call_counts`) attaches a `loop_warning` when a model repeats
    the same `(tool_name, canonical_args)` call in a turn.
  - Observation masking in `_chat_fit_context()` compacts older tool outputs from earlier rounds while
    keeping recent tool outputs intact so multi-round agent turns fit within the 32k context window.
  - Per-request workspace isolation uses `contextvars.ContextVar` (`_chat_workspace` / `_in_workspace`)
    so concurrent requests never mutate process-wide `os.getcwd()`.
- **Safety Fences & Interactive Session Approvals (`_ChatSession`)**:
  - `_harness_path_fence(path, write=...)` resolves symlinks and blocks `~/.ssh`, `~/.aws`, `~/.gnupg`,
    `~/Library/Keychains`, `.env` files (except `.env.example`/`.sample`/`.template`), `*.pem`, `*.key`,
    `*.p12`, `*.pfx`, `*.keychain*`, and (for writes) `/System`, `/Library`, `/usr`, `/bin`, `/sbin`,
    `/etc`, `/var`, `/Applications`, and `STATE/data/agent`.
  - `_gate_shell_command(cmd)` blocks `sudo`, `su `, `rm -rf /` or `~`, `mkfs`, `dd if=`, `curl|sh`,
    `wget|sh`, `launchctl`, `security find-/dump-/export`, `osascript` keystroke injection, and fork bombs.
  - When a tool requires approval (`edit` category for `write_file`/`edit_file`, `exec` category for
    `run_python`/`run_shell`) or asks a question (`ask_user`), `_ChatSession` emits `x_session {session_id}`
    and `x_approval` / `x_question`, then awaits an `asyncio.Event` (with 15-minute timeout and 15 s
    keepalive comments) until the UI posts to `POST /api/chat/sessions/{sid}/reply`. Selecting
    **Allow in this chat** (`allow_session`) caches approval for that category (`edit` or `exec`) scoped
    to that `chat_id`.
- **Step-by-Step Per-Round Thinking & UI (`index.html`)**:
  - When a turn involves tool calls across multiple rounds, `index.html` stores each completed tool round
    in `bot.rounds` (`{thinking, thoughtMs, text, tools}`) and renders `.chround` blocks in chronological
    order: Round 1's collapsible `Thought for Xs · N words` $\rightarrow$ Round 1's intermediate note
    (`.chround-note`) $\rightarrow$ Round 1's expandable tool steps (`.chattoolsteps`), followed by Round 2,
    and finally the concluding round's thinking and final answer.
  - Each tool step renders as an expandable `<details class="chattoolstep">` showing unified diffs,
    stdout/stderr, or file excerpts, plus a 1-click **Undo** button when `backup_id` is present.
  - Additional routes: `POST /api/workspace/pick` (native macOS folder picker via `osascript`),
    `POST /api/agent/undo`, and `GET/POST/DELETE /api/agent/memory`.

### Computer-Use Agent (CuaDriver)

The `# ---- agent` section of `app.py` also lets a local model drive an isolated Chrome browser and Mac apps
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
  route. Inline task cards and Settings → Computer access are in `index.html`; the old Agent navigation entry stays hidden.
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
of files into the bundle (`app.py index.html catalog.json search.py search-requirements.txt setup.sh start.sh stop.sh uninstall.sh`); the agent added none.
Any new runtime file must be added to that list.

## Versioning and releases

The version lives in `APP_VERSION` in `app.py`. `build.sh`/`release.sh` read it from there for
the bundle and the DMG name, and the build number is the git commit count. The README also shows
the version (badge and DMG filename), so update those together. Commit messages follow the pattern
`Local Studio X.Y.Z: <summary>` with a bulleted body for releases.
