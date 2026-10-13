# Local Search: design

Date: 2026-10-13. Status: approved, amended after a follow-up probe (see Amendments). Target release: Local Studio 1.3.0.

## Goal

Let the user search their own files in plain language, entirely on-device. They turn Search
on, the app indexes their content in the background, and they can then:

- **Find**: type a question or description and get a ranked list of matching files and passages.
- **Ask**: turn on "My files" in chat so the active chat model answers from their files and
  cites the ones it used.

Success means: a file is findable by what it contains (text, picture, or spoken audio) and not
only by its name; nothing leaves the Mac; the index is easy to see, pause, rebuild and delete.

## Decisions made with the user

| Topic | Decision |
|---|---|
| Result style | Both Find and Ask. Find ships first. |
| Scope | The user's own content, indexed automatically (see Crawl scope). App data (Mail, Messages, Notes, Photos) is a later opt-in phase. |
| Indexing | Automatic and in the background once Search is turned on. |
| Structure | A separate search service process (`search.py`), not code inside `app.py`. |
| Embedding model | EmbeddingGemma 2 on MLX: `mlx-community/embeddinggemma-2-8bit`. |

## Feasibility test (13 Oct 2026, this Mac, 48 GB)

A throwaway test embedded 8 documents, 7 photos and 4 audio clips and ran 16 queries.

| | PyTorch (MPS, float32) | MLX 8-bit |
|---|---|---|
| Text, ~300-token chunks | 21 /s | 47 /s |
| Short texts | 323 /s | 956 /s |
| Images, 1024 px, default detail | 2.5 /s | 4.1 /s |
| Audio clips, ~5 s | 14 /s | 21 /s |
| One query | 421 ms | 38 ms |
| Memory | ~6 GB | ~2.3 GB peak |

- Both runtimes ranked the expected file first for every query, with near-identical scores.
- Correct matches scored 0.65 to 0.81; unrelated files 0.57 to 0.61. Two files (a short sound
  and one photo) appeared as generic near-matches for many queries.
- MLX support is in `mlx-vlm` at revision `3d87e88402f307efbf68e568971aa887ee7d9ed0` but not in
  the released 0.7.6. Image and audio preprocessing needs `transformers>=5.19.0`, which needs
  `torchvision` (and so `torch`).
- Not tested: video input, and llama.cpp (the installed build predates its support).

A follow-up probe pinned down the calls the service uses:

- The processor treats a list of images as **one** combined input and returns one vector, so
  images and audio segments are embedded one per call. The speeds above were measured that way.
- Audio is passed as `processor(audio=[array], sampling_rate=16000)`.
- `afconvert -f WAVE -d LEI16@16000 -c 1` works; a clip round-tripped through AAC scored 0.988
  cosine against the original.
- uv's Python 3.12 ships SQLite 3.50 with FTS5.
- Loading the cached model in a fresh process takes about 18 s, mostly library imports.

## Architecture

```
index.html ──► app.py (7860) ──► search.py (8083) ──► data/search/index.db
                  │                    │
                  │                    └─ EmbeddingGemma 2 (MLX), crawler, indexer
                  └─ chat engines (8080–8082), unchanged
```

`search.py` is a fourth process alongside the three chat engines. Unlike them it is small
enough (~2.3 GB) to run next to a chat model, so it is exempt from the one-engine-at-a-time rule
and is not managed by `ensure_engine`. Everything binds to `127.0.0.1`.

### Files

| File | Change |
|---|---|
| `search.py` (new) | The service. One file, organised by `# ----` banners like `app.py`: config, crawler, extractors, embedder, store, indexer, searcher, HTTP API. |
| `app.py` | `/api/search/*` routes that proxy to the service; `/api/busy`; the "My files" step in `/api/chat`; start/stop of the service; a `search` kind in the model download code. |
| `index.html` | Search view, "My files" switch, Settings → Search. |
| `catalog.json` | New `search` list with the embedding model entry. |
| `search-requirements.txt` (new) | The service's Python dependencies, read by `setup.sh` and `app.py`. |
| `setup.sh` | A `--search` mode that installs those dependencies. Not part of `--install`. |
| `start.sh`, `stop.sh` | Ask the app to start the service when Search is enabled; stop it. |
| `macos/build.sh` | Add `search.py` and `search-requirements.txt` to the bundled file list; add `Info.plist` usage strings; run the new check. |
| `macos/check_search.py` (new) | Logic checks that need no model. |
| `macos/make_search_samples.py` (new) | Builds the local sample folder for the end-to-end check. |
| `README.md`, `CLAUDE.md` | Document the feature, the fourth process and port 8083. |

