# Local Studio security controls

Reviewed on 2026-10-18 against the P3 implementation and the
[OWASP Top 10 for Agentic Applications 2026 primary PDF](https://genai.owasp.org/download/52117/),
contents and category headings on printed pages 9–38. Category names below follow that document.
This is a control inventory and threat model, not a certification or a claim that prompt injection is solved.

## Scope and trust boundaries

Local Studio serves one user on one Mac through loopback HTTP and a WKWebView or browser.
Assets are workspace files, personal documents and OCR, saved conversations, model files,
agent memory, backups, local credentials, and the user's authenticated browser/app sessions.
Application code is under `ROOT`; mutable data and logs are under `STATE` (`LOCAL_STUDIO_HOME`).
The signed app bundle is never a user-data store.

Untrusted inputs include model-generated arguments, workspace documents, uploaded files, OCR,
web pages, MCP tool descriptions/schemas/results, and delegated-agent output. Workspace instruction
files and saved memory also influence the model: installing or editing them is a trust decision.
A malicious page, file or MCP server can try to redirect the model into reading secrets, changing
files, running code, or operating a signed-in app. A separate local process can call loopback APIs
without browser Origin headers. These are distinct trust boundaries; local inference alone does
not make tool use safe.

The browser reaches FastAPI through `SecurityHeaders` → `LocalOnly` → optional `ApiToken` → optional request logging.
The harness chooses tools, validates paths/commands, and waits for approval before gated actions.
Approved child processes run as the current macOS user. Custom MCP servers use stdio and inherit
Local Studio's environment plus their configured overrides. CuaDriver has its own allowlist,
app tiers, target-derived gate, and macOS accessibility permissions.

## OWASP mapping

| Category | Relevant attack | Controls and owners | Remaining exposure |
|---|---|---|---|
| ASI01: Agent Goal Hijack | A page, document, OCR result or tool descriptor redirects the task. | `services/harness.py::_with_agent_note` treats source material and MCP descriptions/results as untrusted. `services/mcp_custom.py` labels descriptions and JSON-escapes framed results; `api/chat.py` uses this framing in tool messages. `security/harness_gate.py` validates action arguments independently of model claims. | Framing is guidance to the model, not isolation. A user may still approve an injected action. |
| ASI02: Tool Misuse and Exploitation | The agent chains legitimate tools to disclose data or alter the workspace. | `security/fences.py` resolves symlinks and rejects credential paths and blocked commands. `security/harness_gate.py` supplies file diffs and execution previews. `_ChatSession` in `services/harness.py` binds replies to pending approval keys. | Session grants cover an action category. The unchanged read-only shell allowlist includes test runners, whose project code may execute. |
| ASI03: Identity and Privilege Abuse | A local caller or child process uses the user's authority. | `security/local_only.py` checks Host, Origin and Fetch Metadata on all routes/static files. CuaDriver app tiers and `security/agent_gate.py` use target facts. `run_python` has a small environment; shell tools exclude several known credential prefixes. | LocalOnly permits local scripts without Origin. Opt-in `security.api_token` requires a private bearer token or HttpOnly startup cookie. Environment filtering is not a complete secret boundary. |
| ASI04: Agentic Supply Chain Vulnerabilities | A custom MCP executable, dependency or CDN script is compromised. | Exact Python pins in `requirements-app.txt` and `search-requirements.txt`; the UI's third-party files (fonts, marked, xterm.js) bundled in `frontend/vendor/` and verified against `VENDOR.json` hashes, which are also their integrity attributes. `security.mcp_approval` binds launch consent to a hash of command, arguments and configured environment. Settings displays that full configuration before Test launches a server. Tool approvals also include it. | The flag defaults off for compatibility. When off, enabled servers can launch automatically for discovery. Consent does not audit server code or its inherited environment. Dependency auditing is still a P6 item. |
| ASI05: Unexpected Code Execution (RCE) | Model output reaches shell/Python, or uploaded/rendered content executes. | Shell/path gates, explicit exec approval, timeouts, process groups and post-edit syntax diagnostics. `frontend/core/markdown.mjs` uses an HTML allowlist. `security/headers.py` adds nosniff; `security.uploads` checks file signatures when enabled. | Sandbox/resource flags default off. Approved code otherwise runs with user privileges. Signature checks establish a format, not that its parser/content is safe. |
| ASI06: Memory & Context Poisoning | An injected fact persists into later turns or compaction. | `storage/memory.py` uses bounded atomic storage; the leading harness note gives current user instructions priority. `services/harness.py` bounds history and preserves checkpoint summaries. | Memory and workspace instructions influence later turns. There is no provenance-based authorization boundary for remembered text. Review/delete poisoned memory through Settings. |
| ASI07: Insecure Inter-Agent Communication | A delegated agent or external MCP result impersonates a trusted instruction. | `delegate_task` has isolated context and read-only tools in `services/harness.py`; custom MCP output is explicitly untrusted. `infra/mcp_client.py` restricts CuaDriver tools and uses local stdio. | Read-only delegation can still return misleading facts; stdio does not make a server trustworthy. No distributed agent identity protocol is implemented or needed for the current design. |
| ASI08: Cascading Failures | Repeated tools, hung children or interrupted writes amplify a fault. | Twelve-round chat limit, repeat-call warnings, computer-agent step/time limits, subprocess timeouts and process-group termination. `storage/atomic.py` writes a private temporary file, flushes/fsyncs it, then replaces the destination; used for settings, chat PUT/compaction, upload metadata and engine state. Backups support undo. | Resource caps are opt-in; RSS sampling is best effort. Atomic replacement does not serialize competing read-modify-write operations or guarantee directory durability after power loss. |
| ASI09: Human-Agent Trust Exploitation | A misleading approval persuades the user to authorize a harmful action. | `security/harness_gate.py` derives previews from actual arguments and paths. Settings' MCP launch dialog uses textContent, quotes command arguments, and shows all configured environment overrides. `frontend/chat` renders approvals and tool results through escaping/sanitization. | Approval wording is not proof of safety. “Trusted” MCP and session permission settings intentionally reduce prompts. |
| ASI10: Rogue Agents | An autonomous loop continues unwanted actions or hides failures. | Stop/takeover/resume controls in `services/agent/loop.py`, per-run history, tool allowlists, action gates, limits, file backups and turn rollback. Running chat UI state reattaches through `liveGen`. | Undo restores workspace edits with saved backups; it cannot reverse external app actions, sent messages or arbitrary shell side effects. |

## P3 media, headers and redaction

`SecurityHeaders` is outside LocalOnly so rejection responses also carry
`X-Content-Type-Options: nosniff`. Gallery responses get private cache headers; raster images display inline and other formats download as attachments;
upload JPEG responses get a generated filename and private caching. Upload reads are bounded to
60 MB plus one byte before rejecting with 413. Stored upload names are random. Media requests
must match generated names and pass the resolved-path fence and upload-directory containment check.

`security.upload_sniff` rejects mismatched image/PDF/Office signatures with 415 before parsing;
Office files still go through their normal ZIP-based parsers. The old extension-based behavior
remains the default. Parser isolation and archive expansion limits are not provided here.

`security.redact` filters supported API/token patterns and PEM private-key blocks from tool live
output, completed tool results/errors, Python root/uvicorn logging, structured request logs,
CSP reports and application-managed subprocess log descriptors. Filtering happens before writing
those child-process logs. The stream filter buffers incomplete lines so secrets and PEM delimiters
split across chunks cannot bypass it. Overlong lines are withheld conservatively. Log pumps handle
UTF-8 across byte boundaries. Redaction does not erase old files, alter backups/chats, filter terminal
PTY sessions, inspect files a child writes directly, or guarantee detection of an unknown secret format.
MCP approval previews intentionally show the actual configured environment for the local user.
Enable redaction before starting processes whose logs need filtering; already running processes retain
the log descriptor they started with.

## Flags and operation

Merge the desired keys into `STATE/data/settings.json`, preserving the other settings. Values are
re-read when the file changes. All flags below default to the previous behavior:

| Flag | Default | Opt-in value and effect |
|---|---|---|
| `security.api_token` | `false` | `true`: require local bearer/cookie authentication; private token files and native/startup delivery described below. |
| `harness.sandbox` | `false` | `true`: Seatbelt for harness Python/shell/background commands. |
| `harness.rlimits` | `false` | `true`: kernel CPU/file/fd caps and RSS watchdog, with optional lower `harness.limits`. |
| `performance.isolated_io` | `false` | `true`: isolate slow search rendering from quick API routes in a bounded executor. |
| `performance.chat_index` | `false` | `true`: cache chat metadata while validating authoritative JSON signatures on every list request. |
| `search.incremental` | `false` | `true`: poll configured search roots and debounce changed-file reindexing; full passes return 503/Retry-After. |
| `streams.resume` | `false` | `true`: retain bounded process-local chat SSE replay buffers and enable reconnect/reload resume. |
| `voice.enabled` | `false` | `true`: allow the voice service (`voice.py`, port 8084) to be installed and started from `/api/voice/enable`. It needs the token in `data/voice.token` (0600), refuses any request with an `Origin`/`Sec-Fetch-Site` or a non-loopback `Host`, keeps the microphone closed until `/mic/open`, and never writes audio to disk. |
| `harness.coding_workflow` | `false` | `true`: default `search_files` to the active workspace and guide the model through diagnosing, repairing and rerunning failed coding checks. Explicit `scope=indexed` retains personal-document lookup. Existing approval gates still apply. |
| `logging.json_requests` | `false` | `true`: slow, failing and non-GET request metadata in `logs/requests.jsonl`; no bodies, headers or query strings. |
| `security.redact` | `false` | `true`: filter the output/log surfaces described above. Restart child processes to replace their log descriptors. |
| `security.upload_sniff` | `false` | `true`: validate supported upload signatures and reject binary text uploads. |
| `security.mcp_approval` | `false` | `true`: discovery and calls require command/args/env approval through Settings → Test. Changes invalidate the stored fingerprint, even if tools are cached. |
| `security.csp` | `"off"` | `"report-only"`: emit `Content-Security-Policy-Report-Only`. `"enforce"`: enforce the validated policy (authorized for the plan; the default remains off). |

Example for isolated development:

```json
{
  "security": {
    "redact": true,
    "upload_sniff": true,
    "mcp_approval": true,
    "csp": "report-only"
  }
}
```

The CSP permits same-origin resources only: every script, style and font is bundled (`frontend/vendor/`), plus
inline styles and same-origin/data/blob images. No other host is allowed, and `check_app.py ui_loads_nothing_from_another_host`
fails the build if the page, a stylesheet or a module loads anything from one. It disallows inline scripts and event handlers,
plugins, foreign connections, base URLs and framing. The UI has external script/module files and
delegated event handlers, so no inline-script nonce is needed. `/api/csp-report` accepts at most
16 KB per report and stores bounded JSON lines in `STATE/logs/csp.log` only while CSP reporting
is enabled. Reports are diagnostic input, never instructions. A clean log is meaningful only if
the browser loaded the policy and exercised the relevant feature; it is not a full security test.

## Local API authentication and execution isolation

`security.api_token` (default `false`) creates fresh random 32-byte credentials in
`STATE/data/.api-token` and a distinct, single-use startup ticket in `.api-bootstrap`, both mode 0600.
The Swift wrapper sets the port-specific HttpOnly/SameSite=Strict cookie through WKHTTPCookieStore
before loading; `start.sh` prints the one-time `/?t=...` URL, which sets the cookie and redirects to `/`.
Scripts read the bearer file; the search service has its own token (below). LocalOnly remains in force. All routes/static
files require authentication except `/api/system` and `/api/health`; the latter hides the selected model
while authentication is enabled. A cookie is scoped to the loopback host and path, not a TCP port;
a different local HTTP server may receive cookies if visited. This is additional protection from
incidental local API callers, not isolation from malicious code running as the same macOS user.

`harness.sandbox` (default `false`) invokes `/usr/bin/sandbox-exec` for foreground Python/shell and
managed background shell commands. Writes are limited to the selected workspace and a fresh per-process
temporary directory. Credential paths, secret files and protected macOS user data cannot be read.
Python networking is denied by default. A user can opt a particular chat in through the settings list
`harness.python_network_chats: ["chat_id"]`; models cannot grant this permission through tool arguments.
The permission uses a ContextVar bound/reset in that chat's execution worker. Root/home-wide workspaces
are refused, as is execution if Seatbelt is unavailable. A failing profile is never retried unsandboxed.
Seatbelt is a deprecated macOS facility and is not a portable sandbox or isolation for custom MCP servers.

`harness.rlimits` (default `false`) applies CPU (30 seconds), per-file size (64 MiB), and open-file (128)
limits before exec, plus an RSS process-group watchdog (1 GiB, sampled every 100 ms). The existing tool
timeouts and process groups remain. `harness.limits` can reduce `cpu_seconds`, `file_bytes`, `open_files`,
and `rss_bytes` below these ceilings. Memory control is best effort; kernel limits remain effective when
RSS sampling is unavailable. Background processes receive the same policy. These limits do not govern
model engines, CuaDriver, custom MCP executables or the interactive terminal.

Example additions: `{"security":{"api_token":true},"harness":{"sandbox":true,"rlimits":true}}`.
Enable execution controls only after selecting a specific workspace. Authentication can be toggled at
runtime but credentials rotate when enabled or when the server restarts; reopen the startup URL or Mac
window to receive the new cookie.

## Verification

`macos/check_agent.py` covers path/shell fences, approvals, backups/undo, MCP discovery refusing
unapproved configurations without spawning, invalidation after configuration changes, descriptor/result
framing, every split point in representative token/PEM streams, real subprocess/background log redaction,
and the chat SSE error path with a fake engine. `macos/check_app.py` covers all-response headers,
CSP modes/reporting, flagged magic validation, bounded uploads, symlink escape rejection and normal
media delivery, atomic write failure cleanup/readback, and route/SSE/patch-point contracts.
`macos/check_sanitizer.mjs` exercises malicious markdown under plain Node. The full build also runs
search, agent, document OCR, Swift compilation, bundling and signature checks.


## Stream replay

With `streams.resume=true`, POST `/api/chat` adds an `X-Chat-Stream-ID` header and monotonically
increasing SSE `id` lines. Existing JSON payloads are unchanged. GET `/api/chat/streams/{id}?after=N`
replays later frames; DELETE cancels the producer. The producer retains its approval/question session
when an HTTP subscriber leaves. The client reconnects with GET, never another POST, and saves the handle
so a page reload can offer Resume reply. Replaying from zero rebuilds the saved turn without duplicating
actions. Stop aborts the reader and sends DELETE. Legacy streams have no replay header and retain EOF behavior.

Buffers are process-local: at most eight streams, 4,096 events and 2 MiB per stream, with completed
buffers retained for at most five minutes (earlier completed buffers can be evicted for capacity).
An expired replay cursor returns 410 rather than restarting execution. Process restart loses buffers;
a saved handle then reports unavailability. This is reconnection support, not durable job scheduling.


## Search-service boundaries and incremental updates

`search.py` remains the command-line entrypoint, with code under `studio/search/`. The service's
Host/Origin/Fetch-Metadata checks remain intact. Since 1.6.0 every route except `/health` also requires the
service's own token, whatever `security.api_token` says: these routes return the text and bytes of indexed files, and
loopback is reachable by other accounts on the Mac and by sandboxed apps that may not read `~/Documents`. The backend
creates `STATE/data/search.token` (0600), passes it to the service in `LOCAL_STUDIO_SEARCH_TOKEN` at launch (the service
refuses to start without it) and sends it on every proxied call; the UI API token is not accepted in its place. A running
service that answers without the token (one started by 1.5 or older) is stopped and started again by `_start_search`.
`search.token` and `voice.token` are refused to the harness file tools and Seatbelt like `.api-token`. Search's own health
endpoint returns no user data. `LOCAL_STUDIO_UI_PORT` can redirect its busy probe in isolated development.

`search.incremental` defaults to false. When enabled, the normal crawler policy polls metadata
under configured roots once per second while idle, preserving unreadable/unmounted subtrees.
Changes are debounced for 0.5 seconds and only changed paths are synchronized and re-embedded;
unchanged vectors remain. New files and deletions are included. This trades metadata polling
work for shorter edit-to-search delay; it does not add unconfigured workspaces to the search roots.
Full crawl/index passes return HTTP 503 with Retry-After on search/passages until complete, and
the UI proxy preserves that header. The previous periodic-rescan behavior remains when disabled.

## Release gate

`macos/release.sh --dry-run` audits both exact-pinned requirement sets through temporary projects,
then verifies the built app with `codesign --verify --deep --strict`. Audit or signature failure
stops the release. The installed uv build's standalone `--script` audit path did not work; a
temporary pyproject was verified with both dependency sets and leaves no project/lockfile in the checkout.
Developer ID signed builds additionally run `spctl -a -vv --type execute`; a local-development
signature does not satisfy that distribution condition. Dry run creates no DMG and submits no notarization.
