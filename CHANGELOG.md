# Changelog

Each release that changes behavior gets a new version in `app.py` (`APP_VERSION`) and an entry here. The build number in
Settings (and in `Info.plist`) is the git commit count, so a report names exactly which build it came from.

## 1.6.0 (unreleased)

The first step of the [production-readiness plan](docs/production-readiness-plan.md): a safety net, no new features.

### Security
- The file-search service (port 8083) now requires its own token on every route but `/health`, whatever
  `security.api_token` says. Before, any program that could reach `127.0.0.1` (another account on the Mac, or a
  sandboxed app without file access) could read the text and bytes of indexed documents. The backend keeps the token in
  `data/search.token` (0600) and replaces a running service from an older version that still answers without it.
- `search.token` and `voice.token` are refused to the harness file tools and the Seatbelt profile, like `.api-token`.

### Privacy and offline use
- Fonts (Inter, JetBrains Mono), the markdown renderer (marked) and the terminal (xterm.js) are bundled in
  `frontend/vendor/`. Launching the app no longer contacts Google Fonts or jsDelivr. Offline, replies render as
  markdown and the terminal opens.
- The Content-Security-Policy (`security.csp`) allows no other host.

### Reliability
- Every request the chat route sends an engine is rendered with each installed model's chat template in the build
  (`check_templates.py`). System notes the loop adds later are folded into the leading system message, which Qwen
  requires.
- Spoken replies start in about a second: the clock line is last in the system prompt, so the engine's prompt cache
  holds, and opening Voice sends a one-token warm-up when a model is loaded.
- Voice errors say what went wrong ("No chat model is loaded…") instead of "Sorry, something went wrong", and a self-check
  runs when Voice starts.
- A real end-to-end voice check (`check_voice_e2e.py`, attach-only) measures what was heard, the answer and the time to
  first audio.

### Delivery
- CI: GitHub Actions runs `macos/build.sh` (every check, the OCR suite and an ad-hoc signed bundle) on Apple Silicon for
  every pull request and push to `main`.
- Settings shows the build number next to the version.

## 1.5.0 (2026-10-17 to 2026-10-27)

- **Voice conversation** (flag `voice.enabled`): a separate echo-cancelled voice service (Parakeet speech recognition,
  Phonon speech, barge-in), the Voice view with 13 characters and a transcript, and fixes for transcription on one MLX
  thread (#7), spoken turns with no reply (#8) and the Voice view layout (#9).
- **Backend and UI split** into `studio/` and `frontend/` modules, with route, SSE and patch-point contracts.
- **Workflow stack** behind default-off flags: Plan/Act/Auto tasks, durable runs, verification gate, hooks and test
  protection, reviews, queue-only triggers, isolated worktree workers, skills, nested instructions, and a real-model
  eval kit.
- Tool and inference reliability fixes, a 100k+ context window setting, a memory check before image jobs, and eval
  checks that never start or stop a real engine (#6).

## Earlier

- **1.4.3** (2026-10-17): Agent Harness, step-by-step thinking, on-device OCR.
- **1.4.0** (2026-10-17): an agent that works on your Mac (computer use through CuaDriver).
- **1.3.0** (2026-10-13): search your files.
- **1.2.2** (2026-10-05): open source under MIT.