### Units inside `search.py`

Each has one job and a narrow interface so it can be checked alone.

| Unit | Does | Depends on |
|---|---|---|
| Config | Reads the `search` block of `data/settings.json`. | stdlib |
| Crawler | `walk(roots, rules) -> iterator of (path, size, mtime, kind)`. Applies skip rules. | stdlib |
| Extractors | `extract(path, kind) -> list of items`, each a text chunk, an image, or an audio segment, with a locator (page or seconds). | pypdf, pypdfium2, python-docx, openpyxl, Pillow, `afconvert` |
| Embedder | `embed_texts`, `embed_query`, `embed_images`, `embed_audio`, each returning unit-length 768-d vectors. Loads and unloads the model. | mlx-vlm, transformers |
| Store | SQLite reads and writes; holds the in-memory vector matrix. | sqlite3, numpy |
| Indexer | The background loop: diff the crawl against the store, extract, embed, write. Honours pause, busy and battery. | Crawler, Extractors, Embedder, Store |
| Searcher | `search(query, kind, folder, limit) -> results`. Ranking and cut-off. | Embedder, Store |
| HTTP API | FastAPI routes over the above. | fastapi |

Heavy imports (mlx, transformers, PIL, pypdf) happen inside functions, as `app.py` already does,
so the Crawler, Store and Searcher logic can be imported and checked without a model.

## Lifecycle

- Search is **off by default**. No download, no file reads, no service.
- **Turn on** (Settings → Search): `app.py` runs `setup.sh --search` if the dependencies are
  not installed yet, downloads the model through the existing download code, writes
  `search.enabled = true`, starts the service, and the first crawl begins. The UI shows which
  of these steps is running.
- `start.sh` calls `POST /api/search/start`, which starts the service if `search.enabled` is
  true. `stop.sh` always stops it.
- **Turn off**: stops the service and sets `search.enabled = false`. The index stays on disk.
- **Delete index**: stops the service, removes `data/search/`, and restarts it if enabled.
- **Rebuild**: delete index, then crawl from scratch.

### Launch

`app.py` is the only launcher. It runs, with the working directory set to `ROOT`:

```
uv run --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --port 8083
```

Both file names are relative on purpose. The Mac app's code lives under
`/Applications/Local Studio.app/…`, and a path with a space would defeat the `pkill` patterns.

`search-requirements.txt`:

```
mlx-vlm @ git+https://github.com/Blaizzy/mlx-vlm.git@3d87e88402f307efbf68e568971aa887ee7d9ed0
transformers>=5.19.0
torch
torchvision
numpy
pillow
pillow-heif
soundfile
librosa
fastapi
uvicorn
pypdf
pypdfium2
python-docx
openpyxl
```

`librosa` was present in the test environment; drop it during implementation if the audio path
runs without it. The pin moves to a released `mlx-vlm` version once one includes EmbeddingGemma 2.

`stop.sh` gains the `search` pid file and two patterns, one for the `uv` parent and one for the
Python child: `^[^ ]*uv run .*python search.py --port 8083` and
`^[^ ]*python[^ ]* search.py --port 8083`. Logs go to `logs/search.log`, pid to `logs/search.pid`.

### Sharing the machine

- Before each batch the Indexer calls `GET http://127.0.0.1:7860/api/busy`. If a chat reply is
  streaming or an image job is running, it waits one second and asks again. If the app does not
  answer, indexing continues.
- On battery power (`pmset -g batt` does not report "AC Power") indexing pauses, unless
  `search.on_battery` is true.
- A manual Pause sets `search.paused`; Resume clears it.
- When the index is up to date and no query has arrived for 10 minutes, the Embedder unloads
  the model. The next query or changed file reloads it.
- Searches are always served, including while indexing is paused.

## Settings

`data/settings.json` gains:

