# Local Search, Part 1: the search service — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `search.py`, a standalone local service that crawls folders, embeds text, images and audio with EmbeddingGemma 2 on MLX, stores them in SQLite, and answers plain-language queries over HTTP.

**Architecture:** One Python file organised by `# ----` banners (config, chunking, crawler, store, ranking, extractors, embedder, indexer, HTTP API). Module level imports only the standard library and numpy; everything heavy is imported inside the function that uses it, so the logic is checked by a plain script with a stand-in embedder. The real model is exercised by `search.py --eval` on a locally generated sample folder.

**Tech Stack:** Python 3.12 run through `uv run`, numpy, SQLite (FTS5), FastAPI + uvicorn, mlx-vlm (pinned git revision), transformers ≥ 5.19, Pillow, pypdf, pypdfium2, python-docx, openpyxl, soundfile, macOS `afconvert`.

**Spec:** `docs/superpowers/specs/2026-10-13-local-search-design.md`. Part 2 (app integration, UI, chat) is `docs/superpowers/plans/2026-10-13-local-search-2-app.md` and depends on this part.

## Global Constraints

- Everything binds to `127.0.0.1` only. The service port is `8083`.
- User data paths resolve from `STATE` (`$LOCAL_STUDIO_HOME`, default the checkout), never `ROOT`. The index lives at `STATE/data/search/index.db`.
- `search.py` module level imports **only** the standard library and numpy. `import search` must work under `uv run --with numpy`.
- Never crawl the user's real folders during development. Every dev, check and eval run passes explicit folders (`LOCAL_STUDIO_SEARCH_ROOTS` or `--eval <folder>`). Real folders are indexed only in the manual Mac-app check in Part 2.
- Embedding model: `mlx-community/embeddinggemma-2-8bit`, 768 dimensions, unit-length vectors. Documents are embedded as `title: {file name} | text: {chunk}`, queries as `task: search result | query: {q}`.
- Images and audio segments are embedded **one per call**. A list of images is treated by the processor as one combined input.
- Limits, copied from the spec: chunk ≤ 1,200 characters; 200 chunks per file; 20 pages per scanned PDF; images downscaled to 1024 px, skipped under 64 px; audio 30-second segments, first 20; text-like files over 5 MB and other documents over 100 MB are name-only.
- Ranking constants: pool 200, RRF `k = 60`, margin 0.08, floor 0.62, at most 10 keyword-only files, 30 results.
- Match the surrounding code: 120-column lines, terse comments that explain why, `# ----` section banners as in `app.py`.
- Commit messages follow the repo's style: `search.py: <summary>` (no `feat:` prefixes), ending with the `Co-Authored-By` line the session provides. Work on the `local-search` branch.
- Run every `uv run` and every command that touches the model in the Terminal panel, not the sandboxed shell: uv writes to `~/.cache/uv` and MLX needs Metal.

## Review Focus

Inputs and conditions the spec implies that are most likely to bite a real user. Each has a test in the task named.

1. **A search arrives while a batch is being indexed.** It must return in about a second with no thread or SQLite error. → Task 4, check `concurrency`.
2. **A query that matches nothing in the index, or is all stopwords.** It must return at most one file, not every file. → Task 4, check `no_relevant_file`.
3. **A folder becomes unreadable (permission denied, drive unplugged).** Its files must stay in the index, not be deleted and re-embedded later. → Task 3, check `store_sync` (`keep_prefixes`).
4. **A file that cannot be read or decoded.** It is recorded as an error, the rest of the crawl continues, and it is not retried until it changes. → Task 5, check `indexer_errors`.
5. **A file is edited after being indexed.** Its old passages must stop matching. → Task 5, check `indexer_changes`.

## File Structure

| File | Responsibility |
|---|---|
| `search.py` | The whole service (created in Task 1, grown through Task 7). |
| `search-requirements.txt` | The service's Python dependencies. |
| `macos/check_search.py` | Model-free checks, run by `macos/build.sh`. |
| `macos/make_search_samples.py` | Builds `build/search_samples/` for `--eval`. Never committed output. |
| `macos/build.sh` | Runs the new check (bundling changes are in Part 2). |

Sections of `search.py`, in file order, and the names other sections rely on:

| Section | Public names |
|---|---|
| config | `ROOT`, `STATE`, `HOME`, `INDEX_DIR`, `MODEL_REPO`, `DIM`, `load_config(settings_path=None) -> dict` |
| chunking | `CHUNK_CHARS`, `MAX_CHUNKS`, `chunk_text(text, limit=CHUNK_CHARS) -> list[str]` |
| crawler | `classify(name, size) -> str`, `walk(roots, removed=(), no_access=None)` yielding `(path, size, mtime, kind)` |
| store | `class Store` |
| ranking | `keyword_tokens(query)`, `fuse(vec_hits, kw_rowids, chunk_file)`, `class Searcher` |
| extractors | `extract(path, kind) -> list[dict]` |
| embedder | `doc_text(name, chunk)`, `class Embedder` |
| indexer | `class Indexer` |
| HTTP API | `make_app(store, embedder, indexer, searcher)`, `run_eval(folder)`, `main()` |

---

### Task 1: Scaffold, configuration and chunking

**Files:**
- Create: `search.py`
- Create: `search-requirements.txt`
- Create: `macos/check_search.py`
- Modify: `macos/build.sh` (the "Checking UI script" block, lines 15-17)

**Interfaces:**
- Consumes: nothing.
- Produces: `search.load_config(settings_path=None) -> {"roots": list[str], "removed": list[str], "paused": bool, "on_battery": bool}`; `search.chunk_text(text, limit=1200) -> list[str]`; constants `ROOT, STATE, HOME, INDEX_DIR, MODEL_REPO, DIM, CHUNK_CHARS, MAX_CHUNKS`; helper `_under(path, base) -> bool`. The check harness: decorate a function with `@check` in `macos/check_search.py` and it runs.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b local-search
```

- [ ] **Step 2: Write the check harness with the first two checks**

Create `macos/check_search.py`:

```python
"""Build guard for search.py: checks the crawl, store and ranking logic with a stand-in embedder,
so it needs no model. Run by macos/build.sh, or by hand:

    uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py [check_name ...]
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import search  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def config():
    home = Path.home()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        p.write_text(json.dumps({"search": {"folders": ["/tmp/extra"], "removed": [str(home / "Music")], "paused": True}}))
        c = search.load_config(p)
        assert "/tmp/extra" in c["roots"] and str(home / "Music") not in c["roots"], c
        assert str(home / "Documents") in c["roots"]
        assert c["removed"] == [str(home / "Music")]
        assert c["paused"] is True and c["on_battery"] is False
        assert search.load_config(Path(d) / "missing.json")["roots"][0] == str(home / "Documents")
    os.environ["LOCAL_STUDIO_SEARCH_ROOTS"] = "/tmp/a:/tmp/b"
    try:
        assert search.load_config(Path("/nonexistent"))["roots"] == ["/tmp/a", "/tmp/b"]
    finally:
        del os.environ["LOCAL_STUDIO_SEARCH_ROOTS"]


@check
def chunking():
    c = search.chunk_text("a" * 500 + "\n\n" + "b" * 500 + "\n\n" + "c" * 500)
    assert [len(x) for x in c] == [1002, 500], [len(x) for x in c]     # two paragraphs packed, the third starts a new chunk
    long = " ".join(f"Sentence number {i} ends here." for i in range(200))   # one ~6,000-character paragraph
    c = search.chunk_text(long)
    assert len(c) > 3 and all(len(x) <= search.CHUNK_CHARS for x in c)
    assert all(x.endswith(".") for x in c), "an over-long paragraph is split at sentence ends"
    assert "\n" not in c[0], "sentences of one paragraph are rejoined with spaces"
    assert search.chunk_text("x" * 3000) == ["x" * 1200, "x" * 1200, "x" * 600]   # no sentence ends: hard split
    assert search.chunk_text("  \n\n \n") == []
    assert search._under("/a/b/c", "/a/b") and search._under("/a/b", "/a/b") and not search._under("/a/bc", "/a/b")


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   search checks OK")
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'search'`.

- [ ] **Step 4: Create `search-requirements.txt`**

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

- [ ] **Step 5: Create `search.py` with config and chunking**

```python
"""Local Studio search service: indexes the user's files with EmbeddingGemma 2 (MLX) and answers
plain-language queries. Runs as its own process on 127.0.0.1:8083, started by app.py.

