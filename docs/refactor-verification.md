# Refactor completion verification

Verified on 2026-10-18. The authoritative checklist is `docs/refactor-progress.json`.
The work follows the approved modularization/performance/security prompt; production flag defaults remain unchanged.

| Phase | Verified items |
|---|---|
| P0 | 10/10 |
| P1 | 22/22 |
| P2 | 7/7 |
| P3 | 13/13 |
| P4 | 3/3 |
| P5 | 2/2 |
| P6 | 3/3 |
| DoD | 9/9 |

P0–P2's historical completion records are preserved in the tracker. Current route/SSE/patch contracts,
UI module/sanitizer tests, the full native build, and the final interactive checks revalidate their current behavior.
The final audit also adds a strict ordered-route negative test and verifies `_is_safe_readonly_shell`
is byte-for-byte identical to the pre-split `c34ab16` implementation.

## Reproduce

```sh
TMPDIR=/tmp ./macos/build.sh
./macos/release.sh --dry-run
uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/bench_app.py --optimized --chats 2000
uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/bench_document.py
# The existing generator creates synthetic samples when needed:
uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python macos/make_search_samples.py build/search_samples
LOCAL_STUDIO_HOME=/tmp/ls-search-eval LOCAL_STUDIO_SEARCH_ROOTS=/tmp/ls-search-eval/empty PYTHONDONTWRITEBYTECODE=1 uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --eval build/search_samples
```

The build covers UI startup, sanitizer and stream transport/serialization, Python syntax, search,
independent search HTTP/auth/owner contracts, app routes/SSE/patch contracts, agent harness, Swift compilation,
synthetic scanned/filled/multipage documents and a real harness OCR read, bundling, and strict signature checks.
No failing check was removed or weakened. Search suite assignments were retargeted to defining modules;
all 206 pre-existing assertions and expected values were preserved. Search's HTTP payloads now have their own contract instead
of being mistaken for chat-agent events after moving into the package.

Final complete build: **277 successful check outputs**, Swift compilation, bundling and signature verification, exit 0.

## Evidence and decisions

- Full logs and browser snapshots are preserved locally under `build/final-verification/`.
- `docs/perf-baseline.md` records before/after outputs: scaled list p95 73.56 → 20.87 ms;
  polling under slow renders 946.12 → 2.07 ms; no >250 ms event-loop stalls.
- P5.sqlite is conditionally complete with a documented decision to retain JSON. The simpler measured
  cache meets targets at 2,000 chats / 50,000 messages; no SQLite migration, new database or deletion is claimed.
- Real-model search: 21 synthetic files / 24 chunks, zero errors, 20/20 expected top results,
  seven previews and video sampling pass before/after. Warm query p95 7.63 ms on this small corpus.
- Four synthetic scanned pages: 0.403 s maximum in three OCR runs. No personal documents were read.
- Browser 7861 used temporary STATE, a scratch workspace, enforcing CSP and API authentication. Synthetic
  turns verified send/stop, live view switch/reattach, 12 rounds, model-switch status, 1/2/3/Esc/Shift+Tab
  approvals, /compact, Save/Diff/diagnostics, Undo and Revert turn, terminal reload/reattach, dark mode and
  375px layout. No real chat engine or CuaDriver run was started. All these checks produced zero CSP reports.
- Replay was tested across an actual page reload and paused server approval using a synthetic-only SSE
  fixture; Resume reply restored the session, and approval completed the buffered stream without a second POST.
- All 12 feature flags are listed with defaults and opt-in instructions in `docs/security.md` and `CLAUDE.md`.
- Release dry run audits both requirement sets (25 app / 93 search packages, no known vulnerabilities reported)
  and verifies the signature. Gatekeeper distribution assessment is conditional on a Developer ID signature;
  the available certificate is Apple Development. No DMG, notarization, installation or publication was performed.
- The signed bundle was smoke-tested against temporary data for backend routes, frontend modules, ROOT/STATE,
  the search CLI, and nosniff headers. No Python bytecode was written to its resource seal.

All flags remain opt-in. Replay buffers are bounded and process-local; expired handles do not restart actions.
Resource/RSS controls and secret redaction have the limits documented in `docs/security.md`.

## Review follow-up (2026-10-18)

The review found two regressions and an unfinished launch configuration pinning step. All three are corrected:

- Failed Resume restores the saved reply, reasoning, tool history, rounds and Undo checkpoint before saving.
  Expired streams disable Resume; transient failures retain it. Successful replay replaces the saved reply.
  `macos/check_chat_resume.mjs` exercises the actual `generate()` and serializer for 404, 410, network retry
  exhaustion, partial replay followed by expiry, Stop, and successful replay. It runs in the build.
- A full index releases search backpressure during user, battery and active-chat pauses, then restores it
  when work resumes. The search HTTP suite exercises the real Indexer pause/resume loop for all three reasons
  and checks both `/search` and `/passages`, including 503/Retry-After during resumed work.
- Both `.claude/launch.json` configurations use `requirements-app.txt`. The launch file is now explicitly
  included by `.gitignore`; other local Claude files remain ignored.

Fresh full build: **286 successful check outputs**, native compilation, bundling and development signature
verification, exit 0. Log: `build/review-fixes-verification/build.log`. The isolated dev UI on port 7861 used
`/tmp/ls-review-fixes-ui`; synthetic 404/410/partial-replay/success cases verified visible answers and actual
chat-save API readback. The first browser test used an invalid synthetic chat ID and received the expected
400; rerunning with the application's ID generator passed all cases. No real chat was requested. The dev
server was stopped after verification. Release audits and real-model evaluation were not rerun for these fixes;
their earlier results above remain historical evidence.