```json
"search": {
  "enabled": false,
  "folders": [],
  "removed": [],
  "paused": false,
  "on_battery": false
}
```

`folders` are user-added roots; `removed` are default roots or subfolders the user excluded.

## Crawl scope

**Default roots:** `~/Documents`, `~/Desktop`, `~/Downloads`, `~/Pictures`, `~/Movies`,
`~/Music`, `~/Library/Mobile Documents/com~apple~CloudDocs` (iCloud Drive).

**Always skipped:**

- Hidden files and folders (leading dot), and anything under `~/Library` except iCloud Drive.
- Packages and bundles, by folder suffix: `.app`, `.photoslibrary`, `.musiclibrary`,
  `.tvlibrary`, `.imovielibrary`, `.bundle`, `.framework`, `.xcodeproj`, `.xcworkspace`.
- Developer and build folders: `node_modules`, `.git`, `venv`, `.venv`, `__pycache__`, `build`,
  `dist`, `target`, `DerivedData`, `Pods`.
- The Hugging Face cache, and the app's own `STATE` folder.
- Likely secrets: `.env*`, `*.pem`, `*.p12`, `*.keychain*`, `id_rsa*`, `id_ed25519*`. `*.key` is
  not skipped because Keynote uses it; like every unrecognised type it is indexed by name only
  and its content is never read.
- iCloud placeholders not downloaded to the Mac (`st_flags & SF_DATALESS`), so indexing never
  triggers a download.
- Symbolic links.

## What is indexed

| Kind | Extensions | Handling | Limits |
|---|---|---|---|
| Document | `.pdf`, `.docx`, `.xlsx`, `.xlsm`, and the `TEXT_EXT` list from `app.py` | Extract text, pack paragraphs into chunks of up to 1,200 characters, split over-long paragraphs at sentence ends, no overlap. Embed as `title: {file name} \| text: {chunk}`. | 200 chunks per file. Text-like files over 5 MB and other documents over 100 MB: name only. |
| Scanned PDF | `.pdf` with little extractable text (same test as attachments) | Render pages with pypdfium2, downscale to 1024 px, embed as images. | First 20 pages. |
| Image | `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.bmp`, `.tif`, `.tiff`, `.heic` | Convert to RGB, downscale to 1024 px on the long side, one vector. Default vision detail. | Skip if either side is under 64 px. |
| Audio | `.mp3`, `.m4a`, `.wav`, `.aiff`, `.aif`, `.caf`, `.flac` | Convert to 16 kHz mono WAV with `afconvert`, split into 30-second segments, one vector each. | First 20 segments (10 minutes). |
| Name only | Video and every other type | No content vector. The file name and path go in the keyword index. | |

Order of work: documents first, then images, then audio, so text search is useful early.
Text is embedded 32 chunks per call; images and audio segments one per call.

Only the extensions in the table have their content read. There is no sniffing of unknown files.

**Text extraction** uses the same libraries and rules as `_extract` in `app.py`, reimplemented
in `search.py` because `_extract` is tied to uploads and HTTP errors. PDF chunks record their
page number; spreadsheets record the sheet name.

**Change detection:** every 5 minutes the Crawler walks the roots. A file is new or changed if
its path is unknown or its size or modified time differs. Changed files have their chunks
replaced; files no longer present are deleted from the store.

**Failures:** a file that raises during extraction or embedding is stored with `status='error'`
and the message, skipped, and retried only when its size or modified time changes. A folder the
service may not read is recorded as "No access" and the crawl continues.

## The index

`STATE/data/search/index.db`, SQLite in WAL mode.

```sql
CREATE TABLE files (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  kind TEXT NOT NULL,            -- document | image | audio | name
  size INTEGER, mtime REAL,
  status TEXT NOT NULL,          -- pending | ok | error
  error TEXT, indexed_at REAL
);
CREATE TABLE chunks (
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,
  kind TEXT NOT NULL,            -- text | image | audio
  text TEXT,                     -- chunk text; NULL for image and audio
  locator TEXT,                  -- "p3", "Sheet1", or "90s"
  vec BLOB NOT NULL              -- 768 float16, unit length (1,536 bytes)
);
CREATE VIRTUAL TABLE fts USING fts5(name, text, tokenize='unicode61');
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);   -- model repo, dims, schema version
```