Only the standard library and numpy are imported at module level. Everything heavy (mlx,
transformers, PIL, pypdf, fastapi) is imported inside the function that needs it, so the crawl,
store and ranking logic can be checked without a model (macos/check_search.py).
"""

import argparse
import fnmatch
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent                       # code (inside the .app when bundled)
STATE = Path(os.environ.get("LOCAL_STUDIO_HOME", ROOT))      # user data
HOME = Path.home()
SETTINGS = STATE / "data" / "settings.json"
INDEX_DIR = STATE / "data" / "search"
HF_HUB = Path(os.environ.get("HF_HUB_CACHE", HOME / ".cache/huggingface/hub"))
MODEL_REPO = "mlx-community/embeddinggemma-2-8bit"
DIM = 768
APP_URL = "http://127.0.0.1:7860"

# ---------------------------------------------------------------- config

ICLOUD = HOME / "Library/Mobile Documents/com~apple~CloudDocs"
DEFAULT_ROOTS = [HOME / d for d in ("Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music")] + [ICLOUD]


def _under(path, base):
    """True when `path` is `base` or inside it (string paths, no filesystem access)."""
    base = str(base).rstrip("/")
    return str(path) == base or str(path).startswith(base + "/")


def load_config(settings_path=None):
    """The `search` block of settings.json as the crawl needs it."""
    try:
        s = json.loads((settings_path or SETTINGS).read_text()).get("search") or {}
    except Exception:
        s = {}
    removed = [str(Path(p).expanduser()) for p in s.get("removed", [])]
    # Testing: LOCAL_STUDIO_SEARCH_ROOTS replaces the default folders, so a dev run never reads real ones.
    env = os.environ.get("LOCAL_STUDIO_SEARCH_ROOTS")
    defaults = env.split(":") if env else [str(r) for r in DEFAULT_ROOTS]
    roots = defaults + [str(Path(p).expanduser()) for p in s.get("folders", [])]
    roots = [r for r in dict.fromkeys(roots) if r not in removed]
    return {"roots": roots, "removed": removed, "paused": bool(s.get("paused")), "on_battery": bool(s.get("on_battery"))}


# ---------------------------------------------------------------- chunking

CHUNK_CHARS = 1200                # ~300 tokens
MAX_CHUNKS = 200                  # per file
_SENT = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text, limit=CHUNK_CHARS):
    """Pack paragraphs into chunks of at most `limit` characters. A paragraph longer than the limit
    is split at sentence ends, and a sentence longer than the limit is cut hard."""
    pieces = []                                           # (text, separator that joins it to the previous piece)
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            pieces.append((para, "\n\n"))
            continue
        sep = "\n\n"
        for sent in _SENT.split(para):
            while len(sent) > limit:
                pieces.append((sent[:limit], sep))
                sent, sep = sent[limit:], " "
            if sent:
                pieces.append((sent, sep))
                sep = " "
    chunks, cur = [], ""
    for piece, sep in pieces:
        if cur and len(cur) + len(sep) + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = cur + sep + piece if cur else piece
    if cur:
        chunks.append(cur)
    return chunks
```

- [ ] **Step 6: Run the checks to verify they pass**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py`
Expected:
```
   ok config
   ok chunking
   search checks OK
```

- [ ] **Step 7: Hook the check into the build**

In `macos/build.sh`, replace

```zsh
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$ROOT/app.py" && echo "   app.py OK"
```

with

```zsh
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$ROOT/app.py" && echo "   app.py OK"
UV=$(command -v uv || echo ~/.local/bin/uv)
$UV run -q --no-project --python 3.12 --with numpy python "$HERE/check_search.py"
```

and delete the later line `UV=$(command -v uv || echo ~/.local/bin/uv)` in the "==> Icon" block, which is now a duplicate.

- [ ] **Step 8: Verify the build script still parses**

Run: `zsh -n macos/build.sh && echo syntax-ok`
Expected: `syntax-ok`

- [ ] **Step 9: Commit**

```bash
git add search.py search-requirements.txt macos/check_search.py macos/build.sh
git commit -m "search.py: scaffold, configuration and text chunking"
```

---

### Task 2: Crawler

**Files:**
- Modify: `search.py` (add the crawler section after chunking)
- Modify: `macos/check_search.py` (add the `crawler` check before `if __name__`)

**Interfaces:**
- Consumes: `_under`, `STATE`, `HOME`, `HF_HUB`, `ICLOUD` from Task 1.
- Produces: `classify(name: str, size: int) -> "document" | "image" | "audio" | "name"`; `walk(roots, removed=(), no_access=None)` yielding `(path: str, size: int, mtime: float, kind: str)`, appending unreadable roots to the `no_access` list when one is passed; `_dataless(st) -> bool`; sets `TEXT_EXT`, `DOC_EXT`, `IMAGE_EXT`, `AUDIO_EXT`.

- [ ] **Step 1: Write the failing check**

Add to `macos/check_search.py`, above `if __name__ == "__main__":`:

```python
@check
def crawler():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve() / "root"
        files = ["notes.txt", "photo.jpg", "song.mp3", "movie.mp4", "talk.key", "sub/deep.md", ".hidden.txt",
                 ".git/config", "node_modules/x.js", "My.app/Contents/x.txt", "Pics.photoslibrary/db.txt",
                 "id_rsa", "cert.pem", "removed/a.txt"]
        for f in files:
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).write_text("x")
        with open(root / "big.log", "wb") as fh:
            fh.truncate(6 * 2 ** 20)                       # sparse 6 MB: text-like but over the 5 MB limit
        (root / "link.txt").symlink_to(root / "notes.txt")
        locked = root.parent / "locked"
        locked.mkdir()
        locked.chmod(0)
        no_access = []
        try:
            got = {p[len(str(root)) + 1:]: k for p, _, _, k in
                   search.walk([str(root), str(locked)], removed=[str(root / "removed")], no_access=no_access)}
        finally:
            locked.chmod(0o700)
        assert got == {"notes.txt": "document", "photo.jpg": "image", "song.mp3": "audio", "movie.mp4": "name",
                       "talk.key": "name", "sub/deep.md": "document", "big.log": "name"}, got
        assert no_access == [str(locked)], no_access
        row = next(search.walk([str(root)]))
        assert isinstance(row[1], int) and isinstance(row[2], float)
    assert search.classify("a.PDF", 10) == "document" and search.classify("a.pdf", 200 * 2 ** 20) == "name"
    assert search._dataless(types.SimpleNamespace(st_flags=0x40000000)) and not search._dataless(types.SimpleNamespace(st_flags=0))
    # the app's own data and anything under ~/Library (except iCloud Drive) are never walked
    assert list(search.walk([str(search.STATE / "data")])) == []
    assert list(search.walk([str(search.HOME / "Library" / "Preferences")])) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py crawler`
Expected: FAIL with `AttributeError: module 'search' has no attribute 'walk'`.

- [ ] **Step 3: Implement the crawler**

Append to `search.py`:

```python
# ---------------------------------------------------------------- crawler
# Only these extensions have their content read; everything else is indexed by name alone.

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
            ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java",
            ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".zsh", ".sql", ".toml",
            ".ini", ".cfg", ".log", ".r", ".m", ".scala", ".lua", ".pl", ".dart", ".vue", ".svelte"}   # same list as app.py
DOC_EXT = TEXT_EXT | {".pdf", ".docx", ".xlsx", ".xlsm"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aiff", ".aif", ".caf", ".flac"}
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "build", "dist", "target", "DerivedData", "Pods"}
PACKAGE_SUFFIXES = (".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary", ".bundle",
                    ".framework", ".xcodeproj", ".xcworkspace")
SECRET_GLOBS = ("*.pem", "*.p12", "*.keychain*", "id_rsa*", "id_ed25519*")   # .env* is hidden, so already skipped
MAX_TEXT_BYTES = 5 * 2 ** 20
MAX_DOC_BYTES = 100 * 2 ** 20
SF_DATALESS = 0x40000000          # an iCloud placeholder: reading it would download the file


def classify(name, size):
    ext = Path(name).suffix.lower()
    if ext in TEXT_EXT:
        return "document" if size <= MAX_TEXT_BYTES else "name"
    if ext in DOC_EXT:
        return "document" if size <= MAX_DOC_BYTES else "name"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    return "name"


def _dataless(st):
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def walk(roots, removed=(), no_access=None):
    """Yield (path, size, mtime, kind) for every file worth indexing under `roots`. Roots that
    can't be listed (macOS privacy, unplugged drive) are appended to `no_access`."""
    blocked = [str(STATE / d) for d in ("data", "uploads", "logs", "gallery")] + [str(HF_HUB)] + list(removed)
    library, icloud = str(HOME / "Library"), str(ICLOUD)

    def off_limits(path):
        return any(_under(path, b) for b in blocked) or (_under(path, library) and not _under(path, icloud))

    roots = [str(r) for r in roots]
    stack = [r for r in roots if not off_limits(r)]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            if no_access is not None and d in roots:
                no_access.append(d)
            continue
        for e in entries:
            try:
                if e.name.startswith(".") or e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if e.name not in SKIP_DIRS and not e.name.lower().endswith(PACKAGE_SUFFIXES) and not off_limits(e.path):
                        stack.append(e.path)
                    continue
                if any(fnmatch.fnmatch(e.name, g) for g in SECRET_GLOBS):
                    continue
                st = e.stat(follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode) or _dataless(st):
                    continue
                yield e.path, st.st_size, st.st_mtime, classify(e.name, st.st_size)
            except OSError:
                continue
```

- [ ] **Step 4: Run the checks to verify they pass**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py`
Expected: `ok config`, `ok chunking`, `ok crawler`, `search checks OK`.

- [ ] **Step 5: Commit**

```bash
git add search.py macos/check_search.py
git commit -m "search.py: folder crawler with skip rules"
```

---

### Task 3: Store

**Files:**
- Modify: `search.py` (add the store section after the crawler)
- Modify: `macos/check_search.py` (add `_unit`, `store_basics`, `store_sync`)

**Interfaces:**
- Consumes: `MODEL_REPO`, `DIM`, `_under`.
- Produces: `class Store(path: Path, model=MODEL_REPO, dim=DIM)` with:
  - `sync(found, keep_prefixes=()) -> {"new": int, "changed": int, "deleted": int}` where `found` is an iterable of `(path, size, mtime, kind)`
  - `pending() -> list[{"id", "path", "name", "kind"}]`, documents first, then images, audio, name-only
  - `replace_chunks(file_id, rows)` where `rows` is a list of `(kind, text | None, locator, vec)`; `kind` is `"text" | "image" | "audio"`, `vec` a float32 array of length `dim`
  - `mark_error(file_id, message)`
  - `vector_top(q, n, file_kind=None, text_only=False) -> list[(chunk_id, score)]`, best first
  - `keyword_top(tokens, n) -> list[int]` (fts rowids, best first; a negative rowid is `-file_id`, a name hit)
  - `chunk_info(ids) -> {chunk_id: {"file_id", "kind", "text", "locator"}}`
  - `file_info(ids) -> {file_id: {"path", "name", "kind", "mtime"}}`
  - `counts() -> {"files_total", "files_done", "files_error", "chunks", "index_bytes"}`
  - `errors() -> list[{"path", "error"}]`
  - `KIND_ORDER = {"document": 0, "image": 1, "audio": 2, "name": 3}`

- [ ] **Step 1: Confirm SQLite has FTS5 in uv's Python**

Run: `uv run -q --no-project --python 3.12 python -c "import sqlite3; c = sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE f USING fts5(a)'); print(sqlite3.sqlite_version, 'fts5 ok')"`
Expected: a version (3.50.x on this Mac) followed by `fts5 ok`. If it errors, stop and report: the design depends on FTS5.

- [ ] **Step 2: Write the failing checks**

Add to `macos/check_search.py`, above `if __name__ == "__main__":`:

