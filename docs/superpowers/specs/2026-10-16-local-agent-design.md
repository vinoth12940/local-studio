# Local Agent: design

Date: 2026-10-16. Status: approved; amended after the CuaDriver probe (see Amendments). Target release: Local Studio 1.4.0.

## Goal

Let the user hand Local Studio a task in plain language and have a local model carry it out on
the Mac: browse the web, read pages, fill forms, and operate Mac apps, step by step, while the
user watches and approves anything risky. Everything runs on-device: the model is one of the
existing chat engines and the hands are CuaDriver, already installed on this Mac.

Success means the agent completes the acceptance tasks (Testing) within the step limit, every
risky action stops at the approval gate, and nothing the agent reads can change its task.

## Decisions made with the user

| Topic | Decision |
|---|---|
| First capability | Web tasks, with Mac app control in the same release (behind per-app tiers). |
| Browser | An isolated browser with its own profile, never the user's everyday Chrome. |
| Autonomy | Act freely on safe steps; pause for approval before submit, send, buy, post, delete, download. |
| Watching | Inside Local Studio: a live screenshot beside a step log. |
| Sign-in / stuck | "Take over": bring the agent's browser (or app) forward, the user acts, then Resume. The agent never types passwords. |
| Page understanding | Text first (accessibility / DOM refs). A screenshot goes to the model only when the text view is empty and the model reads images. |
| Hands | CuaDriver (`com.trycua.driver`), used as an MCP server. Local Studio is the MCP client; no new MCP server is written. |
| Safety fence (after the probe) | One CuaDriver daemon in `standard` mode with no capability manifest. Local Studio's MCP client refuses any CuaDriver tool outside a fixed allowlist, and the approval gate decides every action. |
| Acceptance tasks | Research and summarize; look up a fact; fill a form and stop before submit; multi-step on a site (add to cart). Plus one Mac-app task. |

## What CuaDriver provides (read from `cua-driver` 0.28.2 on this Mac, 16 Oct 2026)

Installed as `/Applications/CuaDriver.app`, symlinked at `~/.local/bin/cua-driver`. Read-only
inspection (`list-tools`, `describe`, `manifest`, `telemetry status`) showed:

- `cua-driver mcp` is an MCP stdio server. On macOS it proxies to an app daemon; `--socket`
  selects an explicit daemon endpoint.
- `cua-driver serve` takes an immutable `--permission-mode` (`standard`, `bounded`,
  `unrestricted`); `bounded` requires `--capability-manifest <path>` and
  `--approve-capability-manifest`, and limits the daemon to the listed tools and resources.
- Browser: `browser_prepare` with `allow_launch=true` and `profile.mode=isolated_named`
  launches a separate driver-owned Chromium and never touches the user's profile.
  `get_browser_state` binds a native window to a session-scoped `target_id` / `tab_id`, and in
  `semantic_v2` returns a ranked semantic outline with typed action refs; it can include a
  viewport PNG captured through CDP without foregrounding the window. `browser_navigate`,
  `browser_click`, `browser_type`, `browser_pointer`, `browser_dialog`, `browser_download` act
  on refs in a bound tab.
- Mac apps: `list_apps`, `list_windows`, `launch_app` (opens in the background),
  `get_window_state` (AX tree as structured `elements` plus Markdown), `click`, `double_click`,
  `type_text`, `set_value`, `press_key`, `hotkey`, `scroll`, `invoke_menu`, `zoom` (cropped
  JPEG of a window region), `bring_to_front`.
- Sessions: `start_session` / `end_session`; browser targets, tabs and refs belong to a session.
- Telemetry is **enabled by default** and sends events to `eu.i.posthog.com`.
- The daemon is shared: Hermes (connected on this Mac) also uses CuaDriver.

