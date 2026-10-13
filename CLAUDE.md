# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Local Studio is a private, on-device AI studio for Apple Silicon Macs: chat (MLX models with
DFlash/DSpark speculative decoding, or GGUF on llama.cpp), document/image attachments, and
text-to-image via mflux. It ships as a self-contained macOS app (`Local Studio.app`) and also runs
from a source checkout. There are only a handful of source files:

- `app.py`: the entire FastAPI backend (one file, ~1200 lines, organized by `# ----` section banners)
- `index.html`: the entire single-page UI (inline CSS + one inline `<script>`; vanilla JS, `marked` from a CDN)
- `catalog.json`: curated chat and image models
- `search.py` + `search-requirements.txt`: the file-search service (EmbeddingGemma 2 on MLX) and its dependencies
- `setup.sh` / `install.sh` / `start.sh` / `stop.sh` / `uninstall.sh`: zsh lifecycle scripts
- `macos/`: native wrapper (`main.swift`, a WKWebView around the UI), icon generator (`logos.py`), build/release scripts

## Commands

```sh
./setup.sh --install      # install engines (uv, mlx-dspark, mflux, llama.cpp); no models
./setup.sh --check        # status report; exit 0 ready, 1 needs install, 2 unsupported Mac
./start.sh                # UI on http://127.0.0.1:7860 + engine for the active chat model
./start.sh --ui           # UI only (start the engine later from the UI)
./stop.sh                 # stop all engines, the UI and image jobs

macos/build.sh            # build build/Local Studio.app (also the pre-PR check, see below)
macos/build.sh --install  # build and copy to /Applications
macos/release.sh          # build + DMG in build/, signs/notarizes if a Developer ID is set up
```

There is no test suite or linter. `macos/build.sh` is the validation step the README asks for before
a PR: it runs `node macos/check_ui.js index.html` (executes the UI script against a stub DOM so
start-up errors such as a `const` used before its declaration fail the build) and `ast.parse`s
`app.py`. Both checks can be run on their own:

```sh
node macos/check_ui.js index.html
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" app.py
```

The search feature has its own checks:

```sh
uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py
uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py
uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --eval build/search_samples
```

The first two run in `macos/build.sh`. The third needs the model and a sample folder built by
`macos/make_search_samples.py build/search_samples`.

The backend has no project/venv: `start.sh` runs it with
`uv run --no-project --python 3.12 --with fastapi --with uvicorn ... uvicorn app:app`. If you add a
Python dependency, add it to **both** `start.sh` and `APP_DEPS` in `setup.sh`. After editing
`app.py`, restart the UI server (`./stop.sh && ./start.sh --ui`); there is no auto-reload. Logs and
pid files go to `logs/` (`ui.log`, `llm.log`, `vlm.log`, `gguf.log`, `download-<id>.log`).

## Architecture

### Code vs. user data

`ROOT` (where `app.py` lives) holds code; `STATE` = `$LOCAL_STUDIO_HOME` holds user data
(`gallery/`, `uploads/`, `logs/`, `data/settings.json`, `data/chats/`). From a checkout `STATE`
defaults to the checkout itself (those folders are gitignored). The Mac app bundles the code into
`Contents/Resources/app` and sets `LOCAL_STUDIO_HOME` to `~/Library/Application Support/Local Studio`.
Always resolve user-data paths from `STATE`, never `ROOT`. Models live in the shared Hugging Face
cache (`HF_HUB_CACHE`, default `~/.cache/huggingface/hub`), not in the project.

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
`LOCAL_STUDIO_SEARCH_ROOTS` to a sample folder so the real default folders are never crawled.
Because these routes return the text of the user's files, `/api/search*` and `/api/chat` reject
requests with a non-loopback `Host` or a foreign `Origin` (`LocalOnly` in `app.py`), and the
service itself refuses any request that carries an `Origin` (`_local_request`).

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
of files into the bundle (`app.py index.html catalog.json search.py search-requirements.txt setup.sh start.sh stop.sh uninstall.sh`).
Any new runtime file must be added to that list.

## Versioning and releases

The version lives in `APP_VERSION` in `app.py`. `build.sh`/`release.sh` read it from there for
the bundle and the DMG name, and the build number is the git commit count. The README also shows
the version (badge and DMG filename), so update those together. Commit messages follow the pattern
`Local Studio X.Y.Z: <summary>` with a bulleted body for releases.