```python
def _unit(seed):
    v = np.random.default_rng(seed).standard_normal(search.DIM).astype(np.float32)
    return v / np.linalg.norm(v)


@check
def store_basics():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "s" / "index.db"
        st = search.Store(db)
        assert st.sync([("/r/lease.txt", 10, 1.0, "document"), ("/r/pic.jpg", 20, 1.0, "image"),
                        ("/r/clip.mp4", 30, 1.0, "name")]) == {"new": 3, "changed": 0, "deleted": 0}
        pend = st.pending()
        assert [p["kind"] for p in pend] == ["document", "image", "name"] and pend[0]["name"] == "lease.txt"
        ids = {p["name"]: p["id"] for p in pend}
        st.replace_chunks(ids["lease.txt"], [("text", "rent is due on the first", "p1", _unit(1)),
                                             ("text", "pets need written consent", "p2", _unit(2))])
        st.replace_chunks(ids["pic.jpg"], [("image", None, "", _unit(3))])
        st.replace_chunks(ids["clip.mp4"], [])
        assert st.pending() == []
        c = st.counts()
        assert (c["files_total"], c["files_done"], c["files_error"], c["chunks"]) == (3, 3, 0, 3) and c["index_bytes"] > 0

        top = st.vector_top(_unit(2), 5)
        assert st.chunk_info([top[0][0]])[top[0][0]]["text"] == "pets need written consent" and top[0][1] > 0.99
        assert len(top) == 3 and top[0][1] > top[1][1]
        only_img = st.vector_top(_unit(2), 5, file_kind="image")
        assert len(only_img) == 1 and st.chunk_info([only_img[0][0]])[only_img[0][0]]["kind"] == "image"
        assert len(st.vector_top(_unit(2), 5, text_only=True)) == 2

        kw = st.keyword_top(["rent", "first"], 10)
        assert len(kw) == 1 and kw[0] > 0 and st.chunk_info(kw)[kw[0]]["locator"] == "p1"
        assert st.keyword_top(["rent", "pets"], 10) == [], "all words must match"
        assert st.keyword_top(["clip"], 10) == [-ids["clip.mp4"]], "name-only files are found by name"
        assert st.file_info([ids["pic.jpg"]])[ids["pic.jpg"]] == {"path": "/r/pic.jpg", "name": "pic.jpg", "kind": "image", "mtime": 1.0}

        st.mark_error(ids["pic.jpg"], "boom")
        assert st.errors() == [{"path": "/r/pic.jpg", "error": "boom"}] and st.counts()["files_error"] == 1
        assert st.vector_top(_unit(3), 5, file_kind="image") == [] and st.keyword_top(["pic"], 10) == [-ids["pic.jpg"]]

        st2 = search.Store(db)                               # reload from disk
        top2 = st2.vector_top(_unit(2), 5)
        assert top2[0][0] == top[0][0] and abs(top2[0][1] - top[0][1]) < 1e-3 and len(top2) == 2

        st3 = search.Store(db, model="some/other-model")     # a different model means the vectors are useless
        assert st3.counts()["files_total"] == 0 and st3.vector_top(_unit(2), 5) == []

        big = search.Store(Path(d) / "big.db")
        big.sync([("/r/big.txt", 1, 1.0, "document")])
        big.replace_chunks(big.pending()[0]["id"], [("text", f"chunk {i}", "", _unit(i)) for i in range(3000)])
        assert big.counts()["chunks"] == 3000 and big.vector_top(_unit(2999), 1)[0][1] > 0.99   # grew past the initial capacity


@check
def store_sync():
    with tempfile.TemporaryDirectory() as d:
        st = search.Store(Path(d) / "index.db")
        st.sync([("/r/a.txt", 10, 1.0, "document"), ("/x/b.txt", 10, 1.0, "document")])
        a, b = (p["id"] for p in st.pending())
        st.replace_chunks(a, [("text", "alpha", "", _unit(1))])
        st.replace_chunks(b, [("text", "beta", "", _unit(2))])

        assert st.sync([("/r/a.txt", 10, 1.0, "document"), ("/x/b.txt", 10, 1.0, "document")]) == {"new": 0, "changed": 0, "deleted": 0}
        assert st.pending() == []

        r = st.sync([("/r/a.txt", 11, 2.0, "document"), ("/x/b.txt", 10, 1.0, "document")])     # a.txt edited
        assert r == {"new": 0, "changed": 1, "deleted": 0} and [p["id"] for p in st.pending()] == [a]
        st.replace_chunks(a, [("text", "gamma", "", _unit(3))])
        assert st.vector_top(_unit(1), 5)[0][1] < 0.5, "the old passage no longer matches"
        assert st.keyword_top(["alpha"], 5) == [] and len(st.keyword_top(["gamma"], 5)) == 1

        # /x could not be listed this time: its files are kept, not treated as deleted
        assert st.sync([("/r/a.txt", 11, 2.0, "document")], keep_prefixes=["/x"])["deleted"] == 0
        assert st.counts()["files_total"] == 2

        assert st.sync([("/r/a.txt", 11, 2.0, "document")])["deleted"] == 1                      # b.txt really is gone
        assert st.counts() == {**st.counts(), "files_total": 1, "chunks": 1}
        assert st.keyword_top(["beta"], 5) == [] and st.vector_top(_unit(2), 5)[0][1] < 0.5
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py store_basics store_sync`
Expected: FAIL with `AttributeError: module 'search' has no attribute 'Store'`.

- [ ] **Step 4: Implement the store**

Append to `search.py`:

```python
# ---------------------------------------------------------------- store
# SQLite holds files, chunks, a keyword index and each chunk's vector (float16 on disk). The vectors
# are also kept in one float32 matrix in memory, so a query is a single matrix product.

KIND_ORDER = {"document": 0, "image": 1, "audio": 2, "name": 3}
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
  size INTEGER, mtime REAL, status TEXT NOT NULL, error TEXT, indexed_at REAL);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT, locator TEXT, vec BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path, model=MODEL_REPO, dim=DIM):
        self.path, self.dim = Path(path), dim
        self.lock = threading.RLock()                     # guards the connection and the matrix
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        want = f"{model}:{dim}"
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
        if row and row[0] != want:                        # vectors from another model can't be compared
            self.db.executescript("DROP TABLE chunks; DROP TABLE files; DROP TABLE fts; DROP TABLE meta;" + SCHEMA)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (want,))
        self._load()

    # -- in-memory matrix
    def _load(self):
        rows = self.db.execute("SELECT c.id, c.file_id, c.kind, f.kind, c.vec FROM chunks c "
                               "JOIN files f ON f.id = c.file_id ORDER BY c.id").fetchall()
        self.n, self.cap = 0, max(1024, len(rows) * 2)
        self.M = np.zeros((self.cap, self.dim), np.float32)
        self.ids = np.zeros(self.cap, np.int64)
        self.fids = np.zeros(self.cap, np.int64)
        self.fkind = np.zeros(self.cap, np.int8)
        self.is_text = np.zeros(self.cap, bool)
        self.alive = np.zeros(self.cap, bool)
        for cid, fid, ckind, fkind, blob in rows:
            self._append(cid, fid, np.frombuffer(blob, np.float16), fkind, ckind == "text")

    def _append(self, cid, fid, vec, fkind, is_text):
        if self.n == self.cap:
            self.cap *= 2
            for name in ("M", "ids", "fids", "fkind", "is_text", "alive"):
                old = getattr(self, name)
                new = np.zeros((self.cap,) + old.shape[1:], old.dtype)
                new[:self.n] = old[:self.n]
                setattr(self, name, new)
        i = self.n
        self.M[i], self.ids[i], self.fids[i] = vec, cid, fid
        self.fkind[i], self.is_text[i], self.alive[i] = KIND_ORDER[fkind], is_text, True
        self.n += 1

    # -- writes
    def _clear_chunks(self, fid):
        self.db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE file_id=?)", (fid,))
        self.db.execute("DELETE FROM fts WHERE rowid=?", (-fid,))
        self.db.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
        self.alive[:self.n][self.fids[:self.n] == fid] = False

    def _name_row(self, fid):
        path, name = self.db.execute("SELECT path, name FROM files WHERE id=?", (fid,)).fetchone()
        self.db.execute("INSERT INTO fts(rowid, name, text) VALUES (?, ?, '')", (-fid, f"{name} {Path(path).parent.name}"))
        return name

    def sync(self, found, keep_prefixes=()):
        """Bring the files table in line with a crawl. Files under `keep_prefixes` (folders that
        could not be listed this time) are kept even though the crawl did not see them."""
        with self.lock, self.db:
            known = {p: (i, s, m) for i, p, s, m in self.db.execute("SELECT id, path, size, mtime FROM files")}
            seen, new, changed = set(), 0, 0
            for path, size, mtime, kind in found:
                seen.add(path)
                k = known.get(path)
                if k is None:
                    self.db.execute("INSERT INTO files(path, name, kind, size, mtime, status) VALUES (?,?,?,?,?,'pending')",
                                    (path, Path(path).name, kind, size, mtime))
                    new += 1
                elif (k[1], k[2]) != (size, mtime):
                    self.db.execute("UPDATE files SET kind=?, size=?, mtime=?, status='pending', error=NULL WHERE id=?",
                                    (kind, size, mtime, k[0]))
                    changed += 1
            gone = [k[0] for p, k in known.items() if p not in seen and not any(_under(p, kp) for kp in keep_prefixes)]
            for fid in gone:
                self._clear_chunks(fid)
                self.db.execute("DELETE FROM files WHERE id=?", (fid,))
            return {"new": new, "changed": changed, "deleted": len(gone)}

    def replace_chunks(self, file_id, rows):
        with self.lock, self.db:
            row = self.db.execute("SELECT kind FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None:                               # deleted by a crawl while it was being embedded
                return
            self._clear_chunks(file_id)
            self._name_row(file_id)
            for ord_, (kind, text, locator, vec) in enumerate(rows):
                vec = np.asarray(vec, np.float32)
                cid = self.db.execute("INSERT INTO chunks(file_id, ord, kind, text, locator, vec) VALUES (?,?,?,?,?,?)",
                                      (file_id, ord_, kind, text, locator, vec.astype(np.float16).tobytes())).lastrowid
                if text:
                    self.db.execute("INSERT INTO fts(rowid, name, text) VALUES (?, '', ?)", (cid, text))
                self._append(cid, file_id, vec, row[0], kind == "text")
            self.db.execute("UPDATE files SET status='ok', error=NULL, indexed_at=? WHERE id=?", (time.time(), file_id))

    def mark_error(self, file_id, message):
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone() is None:
                return
            self._clear_chunks(file_id)
            self._name_row(file_id)                       # still findable by name
            self.db.execute("UPDATE files SET status='error', error=?, indexed_at=? WHERE id=?", (message, time.time(), file_id))

    # -- reads
    def pending(self):
        with self.lock:
            rows = self.db.execute("SELECT id, path, name, kind FROM files WHERE status='pending'").fetchall()
        rows.sort(key=lambda r: (KIND_ORDER[r[3]], r[1]))
        return [{"id": i, "path": p, "name": n, "kind": k} for i, p, n, k in rows]

    def vector_top(self, q, n, file_kind=None, text_only=False):
        with self.lock:
            if not self.n:
                return []
            scores = self.M[:self.n] @ np.asarray(q, np.float32)
            ok = self.alive[:self.n].copy()
            if file_kind:
                ok &= self.fkind[:self.n] == KIND_ORDER[file_kind]
            if text_only:
                ok &= self.is_text[:self.n]
            scores[~ok] = -2.0
            k = min(n, self.n)
            top = np.argpartition(-scores, k - 1)[:k]
            top = top[np.argsort(-scores[top])]
            return [(int(self.ids[i]), float(scores[i])) for i in top if scores[i] > -2.0]

    def keyword_top(self, tokens, n):
        if not tokens:
            return []
        match = " ".join('"%s"' % t.replace('"', "") for t in tokens)      # quoted terms, all must match
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?", (match, n))]

    def _in(self, sql, ids):
        ids = list(ids)
        with self.lock:
            return self.db.execute(sql % ",".join("?" * len(ids)), ids).fetchall() if ids else []

    def chunk_info(self, ids):
        return {i: {"file_id": f, "kind": k, "text": t, "locator": loc}
                for i, f, k, t, loc in self._in("SELECT id, file_id, kind, text, locator FROM chunks WHERE id IN (%s)", ids)}

    def file_info(self, ids):
        return {i: {"path": p, "name": n, "kind": k, "mtime": m}
                for i, p, n, k, m in self._in("SELECT id, path, name, kind, mtime FROM files WHERE id IN (%s)", ids)}

    def counts(self):
        with self.lock:
            by = dict(self.db.execute("SELECT status, COUNT(*) FROM files GROUP BY status"))
            chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        size = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*"))     # db + WAL files
        return {"files_total": sum(by.values()), "files_done": by.get("ok", 0), "files_error": by.get("error", 0),
                "chunks": chunks, "index_bytes": size}

    def errors(self):
        with self.lock:
            return [{"path": p, "error": e} for p, e in
                    self.db.execute("SELECT path, error FROM files WHERE status='error' ORDER BY path LIMIT 500")]
```

