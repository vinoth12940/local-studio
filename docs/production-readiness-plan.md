# Local Studio: production-readiness plan

Audit of `main` + PR #10 (`reliability/voice-e2e`), 2026-10-28. A plan only: no code was changed for it.
Findings come from reading the code and this month's incidents; where something was read but not exercised, it says so.

## Summary

1. **The core works and is well guarded.** Chat, Code, Voice, Search, Images and computer use all run on-device. Approval
   gates, path fences, atomic writes, exact Python pins and more than a dozen offline check suites are real strengths.
2. **Every bug you hit this month was found by you, not by a check.** "Loading forever", the speech crash, the template
   error, the 4 s first word and the overflowing caption all shipped green. The missing pieces are the same every time:
   no CI, no real-browser UI test, engines that change under the app, and checks that never touch a real model.
3. **A lot is built but not shipped.** 25 features and safety controls sit behind flags that default to off. Your
   installed app runs two of them. That code has never been used for real by anyone, and the off state is what ships.
4. **One security gap matters now.** With the default settings, the file-search service on port 8083 gives the full text
   and raw bytes of your indexed documents to any program that can reach `127.0.0.1` ([details](#s1)).
5. **The plan is five phases, each one or a few PRs**, ordered by what would have caught this month's bugs. The
   flag decisions are yours (see [Your decisions](#your-decisions)).

## What the app is (feature map)

| Surface | What it does | Runs in | Port |
|---|---|---|---|
| **Chat** | Local models with streaming, thinking, attachments (images, PDFs, Office), markdown | UI server → one engine | 7860 → 8080/8081/8082 |
| **Agent harness** | 24 tools in chat: files, edits with diff/undo, shell/Python, web search/read, memory, todo, sub-agents, MCP servers, approvals | UI server | 7860 |
| **Code** | Explorer, editor, diff, real terminal (pty), the same agent chat docked | UI server | 7860 |
| **Voice** | Hands-free conversation: echo-cancelled mic, Parakeet STT, Phonon TTS, barge-in, 13 characters | voice service | 8084 |
| **My files (Search)** | On-device semantic index of Documents/Desktop/Downloads, OCR of scans (Vision), previews | search service | 8083 |
| **Computer use** | Model drives an isolated Chrome and Mac apps through CuaDriver, with per-app tiers and approvals | CuaDriver daemon | socket |
| **Images** | Text-to-image (mflux), gallery with settings sidecars | mflux CLI jobs | – |
| **Library** | Model catalog (11 chat, 3 image), downloads from Hugging Face, custom repos | UI server + `hf` | – |
| **Settings** | Context window, memory, MCP servers, computer access, voice, search, engine updates | UI server | – |
| **Workflow stack** (off) | Plan/Act/Auto tasks, durable runs, verification gate, hooks, test protection, reviews, triggers, worktrees, skills | UI server | – |
| **Mac app** | WKWebView wrapper, first-run setup, migration, Keep Running After Quit | `main.swift` | – |

Engine rule: one chat engine at a time (each can take 16 GB+). They are found by `pgrep` patterns and fixed ports.

## What is already strong

- **Isolation and gates.** Path fence and shell gate ([fences.py](../studio/security/fences.py)), approvals bound to
  sessions, diffs derived from real arguments, per-step undo and turn revert, and a computer-use gate that decides from
  target facts, never the model's claims.
- **Loopback guard on every route.** [local_only.py](../studio/security/local_only.py) checks Host, Origin and Fetch
  Metadata, which blocks DNS rebinding and browser CSRF.
- **Safe storage writes.** Atomic writes for settings, chats and engine state ([atomic.py](../studio/storage/atomic.py)).
- **Reproducible app dependencies.** Exact pins (`requirements-app.txt`, `search-`, `voice-requirements.txt`) and a
  dependency audit in `release.sh --dry-run`.
- **Contracts and evals.** Fixtures for routes, SSE events and patch points; a real-model eval kit with a baseline
  (`macos/evals/`); and, as of PR #10, chat-template rendering and a real end-to-end voice check.
- **Honest documentation.** A threat model mapped to the OWASP agentic Top 10 ([security.md](security.md)).

## This month's incidents and the check that was missing

| What you saw | Root cause | Check that would have caught it | Exists now? |
|---|---|---|---|
| Model "loading forever" (#6) | A test fake lacked `context_window`, so a check suite restarted the real engine | Tests must never reach a real engine; engine identity check | Guard added in #6/#10 |
| Voice: no reply, STT crash (#7) | MLX streams are per-thread; STT ran on a pool | A real-model voice test | `check_voice_e2e.py`, **opt-in, not in any gate** |
| "Sorry, something went wrong" twice (#8) | A late system message is rejected by Qwen's template | Render every request with each model's template | `check_templates.py` in build (#10) |
| Overflowing caption, cramped Voice view (#9) | CSS/layout never seen in a real browser by a check | Real-browser UI smoke test with screenshots | **Missing** |
| 3.6–4.4 s before the first spoken word (#10) | The clock line busted the prompt cache | A latency budget that fails the build | Measured in e2e, **not a gate** |
| Errors spoken as "Sorry, something went wrong" | Exceptions swallowed in the UI and backend | Error-surfacing rule plus logging | Voice fixed in #10; **51 silent `except: pass` remain** |

## Findings, ranked

Priority = (impact + risk) × (6 − effort), each scored 1–5 (from the tech-debt framework). S = security,
R = reliability, D = delivery, Q = quality.

| # | Finding | I | R | E | Score |
|---|---|---|---|---|---|
| S1 | Search service serves file text/bytes to any local caller by default | 5 | 5 | 2 | **40** |
| S2 | A "private" app loads Google Fonts, `marked` and xterm from the internet | 4 | 3 | 1 | **35** |
| D1 | No CI: nothing gates a merge except a build on your Mac | 4 | 4 | 2 | **32** |
| R1 | Engines float: unpinned installs and upgrades, unverified downloads | 5 | 5 | 3 | **30** |
| R2 | Engine supervision by machine-wide `pgrep` and fixed common ports | 5 | 4 | 3 | **27** |
| Q1 | No real-browser UI test; UI breakage is found by you | 4 | 4 | 3 | **24** |
| R3 | Errors swallowed; logs erased by restart; no diagnostics bundle | 4 | 4 | 3 | **24** |
| D2 | 25 default-off flags: a large unshipped surface | 4 | 4 | 3 | **24** |
| R4 | User data has no schema version, migration or export | 3 | 4 | 3 | **21** |
| D3 | Distribution: notarization not enforced, no update check, `curl \| sh` at first run | 4 | 3 | 3 | **21** |
| Q2 | Version stuck at 1.5.0 for 19 commits; docs drift | 2 | 2 | 1 | **20** |
| Q3 | Accessibility: 18 `aria-label`s for 88 buttons, no reduced-motion | 3 | 2 | 2 | **20** |
| Q4 | Very large modules (harness, agent loop, chat route) | 3 | 2 | 4 | **10** |

<a id="s1"></a>
### S1. File-search service is open to any local program (security)

- **Evidence.** [studio/search/auth.py](../studio/search/auth.py) returns `True` (allowed) whenever
  `security.api_token` is off, which is the default. [studio/search/api.py:82-130](../studio/search/api.py) serves
  `/file/{id}` (raw bytes), `/text/{id}` (OCR text), `/search`, `/passages`, `/preview`, `/image` and `/thumb`. Origin
  checks stop browsers, not programs. I read this in the code; I did not exercise it against your running app.
- **Why it matters.** Your documents are protected from other macOS users and sandboxed apps by `~/Library` permissions
  (`drwx------`). Loopback is not protected that way: another user account on this Mac, or an App Store app with network
  access but no file access, can count `file_id` upward and download passports, tax forms and visas. Port 7860's
  `/api/search` proxy has the same exposure. The engine APIs on 8080–8082 are open by design; they give away compute,
  not data.
- **Fix.** Require the token on 8083 regardless of the UI flag. Only the backend calls it, and it already has a
  token file pattern (the voice service does exactly this with `voice.token`). Then decide `security.api_token` for 7860
  (see [Your decisions](#your-decisions)). Add a check that an unauthenticated `GET /file/1` returns 401.

### D1. No CI (delivery)

- **Evidence.** The repo has no `.github/`. Every PR this month merged on the strength of `macos/build.sh` on your Mac.
- **Fix.** GitHub Actions on an Apple Silicon runner (`macos-15`): syntax, `check_ui.js`, the sanitizer, `check_app`,
  `check_agent`, `check_search`, `check_voice` (fakes), `check_templates` (structure only, since no models), workflow
  suites, and a bundle build without signing. Make it a required status on `main`. About 10 minutes per run (to measure).

### S2. Third-party network calls in an on-device app (privacy, offline)

- **Evidence.** [index.html:8-11](../index.html) loads Google Fonts on every launch, which sends your IP to Google, and
  `marked` from jsDelivr. Offline, replies fall back to plain text with `<br>`
  ([markdown.mjs:37](../frontend/core/markdown.mjs)). [terminal.mjs:15-17](../frontend/code/terminal.mjs) loads xterm from
  jsDelivr, so offline the terminal does not open.
- **Fix.** Vendor all three into `frontend/vendor/` (keep the SRI hashes as a check), add them to the `build.sh` copy
  list, and tighten the CSP to `'self'`. Then add an "offline launch" check: block the network, load the UI, and assert
  that markdown renders and the terminal opens.

### R1. Engines change under the app (reliability, supply chain)

- **Evidence.** In [setup.sh](../setup.sh), line 132 runs `curl … astral.sh/uv/install.sh | sh`, line 138 runs
  `uv tool install --upgrade mlx-dspark` (latest), line 142 installs `mflux>=0.20`, and line 64 takes llama.cpp's newest
  GitHub release (or `brew upgrade`). None of these is checksum-verified, and "Update engines" in Settings upgrades
  in place with no rollback.
- **Why it matters.** The app's Python is pinned, but the engine is the piece that parses tool calls and applies chat
  templates, and that is where #8 broke. Two people installing a week apart run different engines.
- **Fix.** Add `engines.lock`: exact versions of uv, mlx-dspark, mflux and llama.cpp (build tag plus sha256).
  `setup.sh` installs exactly those and verifies checksums. "Update engines" moves to the next locked set only after
  `check_templates` and a real-model smoke pass, and keeps the previous set for one-click rollback. Bumping the lock is
  a PR, so CI and the e2e gate run on it.

### R2. Engine supervision (reliability)

- **Evidence.** [engines.py:29](../studio/services/engines.py) finds engines by `pgrep -f` across the whole machine, and
  [engines.py:73](../studio/services/engines.py) treats any HTTP 200 from `127.0.0.1:8080/health` as ready. The ports
  are fixed in [config.py:95-97](../studio/config.py). 7860 is Gradio's default and 8080 is the most common dev port.
  `ensure_engine` holds `engine_lock` for up to 300 s. There is no memory check before load; `/api/context-window` only
  adds an advisory note.
- **Failure scenarios.** Another app on 8080 makes chat "ready" against the wrong server, or the load waits its full
  300 s. Another tool's `mlx-dspark serve` is taken for ours and stopped. A 20 GB model on a Mac with 12 GB free swaps the
  machine, and the engine then drops its cache, which is the slow-first-word cause from #10.
- **Fix.**
  - Own the child process: keep the `Popen` handle, write the pid and port into `engine.json`, and pick a free port at
    start.
  - Verify identity on readiness: `/health` must report the expected repo.
  - Detect a crash and restart once, then report "the engine stopped: <last log line>".
  - Admission check: compare the model's `ram_gb` plus a KV-cache estimate with available memory, and refuse or warn
    before loading.
  - Keep `stop.sh` patterns as a fallback for orphans only.

### Q1. No real-browser UI test (quality)

- **Evidence.** `check_ui.js` loads modules against a stub DOM. CLAUDE.md itself says selector and field mismatches
  pass it silently. #9 was found from your screenshot.
- **Fix.** Add a Playwright smoke suite against the isolated dev server (port 7861, its own `LOCAL_STUDIO_HOME`), with
  `/api/chat` stubbed as a synthetic SSE stream (never a real chat). It covers:
  - send a message and check the reply renders as markdown;
  - approve and decline an edit, then undo;
  - Code view save;
  - the Voice view's off, listening and speaking states via `LS.voiceHeard`, with a long caption that must not overflow;
  - Settings open and close.
  
  It saves screenshots at 1280 px and 390 px, with a pixel-diff budget, and runs in CI.

### R3. Errors and diagnostics (reliability, support)

- **Evidence.**
  - The backend has 51 `except …: pass` blocks and the frontend has 30 empty `catch`es.
  - Only 4 backend modules use `logging`.
  - [start.sh:22](../start.sh) writes `ui.log` with `>`, so the log from the crash that made you restart is erased by
    the restart.
  - `logs/bg-*.log` files accumulate.
  - There is no `window.onerror` or `unhandledrejection` handler.
- **Fix.**
  - Use one logger (JSON lines, size-rotated, redaction on) and keep the previous run's log as `ui.prev.log`.
  - Make every swallowed exception on a user path either surface a message or log one with context. The remaining
    `pass` blocks need a one-line reason.
  - Forward frontend errors to the backend log.
  - Add a **Copy diagnostics** button in Settings: versions, `engine.json`, flags, free memory and disk, and the last 200
    redacted log lines. This is what you would paste to me instead of a screenshot.

### D2. 25 default-off flags (delivery)

- **Evidence.** [flags.py:12](../studio/storage/flags.py) has 25 flags, all defaulting to the old behavior. Your installed
  settings turn on `harness.coding_workflow` and `voice.enabled` only.
- **Why it matters.** Each flag doubles a code path. The off path is what every user gets, and the on path has only been
  exercised by suites and a single eval. Flags are meant to be temporary.
- **Fix.** Triage every flag into ship-on, Labs (a labelled Settings section, documented as experimental) or remove.
  Shipping one means a soak first: a week on your machine plus its eval tasks. **The triage is your decision**; the table
  in [Your decisions](#your-decisions) gives a recommendation and the cost of each.

### R4. User data has no schema version (reliability)

- **Evidence.** Chats (`data/chats/*.json`), `settings.json`, memory and run journals have no `schema_version` and no
  migration step. [security.md](security.md) notes that atomic replace does not serialize competing read-modify-write.
  There is no export or backup of chats.
- **Fix.**
  - Add `schema_version` to each store, with forward migrations at load and tests that load fixtures saved by 1.5.0.
  - Put a lock around settings read-modify-write.
  - Add **Export all data** (zip of chats, memory and settings) and **Import**.
  - Show gallery and uploads size, with cleanup.

### D3. Distribution (delivery)

- **Evidence.**
  - [README.md:71-83](../README.md) points people to a public DMG and tells them how to get past Gatekeeper.
  - Builds are signed with your Developer ID Application certificate (build.sh found it in the keychain), but the README
    still says the DMG may not be notarized, and nothing checks that a release was.
  - There is no app update check; [updates.py](../studio/services/updates.py) updates engines only.
  - First run downloads uv via `curl | sh`.
- **Fix.**
  - Notarize and staple every release, and refuse to publish a DMG that `spctl` and `stapler validate` do not accept.
    **Done in PR #11, the way Murmur ships:** `release.sh --publish` (app and DMG notarized and stapled, `.sha256`), an optional
    tag-triggered release workflow, a README download link, and a Homebrew cask template ([RELEASING.md](RELEASING.md)).
    The last published DMG is 1.2.2 (2026-10-05), and the README pointed at a 1.5.0 DMG that was never published.
  - An update check against signed GitHub releases (notify only, with a link and the release notes).
  - Make first-run downloads come from `engines.lock` with checksums.
  - A clean-install test on a fresh macOS user account before each release.

### Q2. Versioning and docs drift (quality)

- **Version.** `APP_VERSION` has been `1.5.0` for 19 commits titled "Local Studio 1.5.0" since 2026-10-17. Only the
  build number tells them apart, so a bug report cannot easily say which build it came from.
- **Docs drift.**
  - The README says macOS 15+ (line 62) and 14+ (line 79), while `setup.sh` checks for 14.
  - [manual-test-plan.md](manual-test-plan.md) still says "56 changed files sit uncommitted".
- **Fix.** Bump the version on every release that changes behavior (semver), keep a `CHANGELOG.md`, show the version and
  build in Settings and in diagnostics, and fix the stale lines.

### Q3. Accessibility (quality)

- **Evidence.** `index.html` has 88 `<button>`s and 18 `aria-label`s. Many icon buttons rely on `title`. The Voice rings
  animate with no `prefers-reduced-motion` path.
- **Fix.** Label every icon button, make focus order and visible focus work across views, add a reduced-motion variant
  of the Voice view, and do a VoiceOver pass on Chat, Voice and Settings. The Playwright suite can assert names exist.

### Q4. Large modules (quality)

- **Evidence.** `services/agent/loop.py` is 99 KB, `services/harness.py` 91 KB, `api/chat.py` 58 KB (the chat route is
  most of it), and `macos/check_agent.py` is 6,476 lines.
- **Fix.** Do this last, with CI in place. Split the chat route into prepare → engine turn → tool loop → finalize, and
  split harness tools into `harness/tools/*`. Move code without redesigning it, as the earlier refactor did.

## Phases

Each phase is one or a few PRs. Every PR passes CI (once D1 lands) and `build.sh`. A phase is done when its exit
criteria are measured, not when the code is written.

### Phase 0: land what's in flight (today)

- Merge PR #10 (needs your approval) and install it.
- **Exit:** build 149+ installed; `check_voice_e2e` 5/5 against your model.

### Phase 1: a safety net (week 1, about 3 PRs)

**Status (2026-10-28):** built in PR #11 (stacked on PR #10). The full build passes locally and with an empty home. The real
search service returned 401 without its token. CI's first run caught a timing race in the recovery eval, which is now fixed;
CI runs on every push to the PR.


- **D1** CI on GitHub Actions, required on `main`.
- **S1** token on the search service, plus the 401 check.
- **S2** vendor fonts, `marked` and xterm; offline-launch check; CSP `'self'`.
- **Q2** version bump to 1.6.0, `CHANGELOG.md`, doc fixes.
- **Exit:**
  - A PR cannot merge red.
  - With networking blocked, the UI loads, markdown renders and the terminal opens.
  - An unauthenticated `GET 127.0.0.1:8083/file/1` returns 401.
  - No third-party requests at launch (checked with the network log).

### Phase 2: engines you can trust (week 2, about 3 PRs)

- **R1** `engines.lock`: pinned, checksummed installs and update with rollback.
- **R2** owned engine process: free port, identity check, crash restart, memory admission.
- **Exit:**
  - With another server on 8080, chat still works (or a clear message appears within 10 s).
  - Killing the engine mid-reply produces "engine stopped", then recovery on the next message.
  - Loading a model larger than free memory is refused with a reason.
  - `setup.sh --install` twice on two days yields byte-identical engine versions.

### Phase 3: see every failure (week 3, about 3 PRs)

- **R3** logger, rotation, previous-run log, frontend error forwarding, **Copy diagnostics**, the silent-except sweep.
- **Q1** Playwright smoke suite in CI.
- Latency budgets as a gate: `check_voice_e2e` runs in the release gate (attach-only, with your consent) and fails if
  p95 first audio exceeds 1.5 s.
- **Exit:**
  - Every scripted failure (no model, engine crash, template error, search down, voice down) shows a specific message
    and a log line.
  - The UI suite catches a deliberately broken selector and a deliberately overflowing caption.

### Phase 4: decide the flags; keep data safe (weeks 4–5)

- **D2** flag triage per your decisions; soak, then ship-on, Labs or delete.
- **R4** schema versions, migrations, settings lock, export/import.
- **Q3** accessibility pass.
- **Exit:**
  - At most 6 flags remain, all listed under Labs.
  - 1.5.0 data fixtures load and migrate in CI.
  - Export → wipe → import round-trips chats and memory.

### Phase 5: ship to other Macs (week 6)

- **D3** notarization enforced in `release.sh`, update check, clean-account install test, release checklist.
- **Q4** module splits, if time allows (they can trail).
- **Exit:**
  - The DMG opens on a fresh account with no Gatekeeper warning.
  - The previous release reports "1.6.x available".
  - The release checklist runs end to end from `release.sh`.

## Your decisions

1. **Which phase to start.** I recommend Phase 1. It is small, and everything after it depends on CI.
2. **Flag triage.** My recommendation and the cost of each choice:

   | Flag(s) | Recommend | Cost if turned on |
   |---|---|---|
   | `security.redact`, `security.upload_sniff` | Ship on | Rare false positives (a real token redacted; an odd file refused with 415) |
   | `security.csp` | `report-only` now, `enforce` after S2 | None visible once the vendored assets are in place; breakage shows in `csp.log` first |
   | `security.mcp_approval` | Ship on | One extra approval when you add or change an MCP server |
   | `security.api_token` | Ship on after S1 | Scripts that call 7860 must send the token; the Mac app handles it for you |
   | `harness.sandbox`, `harness.rlimits` | Ship on for coding chats after a soak | Some shell commands fail inside Seatbelt (network, writes outside the workspace) and need a per-chat opt-out |
   | `streams.resume`, `performance.chat_index`, `performance.isolated_io` | Ship on | Small memory for replay buffers; none visible |
   | `search.incremental` | Ship on | Background polling of search roots (battery) |
   | `logging.json_requests` | Fold into R3's logger, then delete | – |
   | `harness.coding_workflow`, `voice.enabled` | Ship on (you already run them) | Voice: a one-time speech-model download the first time Voice is turned on |
   | `harness.verification_gate`, `durable_runs`, `verification_fingerprints`, `nested_instructions`, `skills`, `hooks`, `protect_tests`, `workflow`, `review`, `triggers`, `worktrees` | Labs, then decide by use | Each needs its own soak with your model; unused ones after a month should be removed |
3. **Notarization credentials.** You already have a Developer ID Application certificate. Phase 5 needs the one-time
   `xcrun notarytool store-credentials notary` with your Apple ID; that sign-in is yours to do.
4. **Required status check.** The repo is public, so GitHub's macOS runners cost nothing. Making the `Checks` workflow a
   required status on `main` is a repository setting: yours to switch on (Settings → Branches), or I can do it with your OK.
