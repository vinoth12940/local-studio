# Prompt v2: Local Studio — modularize safely, then harden security and performance

> Replaces `modularization-performance-security-prompt.md` (v1, written by a local model without current
> information). Facts about the code were checked against the repository on 2026-10-18, and facts about
> libraries and standards against their docs on the same date (see §11).

## What changed from v1, and why

| v1 said | Problem found | v2 does |
|---|---|---|
| Create an `app/` package | `uvicorn app:app` (start.sh), the `stop.sh` pkill patterns, and `import app` in every check suite all resolve `app` by name. A package dir named `app/` next to `app.py` takes over that import. | The package is `studio/`. `app.py` stays as a thin entrypoint, so launch and stop patterns don't change. |
| "All I/O awaited", convert to async | 80 of the 90 routes are sync `def`, which FastAPI already runs in a threadpool. The code relies on `threading.Lock` (`engine_lock`, `agent_lock`, `image_lock`, …). Converting handlers to `async def` would block the event loop on those locks. | Keep sync handlers sync. Audit only the 10 `async def` handlers and the SSE generators. |
| Split code freely | The check suites patch module globals (`app.SEARCH_URL = …`, `app._download = …`; 88 assignments in `check_agent.py` alone). Code moved to another module silently stops seeing those patches, so tests pass while no longer testing anything. | A patch-point inventory comes first (P0), with explicit rules for retargeting patches (§5 P1). |
| Split the UI into ES modules | `macos/check_ui.js` runs only the last inline `<script>`, so modules would bypass the build guard. Module scope also removes the globals that inline handlers and `innerHTML` templates depend on. | Extending `check_ui.js` to load the modules is a required step, and globals get an explicit bridge. |
| "Generate a per-launch auth token" | `LocalOnly` already blocks foreign Host/Origin, `Sec-Fetch-Site: cross-site|same-site` and DNS rebinding. Requests with no `Origin` header (start.sh's `curl`, the Swift app, any local process) still pass. v1 never said how scripts and the wrapper would get the token. | The token is an addition behind a flag, with a defined way to deliver it (§6.1). |
| `ulimit` memory caps, "no network for run_python" | Darwin does not reliably enforce `RLIMIT_AS`, and there is no `ulimit` way to block network access. | CPU, file-size and open-file limits are enforced; memory is capped best-effort. Network blocking uses a Seatbelt profile via `/usr/bin/sandbox-exec`, behind a flag (§6.3). |
| Budgets such as 300 KB, 800 ms TTI, 50 ms p95 | Invented, never measured. | Measure a baseline first, then set targets from it (§7). |
| Move everything to SQLite in P1 | This is the riskiest change, done before there is any evidence it's needed. | Storage is the last phase. It runs behind a flag with a rollback path, and only if measurements justify it (§5 P5). |
| Run all `check_*` suites at Mac app startup | Slow, they need uv-installed dependencies, and they are dev tooling. | The app waits for an `/api/health` readiness check instead. |
| Rewrite markdown sanitizing | `index.html` already has an allowlist sanitizer: http/https/mailto/# links only, own-origin images, active elements dropped, `on*` attributes stripped. | Keep it and add tests for it. CSP is extra defense in depth. |
| "Every error returns a stable code" | The UI reads `detail` from error responses. | Add a `code` field alongside `detail`; never replace `detail`. |
| Plain "pip-audit in release.sh" | Dependencies are unpinned (`--with fastapi` …). FastAPI shipped breaking changes this month (0.143.0 made OTel auto-configuration opt-in; 0.145.0 removed `openapi_prefix`; 0.146.0 deprecated `root_path`). | Pin versions in one shared requirements file, then `uv audit`. |

---

## 1. Role and goal

You are a senior engineer working on **Local Studio**, a private, on-device AI studio and local-model agent
harness for Apple Silicon Macs. Read `CLAUDE.md` and `AGENTS.md` first. They hold the full feature list and
the invariants, and they win over this prompt if the two disagree. If they disagree, stop and ask.

The goal has two parts, and they must stay separate:

1. **Refactor (no behavior change).** Turn `app.py` (~7,650 lines, 90 routes) and `index.html` (~4,200 lines,
   one inline script) into modules a person or agent can work on one at a time. Every route, response shape,
   SSE event name/field, UI selector and `data-*` attribute stays the same.
2. **Improvements (behavior changes, each behind a flag in `data/settings.json` that defaults to the old
   behavior).** Security hardening, performance fixes, storage changes, stream resume.

Why: the code is hard for agents to edit safely because one file mixes engines, the harness, the terminal,
CuaDriver and storage. The aim is smaller blast radius per change. It is not new architecture for its own sake.
Scale target: **one user, one Mac**. No multi-user, cloud or multi-host design.

## 2. Ground truth (verified in the repo, 2026-10-18)

- Backend: one FastAPI `app` in `app.py`, sections marked by `# ----` banners (helpers, models on disk,
  HF search, engines, LLM proxy, terminal, attachments, saved conversations, image jobs, search, agent,
  approval gate, versions & updates, storage & uninstall). 90 routes: 80 `def`, 10 `async def`.
  `/gallery` is a `StaticFiles` mount on `STATE/gallery`; `/` serves `ROOT/index.html`.
- `LocalOnly` (plain ASGI middleware) guards every route and static file.
- Shared state: module globals plus `threading.Lock`s (`agent_lock`, `engine_lock`, `image_lock`,
  `_memory_lock`, `_bg_lock`, `_terminals_lock`, `_custom_mcp_lock`, `_agent_cli_lock`, `_cua_grant_lock`), and
  dicts `_chat_sessions` and `_terminals`.
- Streams:
  - `/api/chat` is a **POST** read with `fetch()` + `res.body.getReader()`, so the browser's
    `EventSource`/`Last-Event-ID` resume does not apply to it.
  - Agent runs use `EventSource` with `?after=` resume.
  - The terminal uses absolute byte offsets.
- Atomic writes already use temp + `os.replace` for memory, edits and saves. Uploads are capped at 60 MB.
- Frontend: vanilla JS. `marked@12.0.2` comes from jsDelivr with SRI. xterm 5.5.0 + addon-fit 0.10.0 lazy-load
  with SRI (`XTERM_FILES`). 102 `innerHTML` sites. No CSP header.
- Custom MCP servers are **stdio only** (`McpClient`). The 2025 MCP SDK DNS-rebinding CVEs affect HTTP
  transports, not this code. Keep it stdio-only unless the user decides otherwise.
- Web search: DuckDuckGo HTML endpoint through `_fetch_public_web()` (public-IP pinning).
- Tooling on this Mac: `uv 0.11.2` (`uv audit` is available) and `/usr/bin/sandbox-exec` (deprecated by Apple,
  still working; the same Seatbelt mechanism Anthropic's sandbox-runtime uses).

Re-check any of these that your work depends on. The code may have moved since this was written.

## 3. Non-negotiable invariants

1. `ROOT` is read-only in the signed bundle. All mutable paths come from `STATE` (`$LOCAL_STUDIO_HOME`).
2. One active engine at a time. **Never** run `stop.sh` or start an engine or CuaDriver daemon against the user's live
   app. Test on port 7861 with a short `LOCAL_STUDIO_HOME` (`.claude/launch.json` → `agent-ui-dev`), a short
   `TMPDIR=/tmp`, and `LOCAL_STUDIO_SEARCH_PORT`/`LOCAL_STUDIO_SEARCH_ROOTS` set.
3. Never `os.chdir`. The workspace is the `_chat_workspace` ContextVar.
4. Never weaken, skip or delete a check. If a check fails, fix the code. (Retargeting a patch is allowed only under the
   P1 rule below.)
5. Theme colors come only from `:root` tokens (light, `data-theme="dark"` and the `prefers-color-scheme` copy stay in
   sync). `--serif` is for chat replies and the greeting only.
6. `PYTHONDONTWRITEBYTECODE=1`; no `__pycache__` in the bundle. A new package means more imports, so verify this.
7. Routes, SSE event names/fields, UI selectors and `data-*` stay compatible until the user agrees a versioned change.
8. The zero-click read-only shell allowlist (`_is_safe_readonly_shell`) stays exactly as it is.
9. Process patterns stay byte-identical across `app.py`, `stop.sh` and `setup.sh` (`uvicorn app:app … --port 7860`,
   `python search.py --port 8083`, the engine and CuaDriver patterns).
10. Every new runtime file or directory goes in the `macos/build.sh` copy list. Every new dependency goes in **both**
    `start.sh` and `APP_DEPS` in `setup.sh`, or in the shared requirements file both read (§6.6).
11. No frameworks, bundlers, TypeScript, npm or build step.

## 4. How to work (long-running, multi-session)

This job takes many sessions. Keep state outside your context window:

- **`docs/refactor-progress.json`** is the source of truth. It is a list of items like
  `{"id": "P1.engines", "phase": "P1", "description": "...", "verify": ["command or manual step", ...], "passes": false}`.
  Create it in P0 with every item from §5. After that you may only flip `passes` to true, and only after running
  that item's `verify` steps. Never delete or reword items; if one turns out wrong, add a note and ask.
- **Start every session the same way:** `pwd`, read `docs/refactor-progress.json` and `git log --oneline -15`, run the
  fast checks (UI check + `ast.parse` + `check_app.py`). Then pick the first item with `passes: false`. If anything
  is red before you start, fix that first.
- **One phase per branch. Small commits.** Each commit leaves every check green. The message follows
  `Local Studio X.Y.Z: <summary>` only for releases; otherwise use a plain imperative summary.
- **Stop and ask** before:
  - anything that changes a route/SSE contract
  - expanding the shell allowlist
  - turning a flag on by default
  - deleting the old JSON stores
  - any step where this prompt and `CLAUDE.md` disagree
- **After each phase report:** files and lines moved/added, every check's output, measured budget deltas, flags
  added, and any patch retargets (P1).

## 5. Phases (the next starts only when the current one is green: `./macos/build.sh` passes)

### P0 — Safety net (no product code changes)

- **Route contract snapshot.** A check (e.g. in `check_app.py`) dumps `[(methods, path, endpoint name)]` from
  `app.routes` to `macos/fixtures/routes.json` and fails if it changes without that file being updated.
- **SSE contract inventory.** List every `x_*` event key the backend emits (`x_engine`, `x_mlx_dspark`, `x_plan`,
  `x_approval`, `x_question`, `x_tool_output`, `x_checkpoint`, `x_agent`, `x_web_sources`, …) and assert that the UI
  still handles each one.
- **Patch-point inventory.** Grep `macos/check_*.py` for every `app.<name> = …`. Record each target and which
  section of `app.py` owns it. This decides how P1 moves code.
- **Characterization tests** for the riskiest seams, if not already covered: `harness_gate`, `_harness_path_fence`,
  `_gate_shell_command`, `_match_edit_block`, `_chat_fit_context`, backup/undo, `LocalOnly`.
- **Baseline measurements** in `docs/perf-baseline.md` (method in §7), before anything moves.
- **Pinned dependencies** (§6.6): pin the versions that are installed and working today; no upgrades yet.

### P1 — Backend split (no behavior change)

Layout. Arrows show the only allowed import direction; no cycles. Every module imports in a bare
`uv run --no-project --python 3.12 --with <pinned deps>`.

```
app.py                  # thin entrypoint: `from studio.main import app`, plus APP_VERSION (build.sh reads it with sed)
studio/
  config.py             # ROOT/STATE, ports, ENGINES table, logging setup        (imports: stdlib only)
  security/             # LocalOnly, path fence, shell gate, harness_gate, redaction   -> config
  storage/              # JSON stores (atomic), backups, memory file, later SQLite      -> config, security
  infra/                # subprocess/pty helpers, process table, SSE framing, ContextVar workspace -> config
  services/             # engines, LLM proxy, harness loop + tools, terminal, search client, images,
                        # CuaDriver agent, attachments, models/HF   -> storage, infra, security, config
  api/                  # one APIRouter per area; validation and response shaping only   -> services
  main.py               # FastAPI(), middleware, include_router, lifespan
```

Rules:
- **Move code; don't rewrite it.** Do one `# ----` section per commit, leaves first: helpers/config → models on disk/HF
  search → attachments → saved conversations → image jobs → search → engines → LLM proxy → terminal → harness →
  agent/approval gate → versions/storage. Keep function names, so a diff looks like a move.
- **Keep sync handlers sync.** Don't add `async` to a route that takes a `threading.Lock`. Keep each module's locks
  and global dicts in that one module.
- **Patch retargets.** When moved code owns a name the tests patch, change the test's target from `app.X` to the new
  owning module (`studio.services.search.X`). The assertion and its expected values must stay byte-identical. List
  every retarget in the phase report. If `app.py` must keep re-exporting a name for compatibility, it re-exports;
  it never becomes the place the code reads from.
- `macos/build.sh`:
  - copy `studio/` recursively
  - `ast.parse` every `.py` under it (not just `app.py`)
  - after the build, check the bundle has no `__pycache__`: `find "…/Contents/Resources/app" -name __pycache__` must
    print nothing.
- `/api/health` (new, additive): `{ok, version, engine: {mode, model_id}, search: up|down}`. It takes no locks.
- Logging is additive: keep `logs/ui.log` and the others. JSON-line logs, if added, go to their own file with request,
  session and engine ids plus durations.

### P2 — Frontend split (no behavior change)

- `index.html` becomes a shell with markup and CSS, plus `<script type="module" src="frontend/main.mjs">`. Split by
  concern: `core/` (api fetch, SSE parsing, stores), `chat/`, `code/` (editor, explorer, terminal), `settings/`, `ui/`.
  Serve `frontend/` read-only from `ROOT` and add it to the `build.sh` copy list.
- **Globals bridge.** Anything an inline handler or `innerHTML` template calls by name must be set on `window`
  explicitly in one place (`frontend/globals.mjs`). Better: replace inline handlers with delegated
  `addEventListener`. Never rely on implicit module globals.
- **Keep the build guard.** Extend `macos/check_ui.js` to load the module graph against the same stub DOM, using
  Node's ESM loader or a small loader that reads the files. It must still fail on start-up errors such as
  a temporal-dead-zone (TDZ) access. The stub `window` has no `addEventListener`, so either add a no-op stub to the
  check or keep using `window.on…` properties.
- Use one store per concern with pub/sub, and don't read state back from the DOM, but only as you move each piece.
  Don't redesign how chat rendering works in this phase.
- Verify in the `agent-ui-dev` server (port 7861) by rendering synthetic turns into `current` from the console.
  **Never send a real chat.** Check:
  - chat send/stop
  - a reply that keeps running while you switch chats (`liveGen`)
  - Code view Save/Diff/Undo
  - terminal open/reload/reattach
  - settings
  - dark mode
  - the 375 px width

### P3 — Security hardening (each item behind a flag unless purely additive)

See §6.

### P4 — Performance

Measure, fix the worst offenders, re-measure (see §7).

### P5 — Storage (only if P4 measurements justify it)

- If chat list or load is slow at realistic sizes (generate ~2k chats / 50k messages in a temp `STATE`), move
  chats, agent runs, memory and the backups index into `STATE/data/app.db`:
  - SQLite in WAL mode
  - `synchronous=NORMAL`
  - `busy_timeout` set on **every** connection
  - `BEGIN IMMEDIATE` for writes
  - one writer path per process
  - indexes so `EXPLAIN QUERY PLAN` shows no full scans on list endpoints
  - forward-only, idempotent, numbered migrations under `studio/storage/migrations/` with `meta.schema_version`,
    run in one transaction at startup.
- **Migration safety:**
  - Keep the JSON files until the user approves deleting them.
  - The flag `storage.sqlite` defaults to off.
  - Import is idempotent.
  - Turning the flag off reads JSON again (rollback).
  - Back up `data/` before the first import.
  - Use SQLite's backup API (never a raw file copy while a WAL is open) for any app.db backup.
- `settings.json` stays JSON behind the atomic-write store. Every destructive mutation writes its backup first.

### P6 — Search service and release

- Package `search.py` the same way, keeping the `python search.py --port 8083` launch pattern and `cwd=ROOT`.
- Incremental re-indexing on workspace file changes, debounced, so a single edit reindexes one file. Return
  `503` + `Retry-After` while a full index is being built.
- `release.sh`:
  - `uv audit` against the pinned requirements, including `search-requirements.txt`
  - `codesign --verify --deep --strict --verbose=2` on the app
  - `spctl -a -vv` when a Developer ID is set up.

## 6. Security specifics

Map `docs/security.md` (threat model plus what is enforced where) to the **OWASP Top 10 for Agentic Applications
2026** (ASI01–ASI10). Name the categories from the primary OWASP document, not from memory. The ones that matter
most here:
- goal and instruction hijack through file and web content
- tool misuse
- code execution
- memory poisoning
- supply chain (custom MCP servers, CDN scripts, Python dependencies)
- human-trust problems in approval prompts

### 6.1 Local API token (additive, flag `security.api_token`, off first)

Threat: any local process, or another macOS user account, can call `127.0.0.1:7860` without an `Origin` and pass
`LocalOnly`.

- At startup, write a random 32-byte token to `STATE/data/.api-token` with mode 0600.
- The page gets it as an `HttpOnly; SameSite=Strict` cookie:
  - the Swift wrapper sets it through `WKHTTPCookieStore` before loading
  - from a checkout, `start.sh` prints a one-time `http://127.0.0.1:7860/?t=…` URL that sets the cookie and
    redirects to `/`.
- Scripts (`start.sh`, `setup.sh`, the Swift health probe) read the file and send `Authorization: Bearer`.
- `/api/system` and `/api/health` stay open for readiness probes but return no user data. Everything else needs
  the token.
- Keep `LocalOnly` exactly as it is. Chrome's Local Network Access permission (Chrome 141/142) is extra protection
  but doesn't cover WKWebView, Safari or non-browser callers. Don't rely on it.

### 6.2 Fences

`studio/security/` is the only place path and command policy lives. Port `_harness_path_fence`, `_gate_shell_command`
and `harness_gate` verbatim, then add tests:
- `~/.ssh`, `~/.aws`, `~/.gnupg`, Keychains
- `.env*` (allow `.example`/`.sample`/`.template`)
- `*.pem`/`.key`/`.p12`/`.pfx`
- symlink escapes, `../`, null bytes, Unicode lookalikes and NFC/NFD forms, percent-encoding
- `sudo`/`su`, `rm -rf /|~`, `curl|sh` and `wget -O- | bash`, `dd if=`, `mkfs`, `launchctl`
- `security dump-keychain`, `osascript` keystrokes, fork bombs.

Don't change outcomes of existing cases.

### 6.3 Exec sandbox (flag `harness.sandbox`, off first)

Run `run_python`/`run_shell` children under `/usr/bin/sandbox-exec` with a generated Seatbelt profile:
- writes allowed only to the workspace and a temp dir
- reads denied to the §6.2 credential paths
- network denied for `run_python` unless the user opts in per chat.

Also:
- Set `RLIMIT_CPU`, `RLIMIT_FSIZE` and `RLIMIT_NOFILE` in `preexec_fn`.
- Treat memory as best-effort: Darwin doesn't reliably enforce `RLIMIT_AS`, so watch RSS and kill above a cap.
- Keep the existing timeouts, `start_new_session=True` and the minimal environment.
- Background processes started with `run_shell(background=True)` get the same sandbox.
- If `sandbox-exec` is missing or fails, refuse with a clear message. Never fall back silently.

### 6.4 Untrusted content and prompt injection

- File, web, OCR, MCP tool output and **custom MCP tool descriptions** stay framed as untrusted data in what the
  model sees.
- Show the full command line and environment of a custom MCP server in its approval UI before its first run.
- Model output is never executed except through approved tools.
- Keep the markdown allowlist sanitizer and add DOM-free tests for it:
  - `javascript:`/`data:` and entity-encoded schemes
  - `on*` attributes
  - `<svg>`/`<math>`/`<template>`/`<noscript>`
  - `srcset`, `formaction`
  - images from foreign origins.

### 6.5 Headers, uploads, secrets

- **CSP, flag first.**
  - Start as `Content-Security-Policy-Report-Only`. Only after it reports nothing in normal use, switch to
    enforcing.
  - Policy: `default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'
    https://cdn.jsdelivr.net; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'`.
  - If inline scripts remain, use a nonce rather than `'unsafe-inline'`.
- Add `X-Content-Type-Options: nosniff` on all responses. Add `Content-Disposition` plus cache headers on gallery and
  upload media.
- Uploads:
  - random names
  - sniff content type from magic bytes, not the extension
  - keep the 60 MB cap
  - never serve from a path the model chose without the fence.
- Redact secrets (`sk-…`, `sk-ant-…`, `AKIA…`, `ghp_…`/`github_pat_…`, `xox[bp]-…`, PEM private-key blocks) in logs
  and SSE tool output.

### 6.6 Dependencies

- Create one `requirements-app.txt` with exact pins, used by both `start.sh` (`--with-requirements`) and `APP_DEPS`
  in `setup.sh`, and add it to the `build.sh` copy list. Pin `search-requirements.txt` too.
- Pin CDN assets by exact version + SRI, as now. Bumping marked (12.0.2) or xterm (5.5.0 → 6.x, which has breaking
  scrollbar options) is a separate, tested change, never part of a refactor commit.
- FastAPI ≥ 0.135 has native SSE (`fastapi.sse.EventSourceResponse`, 15 s keepalive pings, `Last-Event-ID`). You may
  use it for **new** endpoints. Don't rewrite existing streams, whose exact framing the UI parses.
- Any new dependency needs a one-line justification in the commit or PR.

## 7. Performance method

1. Write a repeatable benchmark (`macos/bench_app.py`, offline, temp `STATE`, engines stubbed with a fake streaming
   server) and record a baseline in `docs/perf-baseline.md` with the Mac model and date:
   - p50/p95 API overhead per hot route (chat list, chat load, workspace files, settings)
   - first-token overhead added by the proxy
   - event-loop lag during 10 concurrent synthetic streams
   - threadpool saturation: anyio's default limit is 40 threads, and long sync handlers such as engine switches or
     OCR can starve it
   - UI: transferred bytes and time to interactive in the dev server.
2. Starting targets, to adjust after the baseline:
   - API overhead p95 < 50 ms
   - search candidate lookup p95 < 300 ms
   - 4-page OCR < 5 s
   - cold start with engines off < 10 s
   - no event-loop stall > 250 ms.
3. Likely fixes, only where the measurements point:
   - move blocking work out of the 10 `async def` handlers and SSE generators (`asyncio.to_thread` under a bounded
     semaphore)
   - give long-running sync work (OCR, DOCX/XLSX parsing, large diffs) its own bounded executor so it can't starve
     the shared pool
   - batch streaming DOM writes through a `requestAnimationFrame` coalescer
   - virtualize very long threads.
4. **Stream resume** (feature, flag `streams.resume`): `/api/chat` is POST + `fetch`, so resume needs a server-side
   ring buffer per stream id with monotonically increasing event ids, plus a `GET /api/chat/streams/{id}?after=N`
   reattach route (additive). Approval/question pauses must survive the reattach.

## 8. Definition of done

- `app.py` is an entrypoint of a few lines. `index.html` is markup and CSS plus a module loader. Every `check_*`
  suite, the extended `check_ui.js`, the route and SSE contract snapshots, and `./macos/build.sh` pass, with output
  included.
- Spot checks in the dev server, all passing (each is an item in `docs/refactor-progress.json`):
  - 12-round agent turn (synthetic)
  - Undo and Revert turn
  - `/compact`
  - OCR `read_file`
  - terminal reattach after reload
  - model-switch UI
  - Code view Save with diagnostics
  - approval prompts (1/2/3, Esc, Shift+Tab)
- `docs/perf-baseline.md` (before and after), `docs/security.md` (threat model mapped to OWASP ASI01–10, plus where
  each control lives), and an updated `CLAUDE.md`/`AGENTS.md` for the new layout.
- Every flag added is listed with its default and how to turn it on.

## 9. Do not

- Rename or remove public routes, SSE events, selectors or `data-*` attributes.
- Simplify fences, gates, backups or the sanitizer. Port them verbatim, then extend.
- Convert sync handlers to async, or swap `threading.Lock` for `asyncio.Lock`, as part of the refactor.
- Add multi-user, cloud or network-exposed components, or bind anything to a non-loopback address.
- Commit anything that fails a check, or mark a progress item as passing without running its `verify` steps.
- Test against the user's running app or real data folders.

## 10. First message back to the user

Before writing code, reply with:
- the P0 inventory results (route count, SSE events, patch points per section)
- the proposed `docs/refactor-progress.json` item list
- any conflict you found between this prompt, `CLAUDE.md` and the code.

Then wait for approval.

## 11. Sources (checked 2026-10-18)

- FastAPI release notes, versions 0.135–0.146 (SSE added in 0.135.0; 0.143.0 OTel opt-in; 0.145.0 removes
  `openapi_prefix`; 0.146.0 deprecates `root_path`): https://fastapi.tiangolo.com/release-notes/
- FastAPI Server-Sent Events (`fastapi.sse`, keepalive, `Last-Event-ID`, POST):
  https://fastapi.tiangolo.com/tutorial/server-sent-events/
- OWASP Top 10 for Agentic Applications 2026: https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/
- MCP SDK DNS-rebinding advisories (HTTP transports only; stdio unaffected), CVE-2025-66414 / CVE-2025-66416:
  https://advisories.gitlab.com/pkg/pypi/mcp/CVE-2025-66416/
- Anthropic, "Effective harnesses for long-running agents" (JSON feature list, progress file, session start routine):
  https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Anthropic sandbox-runtime (Seatbelt on macOS): https://github.com/anthropic-experimental/sandbox-runtime
- Chrome Local Network Access: https://developer.chrome.com/blog/local-network-access (exact stable milestone,
  141 vs 142, unconfirmed)
- uv script locking and `uv audit`: https://pydevtools.com/handbook/how-to/how-to-scan-python-dependencies-for-vulnerabilities/
  (third-party; `uv audit` confirmed present in local uv 0.11.2)
- xterm.js 6.0.0 release notes (breaking scrollbar options, ESM): https://newreleases.io/project/npm/@xterm/xterm/release/6.0.0
- SQLite WAL practice (WAL + `synchronous=NORMAL` + per-connection `busy_timeout` + `BEGIN IMMEDIATE`): practitioner
  sources only; check against https://sqlite.org/wal.html before relying on it.