Every file has one `fts` row for its name, keyed by the negative file id. Each text chunk has
one row keyed by its chunk id.

On start the Store loads every `vec` into one float32 matrix in memory (about 3 KB per chunk:
150,000 chunks is roughly 460 MB) and appends as chunks are written. On disk they stay float16. If `meta` records a
different model or dimension than the service uses, the index is rebuilt.

## Search and ranking

1. Embed the query as `task: search result | query: {q}`.
2. Vector scores: dot product against the whole matrix; take the top 200 chunks.
3. Keyword scores: FTS5 `bm25` over `name` and `text`; top 200. Query words shorter than three
   letters and common stopwords are dropped, the rest are quoted and all must match. If no words
   remain, the keyword step is skipped.
4. Fuse with reciprocal rank fusion (`k = 60`).
5. Group by file, keeping the best chunk per file as the displayed passage.
6. Cut-off. A file is kept if any of these holds:
   - it is the single best vector match (so a search always shows its closest file);
   - its best vector score is at least `max(top − 0.08, 0.62)`;
   - it has a keyword hit, for at most 10 files that fail the vector test.

   0.08 and 0.62 are starting values taken from the feasibility test, to tune on real files.
7. Return up to 30 files. The `kind` filter is applied before the cut-off, so "Images" ranks
   images against each other; the `folder` prefix filter is applied after.

Each result: `file_id`, `path`, `name`, `kind`, `score`, `snippet` (the chunk text, or empty for
image and audio), `locator`, `mtime`.

**Concurrency:** a search can arrive while a batch is being embedded. One lock guards every
model call, and one lock guards the database connection and the in-memory matrix.

## Service API (`127.0.0.1:8083`)

| Route | Purpose |
|---|---|
| `GET /health` | 200 once the service is up. |
| `GET /status` | `{state, model_loaded, files_total, files_done, files_error, chunks, index_bytes, current, paused_reason, no_access: [..]}`. `state` is `indexing`, `idle` or `paused`; `paused_reason` is `user`, `busy` or `battery`. |
| `POST /search` | `{query, kind?, folder?, limit?}` → `{results: [...]}`. |
| `POST /passages` | `{query, limit?, max_chars?}` → `{passages: [{file_id, name, path, locator, text}]}`: the top text chunks, ungrouped, for "My files" in chat. |
| `POST /reload` | Re-read settings (folders, pause, battery) and re-crawl. |
| `GET /errors` | Files with `status='error'` and their messages. |
| `GET /thumb/{file_id}` | A 320 px JPEG of an indexed image. |

## App API (`app.py`)

| Route | Purpose |
|---|---|
| `GET /api/busy` | `{busy: bool}`: true while any chat stream is open or an image job is running. |
| `GET /api/search/status` | `{enabled, installed, model_installed, running, setup: {status, step, error}, ...service status}`. |
| `POST /api/search/start` | Starts the service if Search is enabled and it is not running. Used by `start.sh`. |
| `POST /api/search` | Proxy to the service's `/search`. |
| `POST /api/search/enable`, `/disable` | Lifecycle as above. |
| `PUT /api/search/settings` | Update `folders`, `removed`, `paused`, `on_battery`; then `/reload`. |
| `POST /api/search/choose-folder` | Runs `osascript -e 'POSIX path of (choose folder)'` and returns the path. The UI also accepts a pasted path, in case the chooser cannot open. |
| `POST /api/search/rebuild`, `DELETE /api/search/index` | As above. |
| `GET /api/search/errors`, `GET /api/search/thumb/{file_id}` | Proxies. |
| `POST /api/search/open` | `{file_id, path, reveal}` → looks the path up in the index by id, requires it to equal `path`, and runs `open` or `open -R`. The request's path is only compared, never opened. |

`app.py` tracks open chat streams with a counter around `relay()` for `/api/busy`. The counter
is decremented in a `finally`, so a reply the user stops mid-stream does not leave indexing
paused.

**Model download:** `catalog.json` gains

```json
"search": [{"id": "embeddinggemma-2", "name": "EmbeddingGemma 2",
            "repo": "mlx-community/embeddinggemma-2-8bit", "size_gb": 1.3}]
```