- [ ] **Step 5: Run the checks to verify they pass**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py`
Expected: five `ok` lines ending with `ok store_basics`, `ok store_sync`, then `search checks OK`.

- [ ] **Step 6: Commit**

```bash
git add search.py macos/check_search.py
git commit -m "search.py: SQLite store with keyword index and in-memory vectors"
```

---

### Task 4: Ranking and the Searcher

**Files:**
- Modify: `search.py` (add the ranking section after the store)
- Modify: `macos/check_search.py` (add `FakeEmbedder`, `_library`, `ranking`, `searcher`, `no_relevant_file`, `concurrency`)

**Interfaces:**
- Consumes: `Store.vector_top`, `Store.keyword_top`, `Store.chunk_info`, `Store.file_info`, `_under`.
- Produces:
  - `keyword_tokens(query) -> list[str]`
  - `fuse(vec_hits, kw_rowids, chunk_file) -> list[{"file_id", "fused", "chunk", "vec", "kw"}]`, best first. `chunk` is the best chunk id or `None`; `vec` the file's best vector score or `None`; `chunk_file` maps chunk id to file id.
  - `class Searcher(store, embedder)` with `search(query, kind=None, folder=None, limit=30) -> list[{"file_id", "path", "name", "kind", "score", "snippet", "locator", "mtime"}]` and `passages(query, limit=6, max_chars=12000) -> list[{"file_id", "name", "path", "locator", "text"}]`
  - The embedder interface every later task uses: `embed_texts(list[str]) -> ndarray (n, DIM)`, `embed_query(str) -> ndarray (DIM,)`, `embed_image(img) -> ndarray (DIM,)`, `embed_audio(samples) -> ndarray (DIM,)`, attributes `loaded: bool`, `last_used: float`, method `unload()`.
  - `doc_text(name, chunk) -> str` (defined here because the checks need it; the embedder section reuses it)

- [ ] **Step 1: Write the failing checks**

Add to `macos/check_search.py`, above `if __name__ == "__main__":`:

```python
class FakeEmbedder:
    """Bag-of-words vectors: each word maps to a fixed random direction, so texts that share
    words score high. Images and audio are embedded from a label string."""
    loaded, last_used = True, 0.0
    IGNORE = search.STOPWORDS | {"title", "text", "none", "txt", "task", "query"}

    def _vec(self, text):
        v = np.zeros(search.DIM, np.float32)
        for w in re.findall(r"[a-z0-9]+", str(text).lower()):
            if len(w) >= 3 and w not in self.IGNORE:
                seed = int(hashlib.md5(w.encode()).hexdigest()[:8], 16)
                v += np.random.default_rng(seed).standard_normal(search.DIM).astype(np.float32)
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_texts(self, texts):
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, q):
        return self._vec(q)

    embed_image = embed_audio = embed_query

    def unload(self):
        pass


def _library(d):
    """A small indexed store: three documents, one image, one name-only file."""
    st, emb = search.Store(Path(d) / "index.db"), FakeEmbedder()
    docs = {"/r/docs/plumber.txt": ["plumber invoice kitchen sink valve", "payment bank transfer november"],
            "/r/docs/lease.txt": ["tenancy agreement harbour street rent bond"],
            "/r/trips/tokyo.txt": ["japan itinerary haneda shinjuku kyoto cherry blossoms"]}
    st.sync([(p, 1, 1.0, "document") for p in docs] + [("/r/pics/IMG_0001.jpg", 1, 1.0, "image"), ("/r/holiday.mp4", 1, 1.0, "name")])
    for f in st.pending():
        if f["kind"] == "document":
            st.replace_chunks(f["id"], [("text", c, f"p{i + 1}", v) for i, (c, v) in enumerate(
                zip(docs[f["path"]], emb.embed_texts([search.doc_text(f["name"], c) for c in docs[f["path"]]])))])
        elif f["kind"] == "image":
            st.replace_chunks(f["id"], [("image", None, "", emb.embed_image("desert plant night succulents"))])
        else:
            st.replace_chunks(f["id"], [])
    return st, search.Searcher(st, emb)


@check
def ranking():
    assert search.keyword_tokens("What did I say about sales in Europe?") == ["say", "sales", "europe"]
    assert search.keyword_tokens("the and for of to") == []
    chunk_file = {1: 10, 2: 10, 3: 20, 4: 30, 5: 40}
    files = lambda r: [x["file_id"] for x in r]                                                  # noqa: E731

    r = search.fuse([(1, 0.80), (3, 0.75), (2, 0.70), (4, 0.66), (5, 0.55)], [], chunk_file)
    assert files(r) == [10, 20], files(r)                  # 0.66 is under top - 0.08; 0.55 is under the floor too
    assert r[0]["chunk"] == 1 and r[0]["vec"] == 0.80 and r[0]["kw"] is False

    assert files(search.fuse([(1, 0.58), (3, 0.57), (4, 0.56)], [], chunk_file)) == [10], "a weak top match still shows, alone"
    assert files(search.fuse([(1, 0.70), (3, 0.63), (4, 0.61)], [], chunk_file)) == [10, 20], "the floor applies even inside the margin"

    r = search.fuse([(1, 0.80)], [4, -40], chunk_file)     # keyword-only files are kept after the vector match
    assert files(r) == [10, 30, 40] and r[1]["kw"] and r[1]["chunk"] == 4 and r[2]["chunk"] is None and r[2]["vec"] is None

    assert files(search.fuse([(1, 0.80), (3, 0.79)], [3], chunk_file)) == [20, 10], "matching both ways outranks a slightly better vector"
    assert files(search.fuse([(99, 0.90), (1, 0.80)], [98], chunk_file)) == [10], "ids deleted mid-search are ignored"
    assert len(search.fuse([(1, 0.80)], [-i for i in range(100, 130)], chunk_file)) == 11, "at most 10 keyword-only files"
    assert search.fuse([], [], chunk_file) == []


@check
def searcher():
    with tempfile.TemporaryDirectory() as d:
        st, s = _library(d)
        r = s.search("how much is the plumber invoice")
        assert r[0]["name"] == "plumber.txt" and r[0]["snippet"].startswith("plumber invoice") and r[0]["locator"] == "p1"
        assert set(r[0]) == {"file_id", "path", "name", "kind", "score", "snippet", "locator", "mtime"}
        assert s.search("japan cherry blossoms itinerary")[0]["name"] == "tokyo.txt"
        img = s.search("desert plant night", kind="image")
        assert [x["name"] for x in img] == ["IMG_0001.jpg"] and img[0]["snippet"] == ""
        assert [x["name"] for x in s.search("holiday")] [-1] == "holiday.mp4", "a name-only file is found by its name"
        assert all(x["path"].startswith("/r/trips/") for x in s.search("japan itinerary", folder="/r/trips"))
        assert s.search("plumber invoice", folder="/r/trips") == []
        assert s.search("   ") == []
        assert len(s.search("plumber invoice kitchen", limit=1)) == 1

        p = s.passages("plumber invoice kitchen sink")
        assert p[0]["text"] == "plumber invoice kitchen sink valve" and p[0]["name"] == "plumber.txt" and p[0]["locator"] == "p1"
        assert set(p[0]) == {"file_id", "name", "path", "locator", "text"}
        assert s.passages("completely unrelated zebra astronomy") == [], "no passages when nothing clears the floor"
        assert len(s.passages("plumber invoice kitchen sink valve payment bank transfer november", max_chars=40)) == 1


@check
def no_relevant_file():
    with tempfile.TemporaryDirectory() as d:
        _, s = _library(d)
        assert len(s.search("zebra astronomy quantum")) <= 1, "a query that matches nothing must not list every file"
        assert len(s.search("the and for of to")) <= 1, "an all-stopword query must not list every file"


@check
def concurrency():
    with tempfile.TemporaryDirectory() as d:
        st, s = _library(d)
        fid = st.keyword_top(["tokyo"], 1)[0] * -1
        emb, stop, errors = FakeEmbedder(), threading.Event(), []

        def writer():
            i = 0
            while not stop.is_set():
                try:
                    st.replace_chunks(fid, [("text", f"japan kyoto revision {i}", "p1", emb.embed_query(f"japan kyoto revision {i}"))] * 20)
                    st.sync([(p, 1, 1.0, "document") for p in ("/r/docs/plumber.txt", "/r/docs/lease.txt", "/r/trips/tokyo.txt")]
                            + [("/r/pics/IMG_0001.jpg", 1, 1.0, "image"), ("/r/holiday.mp4", 1, 1.0, "name")])
                except Exception as e:                     # noqa: BLE001
                    errors.append(e)
                i += 1

        t = threading.Thread(target=writer)
        t.start()
        try:
            slowest = 0.0
            for _ in range(100):
                t0 = time.time()
                assert s.search("plumber invoice kitchen")[0]["name"] == "plumber.txt"
                s.passages("japan kyoto")
                slowest = max(slowest, time.time() - t0)
        finally:
            stop.set()
            t.join()
        assert not errors, errors[:1]
        assert slowest < 2.0, f"a search took {slowest:.2f}s while indexing"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py ranking`
Expected: FAIL with `AttributeError: module 'search' has no attribute 'STOPWORDS'` (raised while the `FakeEmbedder` class body is evaluated).

- [ ] **Step 3: Implement ranking**

Append to `search.py`:

```python
# ---------------------------------------------------------------- ranking
# Meaning (vectors) and exact words (FTS5) are fused by rank. Vector scores sit in a narrow band
# (related ~0.65-0.8, unrelated ~0.6), so the cut-off is relative to the best match, with a floor.