Not yet verified (the live probe, Delivery step 0): a second daemon on its own socket alongside
another one; `bounded` mode with our manifest permitting the isolated-profile launch; which
permission identity a daemon started by Local Studio runs under (options A and B under "Our
daemon"); real `semantic_v2` snapshot sizes and per-step latency with Qwen3.8 27B. The latency
measurement reuses whatever engine the user's running app has loaded, or switches engines only
with the user's OK.

## Architecture

```
index.html ──SSE──▶ app.py  # ---- agent
 (Agent view)        ├─ agent loop + approval gate + tool adapter
                     ├─▶ chat engine (8080/8081/8082), OpenAI tool calls
                     └─▶ cua-driver mcp --socket <STATE>/data/agent/cua.sock (stdio JSON-RPC)
                              └─▶ Local Studio's own daemon (standard mode, no manifest)
                                    ├─ isolated Chromium, profile "local-studio"
                                    └─ Mac apps via Accessibility
```

### Files

| File | Change |
|---|---|
| `app.py` | New `# ---- agent` section: MCP client, daemon lifecycle, tool adapter, gate, loop, routes. |
| `index.html` | Agent view, Agent settings, sidebar item. |
| `stop.sh` | Stop our daemon by its literal pattern and the Chromium it launched. |
| `app.py` (existing routes) | `/api/chat`, `/api/image` and `/api/busy` respect `agent_lock` (Agent loop). |
| `macos/check_agent.py` | New, run by `macos/build.sh` with the same `uv run` dependencies as `check_app.py` (fastapi, httpx), since it imports the gate, view trimmer and history folding from `app.py`, where they are pure functions. |
| `macos/check_app.py` | Agent route tests with a fake MCP client and fake engine. |

No new runtime file goes in the bundle, so the `macos/build.sh` copy list is unchanged. No new
Python dependency: the MCP client is hand-written JSON-RPC over the subprocess's stdio.

### MCP client

About 80 lines in `app.py`: spawn `cua-driver mcp --socket <sock>`, send `initialize`, read
`tools/list` once (schemas are cached for the session, not passed to the model), then
`tools/call` per action with a 30 s timeout. Requests are serialized; one client per daemon.
If the process exits, the next call restarts it once.

### Our daemon

Started on first agent use with
`open -n -g -a CuaDriver --args serve --socket <STATE>/data/agent/cua.sock --permission-mode standard`.
The paths are absolute because option A below launches through `open -a`, which does not keep
the working directory. The Mac app's `STATE` contains spaces, so the stop pattern is
`cua-driver serve --socket <re.escape(socket path)>` with no `^` anchor (the `[^ ]*` prefix that
forced `search.py` onto relative names is not used). The socket path must stay under 104 bytes.
A development server with its own `STATE` therefore gets its own socket and pattern automatically.
Another daemon (for example Hermes's) is never reused, signalled or stopped. `stop_agent_daemon` and `stop.sh` use
the same pattern; keep the two in sync, as with search.

**Permission identity (decided by the probe).** macOS grants Accessibility and Screen Recording
per app identity, and a child process of `app.py` may run under the parent's identity rather than
`com.trycua.driver`. Two options:

- **A (preferred):** launch through LaunchServices,
  `open -n -g -a CuaDriver --args serve --socket … --permission-mode bounded …`, so the grant made
  by `cua-driver permissions grant` applies. The stop pattern still matches on the arguments.
- **B:** `cua-driver serve --embedded`, which inherits Local Studio.app's grants; the Mac app
  then requests Accessibility and Screen Recording itself, and a source checkout inherits the
  terminal's.

The MCP client connects with `cua-driver mcp --socket <STATE>/data/agent/cua.sock`.
The daemon stops after 10 idle minutes and when Local Studio stops. In development, a separate
`STATE` (port 7861) gives the dev server its own socket, daemon and stop pattern, away from the
running app.

### CuaDriver tool allowlist (enforced by Local Studio's MCP client)

Included: `start_session`, `end_session`, `get_session`, `check_permissions`, `health_report`,
`browser_prepare`, `get_browser_state`, `browser_navigate`, `browser_click`, `browser_type`,
`browser_pointer`, `browser_dialog`, `browser_download`, `list_apps`, `list_windows`,
`launch_app`, `get_window_state`, `click`, `double_click`, `type_text`, `set_value`,
`press_key`, `hotkey`, `scroll`, `invoke_menu`, `zoom`, `bring_to_front`.

Excluded: `kill_app`, `clipboard_read`,
`clipboard_write`, `set_config`, `start_recording`, `stop_recording`, `replay_trajectory`,
`browser_set_input_files`, `get_desktop_state` (full-display capture), `set_window_frame`,
`move_cursor`, `drag`, `mouse_*`, `escalate_session`, `page`, `check_for_update`,
`install_ffmpeg`.

`McpClient.call` refuses any name outside the included list before anything is sent, so neither
the model nor an adapter bug can reach the excluded tools. (The probe showed a CuaDriver
capability manifest cannot express open-web browsing; see Amendments.)

## Tool adapter (what the model sees)

The adapter hides pids, window ids, `target_id` / `tab_id`, sessions and ref formats. Each run
gets one CuaDriver session labelled `ls-<run id>`. The model gets these tools, each mapped to one
or more CuaDriver calls:

| Tool | Does |
|---|---|
| `open_url(url)` | Prepare and bind the isolated browser on first use, then `browser_navigate`. |
| `read_page(query?)` | `get_browser_state` `semantic_v2` (optionally `query`), trimmed into the view. |
| `click(ref)` | `browser_click`, or `click` on an AX element for apps. |
| `type(ref, text, submit=false)` | `browser_type` / `type_text`; `submit` presses Enter after. |
| `scroll(direction)` | `browser_pointer` scroll / `scroll`. |
| `back()` | Navigate to the previous URL from the adapter's own URL history. |
| `list_apps()` | `list_apps`, trimmed to name and running state. |
| `open_app(name)` | `launch_app`, then bind its main window. |
| `read_app(name)` | `get_window_state`, trimmed into the view. |
| `menu(app, path)` | `invoke_menu`. |
| `hotkey(keys)` | `hotkey` / `press_key` into the bound app or tab. |
| `note(text)` | Append to the run's scratchpad (no CuaDriver call). |
| `ask_user(question)` | Pause and show the question; the reply becomes the tool result. |
| `done(answer)` | End the run with a markdown answer. |

Refs are rewritten to short ids (`e14`) per view; the adapter keeps the map to CuaDriver refs.
A stale ref returns "Page changed; here is the new view" with a fresh snapshot.

## Agent loop

`POST /api/agent/run {task, model_id}` streams SSE events (`x_engine`, `step`, `approval`,
`question`, `answer`, `error`, `end`). One run at a time (`agent_lock`).

**Sharing the machine during a run.** `ensure_engine` stops any other engine, so a chat with
another model, or a chat message with an image while the run is on `text`, would kill the run's
engine mid-step. While `agent_lock` is held:

- `/api/chat` with the run's model and engine waits for the current agent step to finish, then
  proceeds (it shares the engine; the agent's next step waits for it in turn).
- `/api/chat` that would switch the engine or model returns an error event, "The agent is
  running a task. Stop it or wait for it to finish", which the UI shows like other chat errors.
- `/api/image` returns 409 with the same message (image jobs compete for memory).
- `/api/busy` reports busy, so the search service pauses indexing.

Each step:

1. **Observe**: read the current surface (bound tab or bound app) into a view of short refs,
   e.g. `[e14] button "Add to cart"`, capped at ~6k tokens, visible and interactive elements first.
2. **Think and act**: send system prompt + tools, task, scratchpad, folded step history and only
   the newest view. Expect one tool call.
3. **Gate**: classify the call as safe, risky or blocked (Safety). Risky pauses for the user.
4. **Execute** through CuaDriver; record result and an after-screenshot (for the UI only).
5. Repeat until `done`, the user stops it, or a limit is hit.

### Context budget (fixed 32k `CONTEXT`)

System prompt and tools ~2k; task and scratchpad ~1k; step history ~3k (oldest steps fold into
one summary line); current view ≤6k; reply ≤2k. About 14k total, leaving room for thinking
models and a ~1.2k screenshot. Older views are always dropped.

### Engine per run

Uses `ensure_engine`, so the one-engine rule holds. MLX models start on `text` (DFlash/DSpark).
The vision fallback triggers when a view has fewer than ~5 interactive elements or almost no
text and the model reads images; the run then switches to `vision` once and stays there. GGUF
models with an `mmproj` never switch. Models without vision report the page can't be read and
offer Take over.

### Robustness

- No valid tool call: extract a JSON `{"tool":…,"args":…}` from the text; if that fails,
  re-prompt once with "Reply with exactly one tool call".
- Same tool and arguments failing three times: tell the model to try another way or `ask_user`;
  a fourth repeat ends the run.
- Limits: 40 steps and 15 minutes by default (Settings). Stop takes effect between steps.

## Safety

The adapter decides risk from facts about the target (element role, input type, label, form,
URL, app), never from the model's description. Page and app text is untrusted data: the system
prompt says so, and nothing read from it can change the task or skip the gate.

**Web, risky (pause):** a submit control, or a button/link whose label matches send, submit,
buy, pay, order, checkout, confirm, delete, remove, post, publish, sign up, subscribe, book,
transfer; Enter or `submit=true` inside a form; any download; navigating to a domain that the
user did not name and that was not reached by following links, when the URL came from page text;
a page `confirm` or `beforeunload` dialog.

**Web, blocked:** typing into password, credit-card or one-time-code fields (by input type and
autocomplete); the agent calls `ask_user` and the user uses Take over. File uploads (not in the
manifest).

**Mac apps, three tiers (Settings, editable):**

| Tier | Behaviour | Default apps |
|---|---|---|
| Read-only | Read, never act. | Banking/finance apps, Keychain Access, System Settings, Terminal and other shells, password managers. |
| Ask | Every changing click or keystroke pauses; reading and scrolling do not. | Mail, Messages, WhatsApp, Slack, Calendar, Finder. |
| Allowed | Acts freely; the web label rules (send, delete, …) still pause. | Every other app. |

**Approval card:** the exact target ("Click **Place order** on amazon.com") with a screenshot
outlining it; Allow once, Deny (the model is told and continues), Stop run. No "always allow" in
v1. The MCP client's tool allowlist is a second fence behind the adapter. CuaDriver's agent cursor stays on
so app actions are visible. Every action, gate decision and user answer is in the run log.

## Interface

- **Sidebar:** Agent, after Search, shown once Agent is on.
- **Agent view:** two panes (stacked when narrow). Left: task box with model picker (each model
  marked text-only or vision-capable) and Run, example chips when empty; step log (✓ ⏸ ✕ ↻, click
  for screenshot and raw call, reasoning collapsed); inline approval cards and `ask_user`
  questions; the final answer as markdown with the pages used; "Continue in chat". Right: live
  view (newest screenshot, current URL or app), Take over, Stop. A "Recent tasks" list opens
  saved runs.
- **Take over:** browser: `bring_to_front` on the agent's Chromium, the user acts, Resume re-reads
  the page. Apps: the run pauses, the user acts, Resume.
- **Waiting for approval:** a title badge ("⏸ Agent needs your OK") and a highlighted Agent item
  in the sidebar. `macos/main.swift` has no notification bridge today; native notifications are
  out of scope for 1.4.0.
- **Settings → Agent:** on/off; CuaDriver status (version, permissions, telemetry with a Turn
  off button that runs `cua-driver telemetry disable`); app tiers; step and time limits; Reset
  browser profile.

## Setup and failure handling

- Agent is off by default, like Search. Turning it on checks for `cua-driver` ≥ 0.28.2 (link to
  CuaDriver's installer if missing), runs `cua-driver permissions grant` once, and shows the
  telemetry state. Local Studio never changes CuaDriver settings without the user's click.
- Missing, outdated or unpermitted CuaDriver: the Agent view shows a setup card with the exact fix;
  no run starts.
- Daemon dies mid-run: restart once and re-bind; otherwise end with "Lost connection to
  CuaDriver", keeping the steps so far.
- CuaDriver refusals (`browser_requires_setup`, `permission_denied`, stale ref, bounded refusal)
  become short tool results; stale refs trigger a fresh view; bounded refusals are logged as
  blocked.
- Engine errors: as in `/api/chat`, including `x_engine` switching events.
- Local Studio quitting mid-run: the run is saved as `interrupted`; `stop.sh` stops our daemon and
  its Chromium only.
- Runs are saved in `STATE/data/agent/runs/<id>.json` (task, steps, decisions, answer,
  screenshots as files beside it). The browser profile lives under CuaDriver's isolated profile
  named `local-studio`; Reset browser profile removes it.
- Routes `/api/agent/*` are guarded by `LocalOnly`.

## Testing

- `macos/check_agent.py` (no model, no CuaDriver), run by `macos/build.sh` like `check_app.py`: gate rules against a
  table of element/URL/app facts and expected safe/risky/blocked; view trimmer stays inside the
  budget and keeps interactive elements; history folding; JSON tool-call extraction from replies
  without a proper tool call; CuaDriver result → tool result mapping from recorded fixtures.
- `macos/check_app.py`: `/api/agent/*` rejects foreign `Host` / `Origin`; a fake MCP client and
  fake engine drive a scripted three-step run that pauses at an approval and resumes.
- Live probe (Delivery step 0, with the user's OK): dev server on port 7861 with its own `STATE`;
  start our daemon beside Hermes's; isolated-profile launch; record snapshot
  sizes and per-step latency with Qwen3.8 27B. Results go in Amendments.
- Acceptance runs: the four example tasks plus "Make a new note in Notes titled Groceries with
  milk and eggs". A task passes when it finishes correctly within the step limit with every risky
  action stopped at the gate. Results go in Amendments as a pass/fail table.

## Delivery

0. **Probe**: verify the open points under "What CuaDriver provides"; adjust this spec if needed.
1. **Core + web**: MCP client, daemon lifecycle, adapter (web tools), gate, loop, routes, Agent
   view, Settings, checks.
2. **Mac apps**: app tools, tiers, Take over for apps, the Notes acceptance task.

Version 1.4.0: `APP_VERSION`, the README badge and DMG file name change together.

## Out of scope for 1.4.0

- File and shell tools; several tasks at once; runs that outlive Local Studio.
- Driving the user's everyday Chrome profile (`existing-profile`).
- File uploads from the agent; "always allow" approvals; scheduled or recurring tasks.
- Bundling or auto-installing CuaDriver.
- Native macOS notifications for approvals.

## Risks

| Risk | Mitigation |
|---|---|
| Local models pick wrong refs or loop. | Short refs, one newest view, repeat detection, step limit, `ask_user`. |
| CuaDriver is third-party and changes fast (0.28.x). | Minimum version check; adapter isolates its API; fixtures in `check_agent.py`. |
| Telemetry leaves the Mac by default. | Shown in Settings with a one-click Turn off; never changed without the user. |
| A second daemon may conflict with Hermes's or with macOS permission attribution. | Verified in the probe: both run side by side, and LaunchServices start gets CuaDriver's own grants. |
| No CuaDriver-level fence (standard mode, no manifest). | Allowlist in the MCP client, tested by `check_agent.py`; the approval gate; CuaDriver's own hard invariants still apply (no attaching to the user's real browser profile without a grant). |
| Prompt injection from pages or app content. | Gate decides from facts, not model claims; blocked fields; bounded manifest; no "always allow". |
| Acting in real Mac apps touches real data. | Tiers default to Read-only / Ask for sensitive apps; visible agent cursor; full run log. |

## Amendments (16 Oct 2026, after the CuaDriver probe)

Probe on this Mac with CuaDriver 0.28.2 (0.36.0 is available upstream and was not tested), a
probe daemon on a socket under `build/agent-probe/`, and the user's running app left alone.

| Question | Result |
|---|---|
| Permission identity | Option A works: started with `open -n -g -a CuaDriver --args serve …`, the daemon runs as `com.trycua.driver`; Accessibility and Screen Recording were granted by the user during the probe. Option B is not needed. |
| Second daemon beside Hermes's | Works; the existing daemon on the default socket was never touched. |
| Bounded mode and the manifest | Manifest is YAML v3 (`version: 3`, `expires_after` ≤ 24h, `idle_timeout`, `allow.tools`, `resources.{desktop,apps,browser}`). Apps must be listed by exact `bundle_id` (`*` parses but matches nothing). Browsing needs `resources.browser.origins` listing exact origins (no wildcards, `about:blank` included), and an origin list forbids `click`, `double_click`, `scroll`, `type_text`, `press_key`, `hotkey`, `set_value` and `get_window_state` in the same daemon. Any manifest, even in `standard` mode, denies unlisted origins. **Decision (user, 16 Oct): one `standard` daemon, no manifest, Local Studio's allowlist as the fence.** |
| Isolated browser | `browser_prepare {allow_launch, profile: {mode: isolated_named, name: local-studio}}` launches Google Chrome with the profile at `~/Library/Application Support/CuaDriver/BrowserProfiles/local-studio` (Reset browser profile deletes this folder while no run is active). Its window is real and on screen (not headless) but never foregrounded. It closes when the MCP session ends, and sessions end when the MCP connection closes, so one connection is kept per run; a fresh session label per run. |
| Binding | `list_windows {pid}` (retry until an on-screen window taller than 300 px appears, ~0.5 s), then `get_browser_state {pid, window_id}` → `target_id`, `tabs[0].tab_id`. |
| Actions | `browser_navigate` 0.1–0.8 s. `browser_type` works in the background (~1.5 s). `browser_click` must pass `input_route: "dom_event"` (the default trusted route is refused because it would foreground the window); its effect is unverified, so the loop always re-reads the page. |
| Views | `semantic_v2` is viewport-scoped with a 300-node budget and a continuation: refs carry `role`, `name`, `actions`, `states`, `value`, `visibility`; `outline` is compact (8.8k chars for a long Wikipedia article). It can fail once right after navigation (`browser_route_unavailable`); retry after 0.5 s. Element bounds are not included, so browser approvals show no outline. `dom_refs_v1` shows HTML attributes (`type=password`, `type=submit`, `autocomplete`, `name`, `id`); password and submit facts come from joining it to the semantic refs. |
| Password fields | CuaDriver typed into a password field without objecting, so blocking is Local Studio's gate alone. |
| Mac apps | `launch_app {bundle_id}` opens in the background and lists window ids; `get_window_state {pid, window_id}` returns `elements` with `frame` (screen points), `element_index`, `actions`, `label`, `role`; Notes gave 329 elements (~18k tokens as markdown), so `max_elements`/`max_depth` and the view trimmer are required. |
| Model step | LFM2.5-24B-A2B (the engine the user's app had loaded): 2,652 prompt tokens, 61 output tokens, 4.0 s, native `tool_calls`. Qwen3.8 27B was not measured, to avoid switching the user's engine. |

Fixtures from these runs are in `macos/fixtures/agent/` (public pages only; the Notes tree has all
note text replaced).

## Amendment: Agent switch in chat (16 Oct 2026, user-approved)

Chat gets an **Agent** pill next to My files (shown when Agent is on and CuaDriver is ready).
With it on, a message starts an agent run (`POST /api/agent/run` with the chat's model, the last
chat turns as `context`, and `chat_id`); the run appears inline in the thread as a compact card
(status, latest screenshot, collapsed steps, inline approval/question cards, Stop / Take over),
built from the Agent view's own components and sanitizers. When it ends, the answer (images
stripped) becomes the assistant message, with a link to the run's steps. The context is fenced
as background data (`<<<CHAT … CHAT>>>`, every line indented) and never acts as a task.
With the pill off, chat gets one short system note (when Agent is on and CuaDriver ready) so it
tells the user about the Agent switch instead of claiming it can act. Live tests also changed:
the agent's time limit counts only time the Mac is awake (`time.monotonic`).