`start_download` and `_download` get a `search` kind that never changes `chat_model` or
`image_model`. `_find_any`, the `/api/models` "other" list and `delete_model`'s shared-repo check
all treat the repo as known. `load_settings` fills in the `search` block's defaults.

## "My files" in chat

- `/api/chat` accepts `use_files: true`.
- `app.py` calls the service's `/passages` with the text of the latest user message and gets the
  top 6 text chunks, stopping at 12,000 characters in total.
- They are prepended to that user message before the context-size calculation:

  ```
  Answer using these excerpts from the user's files. Name the files you used.
  If they don't contain the answer, say so.

  [1] lease.pdf (p2)
  <chunk text>
  ...
  ```

- Before proxying the stream, `app.py` emits `data: {"x_sources": [{file_id, name, path, locator}]}`.
  The UI shows these as clickable chips under the reply and saves them in the chat JSON.
- If Search is off or the service is not running, the switch is disabled with a hint.
- Not in this version: sending matched images to vision models, and using earlier turns to
  build the query.

## Interface

**Sidebar:** a "Search" item between Images and Model library.

**Search view:** search box (searches on Enter and after a 300 ms pause in typing), kind filter
(All, Documents, Images, Audio), results list. Each row: name, folder path, snippet with
locator ("page 3", "1:30"), thumbnail for images, and Open and Reveal in Finder buttons. A
status line shows progress and the pause reason, with Pause and Resume. When Search is off the
view shows a short explanation and a "Turn on Search" button.

**Chat:** a "My files" pill next to the thinking pill; state remembered like the thinking switch.

**Settings → Search:** on/off switch; the folder list (default roots and added folders, each
with Remove, and "No access" where macOS denied it); Add folder; "Index on battery" switch;
counts (files indexed, index size, failed files with a link to the list); Rebuild; Delete index.

## macOS permissions

- Reading Desktop, Documents, Downloads and iCloud Drive triggers macOS prompts attributed to
  Local Studio.app (or Terminal when run from a checkout).
- `macos/build.sh` adds `NSDesktopFolderUsageDescription`, `NSDocumentsFolderUsageDescription`,
  `NSDownloadsFolderUsageDescription` and `NSFileProviderDomainUsageDescription` to `Info.plist`,
  each reading "Local Studio indexes your files on this Mac so you can search them. Nothing
  leaves your computer."
- A declined folder is reported in `no_access` and shown in Settings with a pointer to
  System Settings → Privacy & Security → Files and Folders.

## Setup and failure handling

- `setup.sh --search` resolves `search-requirements.txt` into uv's cache, imports `mlx_vlm`,
  `torchvision`, `soundfile` and `EmbeddingGemma2Processor`, and on success writes
  `data/search-deps.ok` containing the SHA-256 of the requirements file. It prints `@log` lines
  and ends with `@done` or `@error`.
- It is **not** part of `setup.sh --install` or `--check`. Nothing for Search is downloaded until
  the user turns it on, and existing installs get it the same way as new ones.
- `app.py` treats the dependencies as installed when that marker matches the current file.
- On start with the model present, the service embeds one test sentence and checks for a finite
  unit-length 768-d vector. On failure it stays up, reports the error in `/status`, and does not
  index.
- If the service is down, `/api/search*` returns 503 with a message the UI shows in the Search view.

## Testing

- `macos/check_search.py`, run by `macos/build.sh` with `uv run --no-project --with numpy`.
  With a stand-in embedder (deterministic vectors, no model) it checks:
  - chunking: size limit, paragraph packing, sentence splitting, 200-chunk cap;
  - skip rules: each rule in Crawl scope, against a temporary folder tree;
  - change detection: new, changed, unchanged and deleted files;
  - ranking: fusion order, per-file grouping, the relative cut-off, keyword-only hits, filters;
  - store: write, reload from disk, cascade delete, rebuild on model mismatch;
  - concurrency: searches run while another thread writes, without errors;
  - a query with no relevant file returns one result, not every file.