STOPWORDS = frozenset("the and for are was were with that this from have has had you your what when where which "
                      "who how why did does about into out not but can all any our its".split())
POOL = 200                        # chunks taken from each ranking before fusing
RRF_K = 60
MARGIN = 0.08                     # keep files within this of the best vector score...
FLOOR = 0.62                      # ...but never below this (both from the feasibility test; tune on real files)
KEYWORD_ONLY_MAX = 10
RESULT_LIMIT = 30


def doc_text(name, chunk):
    return f"title: {name} | text: {chunk}"


def keyword_tokens(query):
    return [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 3 and t not in STOPWORDS][:12]


def fuse(vec_hits, kw_rowids, chunk_file):
    """Combine a vector ranking [(chunk_id, score)] and a keyword ranking [rowid] (negative =
    a hit on file -rowid's name) into one per-file ranking, applying the cut-off."""
    vec_hits = [(c, s) for c, s in vec_hits if c in chunk_file]
    kw_rowids = [r for r in kw_rowids if r < 0 or r in chunk_file]
    chunk_score, files = {}, {}

    def slot(fid):
        return files.setdefault(fid, {"file_id": fid, "fused": 0.0, "chunk": None, "vec": None, "kw": False})

    for rank, (cid, score) in enumerate(vec_hits):
        chunk_score[cid] = 1.0 / (RRF_K + rank + 1)
        f = slot(chunk_file[cid])
        f["vec"] = score if f["vec"] is None else max(f["vec"], score)
    name_bonus = {}
    for rank, rowid in enumerate(kw_rowids):
        r = 1.0 / (RRF_K + rank + 1)
        if rowid < 0:
            name_bonus[-rowid] = r
            slot(-rowid)["kw"] = True
        else:
            chunk_score[rowid] = chunk_score.get(rowid, 0.0) + r
            slot(chunk_file[rowid])["kw"] = True
    for cid, score in chunk_score.items():
        f = files[chunk_file[cid]]
        if score > f["fused"]:
            f["fused"], f["chunk"] = score, cid
    for fid, bonus in name_bonus.items():
        files[fid]["fused"] += bonus

    top = vec_hits[0] if vec_hits else None
    threshold = max(top[1] - MARGIN, FLOOR) if top else None
    order = lambda f: (f["fused"], f["vec"] if f["vec"] is not None else -1.0)                    # noqa: E731
    strong = [f for f in files.values()
              if top and (f["file_id"] == chunk_file[top[0]] or (f["vec"] is not None and f["vec"] >= threshold))]
    kept = {f["file_id"] for f in strong}
    kw_only = sorted((f for f in files.values() if f["kw"] and f["file_id"] not in kept), key=order, reverse=True)
    return sorted(strong + kw_only[:KEYWORD_ONLY_MAX], key=order, reverse=True)


class Searcher:
    def __init__(self, store, embedder):
        self.store, self.embedder = store, embedder

    def search(self, query, kind=None, folder=None, limit=RESULT_LIMIT):
        query = (query or "").strip()
        if not query:
            return []
        vec = self.store.vector_top(self.embedder.embed_query(query), POOL, file_kind=kind)
        kw = self.store.keyword_top(keyword_tokens(query), POOL)
        chunks = self.store.chunk_info([c for c, _ in vec] + [r for r in kw if r > 0])
        chunk_file = {c: i["file_id"] for c, i in chunks.items()}
        files = self.store.file_info(set(chunk_file.values()) | {-r for r in kw if r < 0})
        if kind:                                          # the vector side is already filtered; filter the keyword side
            kw = [r for r in kw if files.get(-r if r < 0 else chunk_file.get(r), {}).get("kind") == kind]
        out = []
        for r in fuse(vec, kw, chunk_file):
            f = files.get(r["file_id"])
            if f is None or (folder and not _under(f["path"], folder)):
                continue
            c = chunks.get(r["chunk"]) or {}
            out.append({"file_id": r["file_id"], "path": f["path"], "name": f["name"], "kind": f["kind"],
                        "score": round(r["vec"], 3) if r["vec"] is not None else None,
                        "snippet": c.get("text") or "", "locator": c.get("locator") or "", "mtime": f["mtime"]})
            if len(out) == limit:
                break
        return out

    def passages(self, query, limit=6, max_chars=12000):
        """The best text chunks for a question, ungrouped, for "My files" in chat."""
        query = (query or "").strip()
        if not query:
            return []
        vec = self.store.vector_top(self.embedder.embed_query(query), POOL, text_only=True)
        if not vec:
            return []
        threshold = max(vec[0][1] - MARGIN, FLOOR)
        hits = [c for c, s in vec if s >= threshold][:limit]
        chunks = self.store.chunk_info(hits)
        files = self.store.file_info({c["file_id"] for c in chunks.values()})
        out, used = [], 0
        for cid in hits:
            c = chunks.get(cid)
            f = files.get(c["file_id"]) if c else None
            if not f or not c["text"]:
                continue
            if out and used + len(c["text"]) > max_chars:
                break
            used += len(c["text"])
            out.append({"file_id": c["file_id"], "name": f["name"], "path": f["path"], "locator": c["locator"] or "", "text": c["text"]})
        return out
```

- [ ] **Step 4: Run the checks to verify they pass**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py`
Expected: nine `ok` lines, the last four being `ok ranking`, `ok searcher`, `ok no_relevant_file`, `ok concurrency`, then `search checks OK`.

- [ ] **Step 5: Commit**

```bash
git add search.py macos/check_search.py
git commit -m "search.py: hybrid ranking with a relative cut-off"
```

---

### Task 5: Indexer loop and text extraction

**Files:**
- Modify: `search.py` (add the extractors section with the text path only, then the indexer section)
- Modify: `macos/check_search.py` (add `_tree`, `indexer_changes`, `indexer_errors`, `indexer_holds`)

**Interfaces:**
- Consumes: `walk`, `Store`, `chunk_text`, `MAX_CHUNKS`, `doc_text`, the embedder interface from Task 4, `load_config`.
- Produces:
  - `extract(path, kind) -> list[dict]`. Each item is `{"kind": "text", "text": str, "locator": str}`, `{"kind": "image", "image": PIL.Image, "locator": str}` or `{"kind": "audio", "audio": ndarray, "locator": str}`. This task implements plain-text files; Task 6 adds the rest inside the same function.
  - `class Indexer(store, embedder, config=load_config, app_url=APP_URL)` with `run_once()`, `run()` (blocking loop), `status() -> dict`, attributes `state` (`"starting" | "indexing" | "paused" | "idle"`; `"starting"` until the first pass begins), `paused_reason` (`None | "user" | "battery" | "busy"`), `current`, `no_access`, `error`, events `wake` and `stop`, and overridable methods `on_ac() -> bool`, `app_busy() -> bool`.
  - Constants `TEXT_BATCH = 32`, `RESCAN_SECONDS = 300`, `IDLE_UNLOAD_SECONDS = 600`.

- [ ] **Step 1: Write the failing checks**

Add to `macos/check_search.py`, above `if __name__ == "__main__":`:

```python
def _tree(d, files):
    root = Path(d).resolve() / "docs"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    st = search.Store(Path(d) / "index.db")
    cfg = {"roots": [str(root)], "removed": [], "paused": False, "on_battery": True}
    ix = search.Indexer(st, FakeEmbedder(), config=lambda: cfg, app_url=None)
    return root, st, ix, cfg


@check
def indexer_changes():
    with tempfile.TemporaryDirectory() as d:
        root, st, ix, _ = _tree(d, {"plumber.txt": "plumber invoice kitchen sink valve", "sub/tokyo.md": "japan itinerary kyoto cherry blossoms",
                                    "holiday.mp4": "not text", "long.txt": "\n\n".join(f"paragraph {i} " + "word " * 230 for i in range(300))})
        s = search.Searcher(st, ix.embedder)
        assert ix.status()["state"] == "starting" and ix.status()["roots"] == [str(root)]
        ix.run_once()
        c = st.counts()
        assert (c["files_total"], c["files_done"], c["files_error"]) == (4, 4, 0), c
        assert c["chunks"] == 2 + search.MAX_CHUNKS, "a huge file is capped at MAX_CHUNKS"
        assert s.search("plumber invoice")[0]["name"] == "plumber.txt" and ix.state == "indexing" and ix.current is None
        assert ix.status()["files_done"] == 4 and ix.status()["no_access"] == []

        (root / "plumber.txt").write_text("electrician quote garage wiring")           # edited
        os.utime(root / "plumber.txt", (time.time() + 5, time.time() + 5))
        (root / "sub/tokyo.md").unlink()                                               # deleted
        (root / "new.txt").write_text("dentist appointment thursday")                  # added
        ix.run_once()
        assert st.counts()["files_total"] == 4
        assert s.search("electrician garage wiring")[0]["name"] == "plumber.txt"
        assert all(r["name"] != "plumber.txt" for r in s.search("plumber invoice kitchen sink valve") if r["score"] and r["score"] > 0.62), \
            "the old passage must stop matching"
        assert all(r["name"] != "tokyo.md" for r in s.search("japan itinerary kyoto"))
        assert s.search("dentist appointment")[0]["name"] == "new.txt"


@check
def indexer_errors():
    with tempfile.TemporaryDirectory() as d:
        root, st, ix, _ = _tree(d, {"good.txt": "plumber invoice", "bad.txt": "broken", "also.txt": "japan itinerary"})
        real, calls = search.extract, []

        def flaky(path, kind):
            calls.append(Path(path).name)
            if Path(path).name == "bad.txt":
                raise ValueError("cannot read this one")
            return real(path, kind)

        search.extract = flaky
        try:
            ix.run_once()
            c = st.counts()
            assert (c["files_done"], c["files_error"]) == (2, 1), c
            assert st.errors() == [{"path": str(root / "bad.txt"), "error": "ValueError: cannot read this one"}]
            assert sorted(calls) == ["also.txt", "bad.txt", "good.txt"]
            ix.run_once()
            assert calls.count("bad.txt") == 1, "a failed file is not retried until it changes"
            (root / "bad.txt").write_text("fixed now")
            os.utime(root / "bad.txt", (time.time() + 5, time.time() + 5))
            ix.run_once()
            assert calls.count("bad.txt") == 2
        finally:
            search.extract = real


@check
def indexer_holds():
    with tempfile.TemporaryDirectory() as d:
        _, st, ix, cfg = _tree(d, {"a.txt": "plumber invoice"})
        ix.on_ac, ix.app_busy = (lambda: True), (lambda: False)
        assert ix.hold_reason(cfg) is None
        assert ix.hold_reason({**cfg, "paused": True}) == "user"
        ix.app_busy = lambda: True
        assert ix.hold_reason(cfg) == "busy"
        ix.app_busy, ix.on_ac = (lambda: False), (lambda: False)
        assert ix.hold_reason(cfg) is None, "on_battery is on in this config"
        assert ix.hold_reason({**cfg, "on_battery": False}) == "battery"

        cfg["paused"] = True                               # paused: nothing is indexed, and stop ends the wait promptly
        t = threading.Thread(target=ix.run_once)
        t.start()
        time.sleep(0.3)
        assert ix.state == "paused" and ix.paused_reason == "user" and st.counts()["files_done"] == 0
        ix.stop.set()
        t.join(3)
        assert not t.is_alive()

        ix.error = "model failed its self-test"
        assert ix.status()["error"] == "model failed its self-test"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py indexer_changes`
Expected: FAIL with `AttributeError: module 'search' has no attribute 'Indexer'`.

- [ ] **Step 3: Implement text extraction**

Append to `search.py`:

```python
# ---------------------------------------------------------------- extractors
# extract() turns one file into items to embed: text chunks, images, or audio segments.

def _text_items(sections):
    """sections: [(locator, text)] -> text items, capped at MAX_CHUNKS for the whole file."""
    items = []
    for locator, text in sections:
        for chunk in chunk_text(text):
            items.append({"kind": "text", "text": chunk, "locator": locator})
            if len(items) == MAX_CHUNKS:
                return items
    return items


def extract(path, kind):
    p = Path(path)
    return _text_items([("", p.read_bytes().decode("utf-8", errors="replace"))])
```

- [ ] **Step 4: Implement the indexer**

Append to `search.py`:

```python
# ---------------------------------------------------------------- indexer

TEXT_BATCH = 32
RESCAN_SECONDS = 300
IDLE_UNLOAD_SECONDS = 600


class Indexer:
    def __init__(self, store, embedder, config=load_config, app_url=APP_URL):
        self.store, self.embedder, self.config, self.app_url = store, embedder, config, app_url
        # "starting" until the first pass begins: an empty index must not read as "up to date"
        self.state, self.paused_reason, self.current, self.no_access, self.error = "starting", None, None, [], None
        self.wake, self.stop = threading.Event(), threading.Event()
        self._ac = (0.0, True)

    def on_ac(self):
        if time.time() - self._ac[0] > 30:                # pmset is a subprocess: ask at most twice a minute
            try:
                out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
                self._ac = (time.time(), "AC Power" in out)
            except Exception:
                self._ac = (time.time(), True)
        return self._ac[1]

    def app_busy(self):
        """True while the app is streaming a chat reply or generating an image."""
        if not self.app_url:
            return False
        try:
            with urllib.request.urlopen(self.app_url + "/api/busy", timeout=1) as r:
                return bool(json.load(r).get("busy"))
        except Exception:
            return False                                  # app not answering: keep indexing

    def hold_reason(self, cfg):
        if cfg["paused"]:
            return "user"
        if not cfg["on_battery"] and not self.on_ac():
            return "battery"
        return "busy" if self.app_busy() else None

    def wait_clear(self):
        """Block while indexing should hold off. False means the service is stopping."""
        while not self.stop.is_set():
            reason = self.hold_reason(self.config())
            if reason is None:
                self.state, self.paused_reason = "indexing", None
                return True
            self.state, self.paused_reason = "paused", reason
            self.stop.wait(1)
        return False

    def index_file(self, f):
        self.current = f["path"]
        try:
            items = extract(f["path"], f["kind"]) if f["kind"] != "name" else []
            rows, texts = [], [i for i in items if i["kind"] == "text"]
            for i in range(0, len(texts), TEXT_BATCH):
                if not self.wait_clear():
                    return
                batch = texts[i:i + TEXT_BATCH]
                vecs = self.embedder.embed_texts([doc_text(f["name"], t["text"]) for t in batch])
                rows += [("text", t["text"], t["locator"], v) for t, v in zip(batch, vecs)]
            for item in items:                            # images and audio: one per call
                if item["kind"] == "text":
                    continue
                if not self.wait_clear():
                    return
                vec = self.embedder.embed_image(item["image"]) if item["kind"] == "image" else self.embedder.embed_audio(item["audio"])
                rows.append((item["kind"], None, item["locator"], vec))
            self.store.replace_chunks(f["id"], rows)
        except Exception as e:                            # one unreadable file must not stop the crawl
            self.store.mark_error(f["id"], f"{type(e).__name__}: {e}"[:300])
        finally:
            self.current = None

    def run_once(self):
        cfg, no_access = self.config(), []
        found = list(walk(cfg["roots"], cfg["removed"], no_access))
        self.no_access = no_access
        self.store.sync(found, keep_prefixes=no_access)
        for f in self.store.pending():
            if not self.wait_clear():
                return
            self.index_file(f)

    def run(self):
        while not self.stop.is_set():
            if self.error is None:
                try:
                    self.run_once()
                except Exception as e:
                    print(f"index pass failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            self.state, self.paused_reason = "idle", None
            deadline = time.time() + RESCAN_SECONDS
            while not self.stop.is_set() and not self.wake.is_set() and time.time() < deadline:
                if self.embedder.loaded and time.time() - self.embedder.last_used > IDLE_UNLOAD_SECONDS:
                    self.embedder.unload()                # give the memory back until the next query or change
                self.wake.wait(5)
            self.wake.clear()

    def status(self):
        return {"state": self.state, "paused_reason": self.paused_reason, "current": self.current,
                "model_loaded": self.embedder.loaded, "roots": self.config()["roots"], "no_access": self.no_access,
                "error": self.error, **self.store.counts()}
```