- `search.py --eval <folder>`: indexes a folder into a temporary index with the real model, runs
  the folder's `queries.json` (query → expected file) and exits non-zero if any expected file is
  not ranked first. `macos/make_search_samples.py` builds that folder in `build/search_samples/`
  on the local Mac: the 8 text files from the feasibility test, speech clips made with `say`, and
  photos converted from the system wallpapers, plus a PDF, a spreadsheet, a Word file and a scanned
  PDF. It is generated, never committed, so nothing Apple-owned is redistributed. It carries 17
  queries: 13 from the feasibility test and 4 for the added file types. The queries file sits
  beside the indexed folder, not in it.
- Development and `--eval` runs always pass explicit folders. Real user folders are indexed only
  in the manual check below.
- Manual check in the Mac app: turn Search on, grant the prompts, confirm progress, pause while
  chatting, search, Open and Reveal, "My files" in chat with sources, Delete index.

## Delivery

Two steps, one release:

1. **Find**: the service, index, Search view, Settings, setup and build changes.
2. **Ask**: `use_files` in `/api/chat`, sources in the UI.

Version 1.3.0: `APP_VERSION`, the README badge and DMG file name change together.

## Out of scope for 1.3.0

- Mail, Messages, Notes and Photos library (needs Full Disk Access and a reader per app).
- Video content, and audio beyond the first 10 minutes of a file.
- Sending matched images to vision models in chat.
- A file-system watcher; the 5-minute re-walk is enough.
- Lowering the vision detail setting for faster image indexing (measure first).
- An approximate-nearest-neighbour index; brute force is fast enough at this scale.

## Risks

| Risk | Mitigation |
|---|---|
| `mlx-vlm` is pinned to an unreleased revision. | Search is optional; setup verifies it; move to a release when available. |
| First index time is an estimate (1–2 hours). | Progress is visible, indexing is pausable, documents go first. |
| The 0.08 score margin may not suit real data. | It is one constant; tune it during the manual check on the user's files. |
| `torch` and `torchvision` add install size and time. | Installed only in the search environment, and only as an optional step. |
| Desktop has ~26,000 files of unknown type. | Skip rules and name-only handling bound the work; review the first crawl's counts. |

## Amendments (13 Oct 2026, after the follow-up probe and a code read)

| Was | Now | Why |
|---|---|---|
| Search dependencies installed by `setup.sh --install`, with a `@check` line. | `setup.sh --search`, run when the user turns Search on. | Existing installs never re-run `--install`; and "nothing is downloaded until Search is on". |
| `SEARCH_DEPS` duplicated in `setup.sh` and `start.sh`. | One `search-requirements.txt`; `app.py` is the only launcher. | One source of truth. |
| `stop.sh` pattern matched an absolute `search.py` path. | Relative launch from `ROOT`, two patterns. | The bundled path contains a space. |
| Images and audio embedded 4 per call. | One per call. | A list of images is embedded as one combined input. |
| Relative cut-off only. | Relative cut-off, a 0.62 floor, always the top match, capped keyword-only hits. | A weak top score would otherwise keep everything. |
| `*.key` skipped as a secret. | Indexed by name only. | Keynote files use `.key`. |
| Packages detected by macOS metadata. | Detected by folder suffix. | No cheap way to ask macOS from Python. |
| Committed sample folder, 16 queries. | Locally generated sample folder, 17 queries. | Licensing; three test queries depended on files that can't be regenerated; four added for PDF, spreadsheet, Word and scanned PDF. |
| Service state `idle` on start. | A `starting` state until the first pass begins. | An empty index read as "up to date". |
| `/api/search/open` took a file id. | It takes the id and the path, and both must match the index. | File ids are reused after a rebuild. |
| iCloud placeholders skipped. | Kept if already indexed; otherwise indexed by name. | Eviction would delete indexed files. |
| Folder rules applied globally. | A folder the user adds is indexed even inside a removed folder or under `~/Library`; overlapping folders are walked once. | Final review: overlapping folders broke every pass; added folders were silently ignored. |
| Any embedding error marked the file as failed. | A failing embedder stops the pass and leaves files pending (`problem` in `/status`); only file-specific failures are recorded. | Final review: a memory failure marked every file unreadable until a rebuild. |
| No request-origin checks. | `/api/search*`, `/api/chat` and the service refuse non-loopback hosts and foreign origins. | Final review: any web page could turn Search on or read snippets. |