- [ ] **Step 5: Run the checks to verify they pass**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py`
Expected: twelve `ok` lines, the last three being `ok indexer_changes`, `ok indexer_errors`, `ok indexer_holds`, then `search checks OK`.

- [ ] **Step 6: Commit**

```bash
git add search.py macos/check_search.py
git commit -m "search.py: background indexer with pause, battery and busy holds"
```

---

### Task 6: Real extractors, the MLX embedder and the end-to-end check

**Files:**
- Modify: `search.py` (replace `extract`, add helpers in the extractors section; add the embedder section after it; add `run_eval` and `main` at the end)
- Create: `macos/make_search_samples.py`

**Interfaces:**
- Consumes: `_text_items`, `Store`, `Indexer`, `Searcher`, `doc_text`, `MODEL_REPO`, `DIM`.
- Produces:
  - `extract(path, kind)` now handles `.pdf` (text, and scanned as page images), `.docx`, `.xlsx`/`.xlsm`, images and audio.
  - `_load_image(path) -> PIL.Image | None` (RGB, long side ≤ 1024; `None` when a side is under 64 px).
  - `class Embedder(repo=MODEL_REPO)` implementing the embedder interface from Task 4, plus `selftest() -> bool`.
  - `run_eval(folder: Path) -> int` (0 when every query's expected file ranks first) and `main()`.
  - `python macos/make_search_samples.py <out_dir>` writes the sample files to `<out_dir>/files/` and `<out_dir>/queries.json` (`[{"query": str, "expect": str}]`). The queries file sits outside the indexed folder on purpose: it contains every query word and would otherwise outrank the real files.
  - Constants `IMAGE_SIDE = 1024`, `MIN_IMAGE_SIDE = 64`, `SCAN_PAGES = 20`, `SAMPLE_RATE = 16000`, `SEGMENT_SECONDS = 30`, `MAX_SEGMENTS = 20`.

- [ ] **Step 1: Write the sample builder (the end-to-end test's input)**

Create `macos/make_search_samples.py`:

```python
"""Builds a small mixed folder (documents, photos, speech) in <out>/files and <out>/queries.json for
`search.py --eval`. The output is generated on this Mac from system files and is never committed.

    uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python macos/make_search_samples.py build/search_samples
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

TEXTS = {
    "invoice_acme.txt": "Invoice 2291 from Acme Plumbing. Replaced the kitchen sink trap and a leaking shut-off valve. Labour 2 hours at 95 dollars, parts 48 dollars. Total due 238 dollars by 30 November. Pay by bank transfer.",
    "lasagna.txt": "Grandma's lasagna: brown 500 g of beef mince with onion and garlic, add crushed tomatoes and simmer 30 minutes. Layer with pasta sheets, bechamel and mozzarella. Bake at 180 C for 45 minutes until golden.",
    "standup_notes.txt": "Team sync 12 Oct. Priya will finish the login redesign by Friday. The payments bug is blocked on the bank sandbox. We agreed to move the launch to the first week of December.",
    "tokyo_trip.txt": "Japan itinerary: land at Haneda on 3 April, four nights in Shinjuku, day trip to Nikko, then the bullet train to Kyoto for the cherry blossoms. Return flight leaves Osaka on 12 April.",
    "blood_test.txt": "Lab results: fasting glucose 5.4 mmol/L, total cholesterol 6.1 mmol/L (slightly high), vitamin D 42 nmol/L (low). Doctor recommends a vitamin D supplement and a repeat lipid panel in six months.",
    "lease.txt": "Residential tenancy agreement for Unit 4, 18 Harbour Street. Rent is 2,450 dollars per month, due on the first. Bond of four weeks' rent. The lease runs 12 months from 1 February and pets need written consent.",
    "debounce.py": 'import time\ndef debounce(wait):\n    """Decorator: ignore calls that arrive less than wait seconds after the previous one."""\n    def deco(fn):\n        last = [0.0]\n        def inner(*a, **k):\n            now = time.monotonic()\n            if now - last[0] >= wait:\n                last[0] = now\n                return fn(*a, **k)\n        return inner\n    return deco\n',
    "car_insurance.txt": "Policy renewal notice: comprehensive cover for the 2021 Mazda CX-5 renews on 14 January. Annual premium 1,180 dollars, excess 750 dollars. Windscreen cover and a hire car after theft are included.",
}
SPEECH = {
    "voice_memo_1.aiff": "Reminder to self. Call the dentist tomorrow morning and move my appointment to next Thursday afternoon.",
    "voice_memo_2.m4a": "Idea for the garden. Plant tomatoes and basil along the back fence, and build a raised bed for the strawberries.",
    "voice_memo_3.aiff": "The quarterly sales numbers are up twelve percent, mostly from the new subscription plan in Europe.",
}
WALLPAPERS = Path("/System/Library/Desktop Pictures/.thumbnails")
PHOTOS = {"IMG_2001.jpg": "Big Sur Night Succulents.heic", "IMG_2002.jpg": "Big Sur Road.heic",
          "IMG_2003.heic": "Catalina Sunset.heic", "IMG_2004.jpg": "Big Sur Waters Edge.heic"}
# The first 13 were verified in the feasibility test (13 Oct 2026). The last 4 cover file types that test did not.
QUERIES = [
    ("how much do I owe the plumber", "invoice_acme.txt"), ("when does my lease start and what is the rent", "lease.txt"),
    ("recipe with mince and cheese", "lasagna.txt"), ("when are we launching", "standup_notes.txt"),
    ("my cholesterol results", "blood_test.txt"), ("function that rate limits repeated calls", "debounce.py"),
    ("flights to Japan in spring", "tokyo_trip.txt"), ("car insurance excess", "car_insurance.txt"),
    ("photo of a desert plant at night", "IMG_2001.jpg"), ("road winding along the coast", "IMG_2002.jpg"),
    ("voice note about the dentist", "voice_memo_1.aiff"), ("recording about growing vegetables", "voice_memo_2.m4a"),
    ("what did I say about sales in Europe", "voice_memo_3.aiff"),
    ("warranty period for the washing machine", "warranty.pdf"), ("monthly grocery and electricity spending", "budget.xlsx"),
    ("cover letter for the nursing job", "cover_letter.docx"), ("scanned parking fine", "scan_0042.pdf"),
]


def text_pdf(path, lines):
    """A minimal one-page PDF with real (extractable) text, written by hand to avoid a PDF library."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")              # noqa: E731
    stream = "BT /F1 12 Tf 50 760 Td 16 TL " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def main(out):
    import docx
    import openpyxl
    from PIL import Image, ImageDraw, ImageFont

    shutil.rmtree(out, ignore_errors=True)
    top, out = out, out / "files"                          # queries.json stays outside the indexed folder
    out.mkdir(parents=True)
    for name, text in TEXTS.items():
        (out / name).write_text(text)

    for name, src in PHOTOS.items():                       # real photos, converted from this Mac's own wallpapers
        if name.endswith(".heic"):
            shutil.copy(WALLPAPERS / src, out / name)
        else:
            subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "1600", str(WALLPAPERS / src), "--out", str(out / name)],
                           check=True, capture_output=True)

    for name, words in SPEECH.items():
        aiff = out / (Path(name).stem + ".aiff")
        subprocess.run(["say", "-o", str(aiff), words], check=True)
        if name.endswith(".m4a"):
            subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(aiff), str(out / name)], check=True)
            aiff.unlink()

    text_pdf(out / "warranty.pdf", ["Warranty certificate", "Product: Bosch Series 6 washing machine, model WAU28T64.",
                                    "The warranty period is 24 months from the date of purchase, 3 March 2026.",
                                    "It covers parts and labour for manufacturing faults. Keep this certificate with your receipt."])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Household budget"
    for row in [("Month", "Groceries", "Electricity", "Internet"), ("July", 812, 143, 79), ("August", 790, 151, 79), ("September", 845, 128, 79)]:
        ws.append(row)
    wb.save(out / "budget.xlsx")

    d = docx.Document()
    d.add_heading("Cover letter", 1)
    d.add_paragraph("Dear Hiring Manager, I am applying for the registered nurse position in the cardiac ward at St Mary's Hospital.")
    d.add_paragraph("I have six years of experience in acute care nursing, including two years as a charge nurse on night shift.")
    d.save(out / "cover_letter.docx")

    page = Image.new("RGB", (1240, 1754), "white")         # an image-only PDF, like a phone scan
    draw, font = ImageDraw.Draw(page), ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 44)
    for i, line in enumerate(["CITY COUNCIL", "PARKING INFRINGEMENT NOTICE", "", "Vehicle: Mazda CX-5", "Offence: parked in a no-stopping zone",
                              "Location: Harbour Street", "Amount due: 96 dollars", "Pay within 28 days"]):
        draw.text((110, 160 + i * 90), line, fill="black", font=font)
    page.save(out / "scan_0042.pdf")

    (top / "queries.json").write_text(json.dumps([{"query": q, "expect": e} for q, e in QUERIES], indent=2))
    print(f"wrote {len(list(out.iterdir()))} files and {len(QUERIES)} queries to {top}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
```

- [ ] **Step 2: Build the samples**

Run: `uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python macos/make_search_samples.py build/search_samples`
Expected: `wrote 19 files and 17 queries to build/search_samples`. (The first run downloads the dependencies, a few hundred MB.)

- [ ] **Step 3: Run the end-to-end check to verify it fails**

Run: `uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --eval build/search_samples`
Expected: no output and exit status 0, because `search.py` has no `main` yet. Confirm with `grep -c "def main" search.py` printing `0`.

- [ ] **Step 4: Replace `extract` with the full version**

In `search.py`, replace the whole `extract` function from Task 5 with:

```python
IMAGE_SIDE = 1024
MIN_IMAGE_SIDE = 64               # icons and spacers
SCAN_PAGES = 20
SAMPLE_RATE = 16000
SEGMENT_SECONDS = 30
MAX_SEGMENTS = 20                 # the first 10 minutes of a file


def _fit(im):
    im = im.convert("RGB")
    im.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
    return im


def _load_image(p):
    from PIL import Image
    if p.suffix.lower() == ".heic":
        import pillow_heif
        pillow_heif.register_heif_opener()
    with Image.open(p) as im:
        if min(im.size) < MIN_IMAGE_SIDE:
            return None
        im.draft("RGB", (IMAGE_SIDE, IMAGE_SIDE))         # lets the JPEG decoder downscale while decoding
        return _fit(im)


def _pdf_items(p):
    from pypdf import PdfReader
    pages = [(pg.extract_text() or "").strip() for pg in PdfReader(str(p)).pages]
    chars = sum(len(t) for t in pages)
    if pages and chars < 80 * len(pages) and chars < 2000:            # scanned: same test as app.py's attachments
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(p))
        return [{"kind": "image", "image": _fit(doc[i].render(scale=2).to_pil()), "locator": f"p{i + 1}"}
                for i in range(min(SCAN_PAGES, len(doc)))]
    return _text_items([(f"p{i + 1}", t) for i, t in enumerate(pages) if t])


def _docx_text(p):
    import docx
    d = docx.Document(str(p))
    parts = [para.text for para in d.paragraphs if para.text.strip()]
    for t in d.tables:
        parts.append("\n".join(" | ".join(c.text.strip() for c in r.cells) for r in t.rows))
    return "\n\n".join(parts)


def _xlsx_sections(p):
    import openpyxl
    wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    sections = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                rows.append(" | ".join("" if v is None else str(v) for v in row))
            if len(rows) >= 2000:
                break
        sections.append((ws.title, "\n".join(rows)))
    return sections


def _audio_segments(p):
    import soundfile as sf
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "a.wav"
        r = subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(p), str(wav)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"afconvert failed: {r.stderr.strip()[:200]}")
        samples, _ = sf.read(wav, dtype="float32", frames=SAMPLE_RATE * SEGMENT_SECONDS * MAX_SEGMENTS)
    size = SAMPLE_RATE * SEGMENT_SECONDS
    segs = [samples[i:i + size] for i in range(0, len(samples), size)]
    return [s for i, s in enumerate(segs) if i == 0 or len(s) >= SAMPLE_RATE]     # drop a tail shorter than a second


def extract(path, kind):
    p = Path(path)
    ext = p.suffix.lower()
    if kind == "image":
        im = _load_image(p)
        return [{"kind": "image", "image": im, "locator": ""}] if im is not None else []
    if kind == "audio":
        return [{"kind": "audio", "audio": seg, "locator": f"{i * SEGMENT_SECONDS}s"} for i, seg in enumerate(_audio_segments(p))]
    if ext == ".pdf":
        return _pdf_items(p)
    if ext == ".docx":
        return _text_items([("", _docx_text(p))])
    if ext in (".xlsx", ".xlsm"):
        return _text_items(_xlsx_sections(p))
    return _text_items([("", p.read_bytes().decode("utf-8", errors="replace"))])
```

- [ ] **Step 5: Add the embedder section**

Insert into `search.py` between the extractors section and the indexer section:

```python
# ---------------------------------------------------------------- embedder
# EmbeddingGemma 2 on MLX. One lock around every model call: a query can arrive mid-batch.

class Embedder:
    def __init__(self, repo=MODEL_REPO):
        self.repo, self.lock = repo, threading.Lock()
        self.model = self.processor = None
        self.last_used = time.time()

    @property
    def loaded(self):
        return self.model is not None

    def unload(self):
        with self.lock:
            self.model = self.processor = None
            import gc
            import mlx.core as mx
            gc.collect()
            mx.clear_cache()

    def _run(self, make_inputs):
        import mlx.core as mx
        with self.lock:
            if self.model is None:
                from mlx_vlm import load
                self.model, self.processor = load(self.repo)
            self.last_used = time.time()
            inputs = make_inputs(self.processor)
            out = self.model(**{k: mx.array(v) for k, v in inputs.items()}).text_embeds
            mx.eval(out)
            return np.array(out.astype(mx.float32))

    def embed_texts(self, texts):
        """texts already carry their task prefix (see doc_text)."""
        return self._run(lambda p: p.tokenizer(list(texts), padding=True, truncation=True, max_length=2048, return_tensors="np"))

    def embed_query(self, query):
        return self.embed_texts([f"task: search result | query: {query}"])[0]

    def embed_image(self, image):
        # One image per call: the processor treats a list of images as a single combined input.
        return self._run(lambda p: p(images=[image], return_tensors="np"))[0]

    def embed_audio(self, samples):
        return self._run(lambda p: p(audio=[samples], sampling_rate=SAMPLE_RATE, return_tensors="np"))[0]

    def selftest(self):
        v = self.embed_query("hello")
        return v.shape == (DIM,) and bool(np.isfinite(v).all()) and abs(float(np.linalg.norm(v)) - 1.0) < 0.01
```

- [ ] **Step 6: Add `run_eval` and `main` at the end of the file**

Append to `search.py`:

```python
# ---------------------------------------------------------------- command line

def run_eval(folder):
    """Index `folder`/files into a throwaway index with the real model and check `folder`/queries.json."""
    folder = Path(folder).resolve()
    queries = json.loads((folder / "queries.json").read_text())
    with tempfile.TemporaryDirectory() as d:
        store, embedder = Store(Path(d) / "index.db"), Embedder()
        cfg = {"roots": [str(folder / "files")], "removed": [], "paused": False, "on_battery": True}
        indexer, searcher = Indexer(store, embedder, config=lambda: cfg, app_url=None), Searcher(store, embedder)
        t = time.time()
        indexer.run_once()
        c = store.counts()
        print(f"indexed {c['files_done']} files ({c['chunks']} chunks, {c['files_error']} errors) in {time.time() - t:.1f}s")
        for e in store.errors():
            print(f"  ERROR {Path(e['path']).name}: {e['error']}")
        failed = 0
        for q in queries:
            res = searcher.search(q["query"])
            top = res[0] if res else None
            ok = top is not None and top["name"] == q["expect"]
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {q['query']:48s} -> {top['name'] if top else '(nothing)'} "
                  f"{top['score'] if top else ''}{'' if ok else '   expected ' + q['expect']}")
        print(f"{len(queries) - failed}/{len(queries)} queries ranked the expected file first")
        return 1 if failed or c["files_error"] else 0


def main():
    ap = argparse.ArgumentParser(description="Local Studio search service")
    ap.add_argument("--port", type=int, default=8083)
    ap.add_argument("--eval", metavar="FOLDER", help="index FOLDER/files into a temporary index and check FOLDER/queries.json")
    args = ap.parse_args()
    if args.eval:
        sys.exit(run_eval(args.eval))


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Run the model-free checks (nothing should have regressed)**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py`
Expected: twelve `ok` lines and `search checks OK`.

- [ ] **Step 8: Run the end-to-end check with the real model**

Run: `uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --eval build/search_samples`
Expected: `indexed 19 files (… chunks, 0 errors) in …s`, seventeen lines starting `ok`, then `17/17 queries ranked the expected file first`, exit status 0. The first 13 queries were verified in the feasibility test and must pass. If one of the last four (PDF, spreadsheet, Word, scanned PDF) fails, first check the `ERROR` lines and the extractor for that type; if extraction is correct and only the ranking is off, change that sample's wording or query in `macos/make_search_samples.py`, not the ranking constants, and say so in the commit message.

- [ ] **Step 9: Check whether `librosa` is needed**

Remove the `librosa` line from `search-requirements.txt` and repeat Step 8. If it still prints `17/17`, keep the line removed. If the audio queries now fail or error, restore the line.

- [ ] **Step 10: Commit**

```bash
git add search.py search-requirements.txt macos/make_search_samples.py
git commit -m "search.py: document, image and audio extraction with the MLX embedder"
```

---

### Task 7: HTTP API and service start-up

**Files:**
- Modify: `search.py` (add the HTTP API section before the command-line section; extend `main`)

**Interfaces:**
- Consumes: `Store`, `Embedder`, `Indexer`, `Searcher`, `_load_image`, `INDEX_DIR`.
- Produces: `make_app(store, embedder, indexer, searcher) -> FastAPI` with the routes below, and `python search.py --port 8083` serving them on `127.0.0.1`.

| Route | Request | Response |
|---|---|---|
| `GET /health` | | `{"ok": true}` |
| `GET /status` | | `Indexer.status()`: `state`, `paused_reason`, `current`, `model_loaded`, `roots`, `no_access`, `error`, plus the store counts |
| `POST /search` | `{"query", "kind"?, "folder"?, "limit"?}` | `{"results": [...]}` |
| `POST /passages` | `{"query", "limit"?, "max_chars"?}` | `{"passages": [...]}` |
| `POST /reload` | | `{"ok": true}`; wakes the indexer to re-read settings and re-crawl |
| `GET /errors` | | `{"errors": [{"path", "error"}]}` |
| `GET /file/{file_id}` | | `{"path", "name", "kind", "mtime"}`; 404 if the id is not in the index. The app uses it to open a file without trusting a path from the browser. |
| `GET /thumb/{file_id}` | | a JPEG, long side ≤ 320 px; 404 if the id is not an indexed image |

- [ ] **Step 1: Add the HTTP API section**

Insert into `search.py` before the `# ---- command line` banner:

```python
# ---------------------------------------------------------------- HTTP API

def make_app(store, embedder, indexer, searcher):
    import io
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import Response

    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/status")
    def status():
        return indexer.status()

    @app.post("/search")
    def search(body: dict = Body(...)):
        if indexer.error:
            raise HTTPException(503, indexer.error)
        limit = max(1, min(int(body.get("limit") or RESULT_LIMIT), 100))
        return {"results": searcher.search(body.get("query"), body.get("kind") or None, body.get("folder") or None, limit)}

    @app.post("/passages")
    def passages(body: dict = Body(...)):
        if indexer.error:
            raise HTTPException(503, indexer.error)
        return {"passages": searcher.passages(body.get("query"), int(body.get("limit") or 6), int(body.get("max_chars") or 12000))}

    @app.post("/reload")
    def reload():
        indexer.wake.set()
        return {"ok": True}

    @app.get("/errors")
    def errors():
        return {"errors": store.errors()}

    @app.get("/file/{file_id}")
    def file(file_id: int):
        f = store.file_info([file_id]).get(file_id)
        if not f:
            raise HTTPException(404, "not in the index")
        return f

    @app.get("/thumb/{file_id}")
    def thumb(file_id: int):
        f = store.file_info([file_id]).get(file_id)
        if not f or f["kind"] != "image":
            raise HTTPException(404, "not an indexed image")
        try:
            im = _load_image(Path(f["path"]))
        except Exception:
            im = None
        if im is None:
            raise HTTPException(404, "image unavailable")
        im.thumbnail((320, 320))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    return app
```

- [ ] **Step 2: Make `main` start the service**

In `search.py`, replace the `main` function with:

```python
def main():
    ap = argparse.ArgumentParser(description="Local Studio search service")
    ap.add_argument("--port", type=int, default=8083)
    ap.add_argument("--eval", metavar="FOLDER", help="index FOLDER/files into a temporary index and check FOLDER/queries.json")
    args = ap.parse_args()
    if args.eval:
        sys.exit(run_eval(args.eval))

    store, embedder = Store(INDEX_DIR / "index.db"), Embedder()
    indexer, searcher = Indexer(store, embedder), Searcher(store, embedder)

    def boot():
        try:                                              # a broken install must not index garbage vectors
            if not embedder.selftest():
                indexer.error = "The embedding model returned an invalid vector."
        except Exception as e:
            indexer.error = f"The embedding model failed to load: {type(e).__name__}: {e}"[:400]
        indexer.run()

    threading.Thread(target=boot, daemon=True).start()
    import uvicorn
    uvicorn.run(make_app(store, embedder, indexer, searcher), host="127.0.0.1", port=args.port, log_level="warning")
```

- [ ] **Step 3: Run the model-free checks**

Run: `uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py`
Expected: twelve `ok` lines and `search checks OK`. This also proves `import search` still needs only numpy.

- [ ] **Step 4: Start the service against the sample folder only**

The environment variables point user data at a throwaway folder and replace the default crawl roots, so no real folder is read.

Run in one Terminal tab (it keeps running):

```bash
T=$(mktemp -d) && echo "state: $T" && LOCAL_STUDIO_HOME="$T" LOCAL_STUDIO_SEARCH_ROOTS="$PWD/build/search_samples/files" uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --port 8083
```

Expected: `state: /var/folders/…` and then no further output (log level is warning).

- [ ] **Step 5: Exercise every route**

Run in a second tab, one at a time:

```bash
until curl -sf http://127.0.0.1:8083/health; do sleep 1; done; echo
```
Expected: `{"ok":true}`

```bash
until curl -s http://127.0.0.1:8083/status | grep -q '"state":"idle"'; do sleep 2; done; curl -s http://127.0.0.1:8083/status
```
Expected (after about 30 s): JSON with `"state":"idle"`, `"files_total":19`, `"files_done":19`, `"files_error":0`, `"model_loaded":true`, `"no_access":[]`, `"error":null`. While the model loads the state is `"starting"`, which is why the loop waits for `"idle"`.

```bash
curl -s http://127.0.0.1:8083/search -H 'Content-Type: application/json' -d '{"query":"how much do I owe the plumber"}' | python3 -c "import json,sys; r=json.load(sys.stdin)['results']; print(len(r), r[0]['name'], r[0]['score'], r[0]['snippet'][:40])"
```
Expected: `1 invoice_acme.txt 0.767 Invoice 2291 from Acme Plumbing. Replace` (the count may be 2 or 3 and the score may differ in the last digit).

```bash
curl -s http://127.0.0.1:8083/search -H 'Content-Type: application/json' -d '{"query":"desert plant at night","kind":"image"}' | python3 -c "import json,sys; r=json.load(sys.stdin)['results']; print([x['name'] for x in r]); print(r[0]['file_id'])"
```
Expected: `['IMG_2001.jpg']` (only image files may appear), then its numeric id.

```bash
curl -s -o /tmp/thumb.jpg -w "%{http_code} %{content_type} %{size_download}\n" http://127.0.0.1:8083/thumb/<id from the previous command>
```
Expected: `200 image/jpeg` and a size of about 20,000 bytes.

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8083/thumb/999999
```
Expected: `404`

```bash
curl -s http://127.0.0.1:8083/passages -H 'Content-Type: application/json' -d '{"query":"what is the rent on my lease"}' | python3 -c "import json,sys; p=json.load(sys.stdin)['passages']; print(p[0]['name'], '|', p[0]['text'][:50])"
```
Expected: `lease.txt | Residential tenancy agreement for Unit 4, 18 Harbo`

```bash
curl -s http://127.0.0.1:8083/search -H 'Content-Type: application/json' -d '{"query":"zebra astronomy quantum physics lecture"}' | python3 -c "import json,sys; print(len(json.load(sys.stdin)['results']))"
```
Expected: `1`. A query with nothing relevant shows its single closest file, not all 19.

```bash
curl -s http://127.0.0.1:8083/file/<id from the image search>; echo; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8083/file/999999
```
Expected: JSON with the full `path` of `IMG_2001.jpg`, then `404`.

```bash
curl -s http://127.0.0.1:8083/errors; echo; curl -s -X POST http://127.0.0.1:8083/reload; echo
```
Expected: `{"errors":[]}` then `{"ok":true}`.

- [ ] **Step 6: Confirm a restart reuses the index**

Stop the service with Ctrl-C in its tab. Re-run the Step 4 command but with the same state folder: replace `T=$(mktemp -d)` with `T=<the folder it printed>`. Then:

```bash
until curl -sf http://127.0.0.1:8083/health >/dev/null; do sleep 1; done; sleep 20; curl -s http://127.0.0.1:8083/status
```
Expected: `"state":"idle"` with `"files_done":19`, reached without re-embedding (the `chunks` count is unchanged from Step 5). Stop the service with Ctrl-C and delete the state folder.

- [ ] **Step 7: Commit**

```bash
git add search.py
git commit -m "search.py: HTTP API and service start-up"
```
