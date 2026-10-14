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


# ---------------------------------------------------------------- crawler
# Only these extensions have their content read; everything else is indexed by name alone.

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
            ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java",
            ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".zsh", ".sql", ".toml",
            ".ini", ".cfg", ".log", ".r", ".m", ".scala", ".lua", ".pl", ".dart", ".vue", ".svelte"}   # same list as app.py
DOC_EXT = TEXT_EXT | {".pdf", ".docx", ".xlsx", ".xlsm"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aiff", ".aif", ".caf", ".flac"}
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "build", "dist", "target", "DerivedData", "Pods",
             "site-packages", "dist-packages"}
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


def _python_env(path):
    """A virtual or conda environment, whatever it is called: thousands of library files, none the user's."""
    return os.path.exists(os.path.join(path, "pyvenv.cfg")) or os.path.isdir(os.path.join(path, "conda-meta"))


# Not node_modules: installed packages often have a nested one, and a project that ran an install has a lockfile.
PROJECT_MARKERS = (".git", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock")
PACKAGE_DUMP_MIN = 5              # this many installed packages side by side means "the contents of a node_modules"


def _installed_package(path):
    """An npm package as it sits in node_modules: a package.json, and none of the things a project
    someone is working on has (a lockfile, git)."""
    return (os.path.exists(os.path.join(path, "package.json"))
            and not any(os.path.exists(os.path.join(path, m)) for m in PROJECT_MARKERS))


def _package_dump(entries):
    """Names of the sub-folders that are installed npm packages, when there are enough of them side
    by side to be sure this folder is a node_modules under another name. One or two folders with a
    package.json are far more likely to be the user's own, so those are left alone."""
    dirs = []
    for e in entries:
        try:
            if not e.name.startswith(".") and not e.is_symlink() and e.is_dir(follow_symlinks=False):
                dirs.append(e)
        except OSError:
            pass
    found = {e.name for e in dirs if not e.name.startswith("@") and _installed_package(e.path)}
    if len(found) < PACKAGE_DUMP_MIN:
        return set()
    for e in dirs:                                        # scopes: @babel/core, @types/node, ...
        if e.name.startswith("@"):
            try:
                subs = [x.path for x in os.scandir(e.path) if x.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            if subs and all(_installed_package(x) for x in subs):
                found.add(e.name)
    return found


def walk(roots, removed=(), no_access=None, failed=None):
    """Yield (path, size, mtime, kind) once for every file worth indexing under `roots`. Kind is
    "placeholder" for an iCloud file that isn't downloaded. Roots macOS won't let us list go to
    `no_access`; every folder that couldn't be listed (denied, unplugged, missing) goes to `failed`,
    so the caller can keep what it already knows about the files inside."""
    app_data = [str(STATE / d) for d in ("data", "uploads", "logs", "gallery")] + [str(HF_HUB)]
    library = str(HOME / "Library")
    roots = list(dict.fromkeys(str(r).rstrip("/") or "/" for r in roots))
    root_set = set(roots)
    for root in roots:
        # A folder the user added is indexed even inside one they removed, and even under ~/Library
        # (iCloud Drive, ~/Library/CloudStorage); walking down from above never enters either.
        gone = [x for x in removed if not _under(root, x)]
        in_library = _under(root, library)

        def off_limits(path):
            return (any(_under(path, b) for b in app_data) or any(_under(path, x) for x in gone)
                    or (not in_library and _under(path, library)))

        if off_limits(root):
            continue
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError as err:
                if failed is not None:
                    failed.append(d)
                if no_access is not None and d == root and isinstance(err, PermissionError):
                    no_access.append(d)
                continue
            dumped = _package_dump(entries)
            for e in entries:
                try:
                    if e.name.startswith(".") or e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if (e.path not in root_set                   # another root: its own walk covers it
                                and e.name not in dumped
                                and e.name not in SKIP_DIRS and not e.name.lower().endswith(PACKAGE_SUFFIXES)
                                and not off_limits(e.path) and not _python_env(e.path)):
                            stack.append(e.path)
                        continue
                    if any(fnmatch.fnmatch(e.name, g) for g in SECRET_GLOBS):
                        continue
                    st = e.stat(follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    yield e.path, st.st_size, st.st_mtime, "placeholder" if _dataless(st) else classify(e.name, st.st_size)
                except OSError:
                    continue


# ---------------------------------------------------------------- store
# SQLite holds files, chunks, a keyword index and each chunk's vector (float16 on disk). The vectors
# are also kept in one float32 matrix in memory, so a query is a single matrix product.

KIND_ORDER = {"document": 0, "image": 1, "audio": 2, "name": 3}
EXTRACT_VERSION = 2               # bump when a version can read files an earlier one could not: failed files get another go
VACUUM_AFTER = 1000               # files deleted in one crawl before the database file is compacted
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
        self.db.execute("PRAGMA synchronous=NORMAL")      # safe with WAL; the in-flight marker commits once per file
        self.db.execute("PRAGMA foreign_keys=ON")
        want = f"{model}:{dim}"
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
        if row and row[0] != want:                        # vectors from another model can't be compared
            self.db.executescript("DROP TABLE chunks; DROP TABLE files; DROP TABLE fts; DROP TABLE meta;" + SCHEMA)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (want,))
            row = self.db.execute("SELECT value FROM meta WHERE key='extract_version'").fetchone()
            if row is None or row[0] != str(EXTRACT_VERSION):     # this version may read what an older one could not
                self.db.execute("UPDATE files SET status='pending', error=NULL WHERE status='error'")
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('extract_version', ?)", (str(EXTRACT_VERSION),))
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
        self._compact()

    def _compact(self):
        """Drop dead rows once they outnumber the live ones (a file that is rewritten on every scan)."""
        keep = np.flatnonzero(self.alive[:self.n])
        if self.n - len(keep) <= max(1024, len(keep)):
            return
        for name in ("M", "ids", "fids", "fkind", "is_text", "alive"):
            a = getattr(self, name)
            a[:len(keep)] = a[keep]
        self.alive[len(keep):self.n] = False
        self.n = len(keep)

    def set_meta(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def get_meta(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _name_row(self, fid):
        path, name = self.db.execute("SELECT path, name FROM files WHERE id=?", (fid,)).fetchone()
        self.db.execute("INSERT INTO fts(rowid, name, text) VALUES (?, ?, '')", (-fid, f"{name} {Path(path).parent.name}"))
        return name

    def sync(self, found, keep_prefixes=()):
        """Bring the files table in line with a crawl. Files under `keep_prefixes` (folders that
        could not be listed this time) are kept even though the crawl did not see them."""
        with self.lock:
            result = self._sync(found, keep_prefixes)
            if result["deleted"] >= VACUUM_AFTER:         # a folder was removed or excluded: hand the space back
                self.db.execute("VACUUM")
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return result

    def _sync(self, found, keep_prefixes):
        with self.db:
            known = {p: (i, s, m, k) for i, p, s, m, k in self.db.execute("SELECT id, path, size, mtime, kind FROM files")}
            seen, new, changed = set(), 0, 0
            for path, size, mtime, kind in found:
                if path in seen:
                    continue
                seen.add(path)
                k = known.get(path)
                if kind == "placeholder":                 # iCloud evicted it: keep what we have; a new one is name-only
                    if k is not None:
                        continue
                    kind = "name"
                if k is None:
                    fid = self.db.execute("INSERT INTO files(path, name, kind, size, mtime, status) VALUES (?,?,?,?,?,'pending')",
                                          (path, Path(path).name, kind, size, mtime)).lastrowid
                    self._name_row(fid)                   # findable by name before its turn in the queue
                    new += 1
                elif (k[1], k[2]) != (size, mtime) or (k[3] == "name" and kind != "name"):   # edited, or now downloaded
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
        """The n best chunks as (chunk_id, score, z), best z first. z is how many deviations the score stands above
        the typical score for this query, measured separately for text and for image/audio chunks
        (media scores sit lower), with the median and MAD so a handful of real matches don't move it."""
        with self.lock:
            if not self.n:
                return []
            scores = self.M[:self.n] @ np.asarray(q, np.float32)
            alive, text = self.alive[:self.n], self.is_text[:self.n]
            z = np.zeros(self.n, np.float32)
            for group in (alive & text, alive & ~text):
                base = group if group.sum() >= Z_GROUP_MIN else alive      # too few of a kind: judge against everything
                if not base.any():
                    continue
                mid = float(np.median(scores[base]))
                spread = max(1.4826 * float(np.median(np.abs(scores[base] - mid))), SIGMA_MIN)
                z[group] = (scores[group] - mid) / spread
            ok = alive.copy()
            if file_kind:
                ok &= self.fkind[:self.n] == KIND_ORDER[file_kind]
            if text_only:
                ok &= text
            # Ranked by z, not raw score: image and audio chunks score lower than text across the board,
            # so by raw score a strong picture match would lose its place to mediocre text.
            z[~ok] = -np.inf
            k = min(n, self.n)
            top = np.argpartition(-z, k - 1)[:k]
            top = top[np.argsort(-z[top])]
            return [(int(self.ids[i]), float(scores[i]), float(z[i])) for i in top if ok[i]]

    def keyword_top(self, tokens, n):
        if not tokens:
            return []
        # Quoted terms, all must match. Longer words also match as a prefix: "mithran"* finds MithranPassport,
        # which the tokenizer keeps as one word. Short ones don't, or "rent" would find every "render".
        match = " ".join('"%s"%s' % (t.replace('"', ""), "*" if len(t) >= 5 else "") for t in tokens)
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

    def context(self, chunk_id):
        """A chunk with the text just before and after it in the same file, or None."""
        with self.lock:
            row = self.db.execute("SELECT file_id, ord, kind, text, locator FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            if row is None:
                return None
            fid, ord_, kind, text, locator = row
            near = dict(self.db.execute("SELECT ord, text FROM chunks WHERE file_id=? AND ord IN (?, ?) AND kind='text'",
                                        (fid, ord_ - 1, ord_ + 1)))
        return {"file_id": fid, "kind": kind, "locator": locator or "", "before": near.get(ord_ - 1) or "",
                "match": text or "", "after": near.get(ord_ + 1) or ""}

    def first_chunk(self, file_id):
        with self.lock:
            row = self.db.execute("SELECT id FROM chunks WHERE file_id=? ORDER BY ord LIMIT 1", (file_id,)).fetchone()
        return row[0] if row else None

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


# ---------------------------------------------------------------- ranking
# Meaning (vectors) and exact words (FTS5) are fused by rank. On a real index unrelated text scores
# around 0.6 against almost any query and the best unrelated chunk sits only a few hundredths below
# a real match, so absolute thresholds don't work. A vector hit counts when its score stands out
# from the background for that query (z, see Store.vector_top) and is not far below the best hit.

STOPWORDS = frozenset("the and for are was were with that this from have has had you your what when where which "
                      "who how why did does about into out not but can all any our its".split())
POOL = 200                        # chunks taken from each ranking before fusing
RRF_K = 60
Z_MIN = 4.5                       # a hit must stand at least this far above the background...
Z_MARGIN = 2.5                    # ...and within this of the best hit (both tuned on a 31,000-chunk index, 13 Oct 2026)
Z_GROUP_MIN = 50                  # fewer chunks of a kind than this: use the whole index as background
SIGMA_MIN = 0.02                  # floor for the spread, so a tiny or uniform index can't produce huge z values
KEYWORD_ONLY_MAX = 10             # files that only mention the words; files NAMED with them are never capped
RESULT_LIMIT = 30


def doc_text(name, chunk):
    return f"title: {name} | text: {chunk}"


def keyword_tokens(query):
    return [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 3 and t not in STOPWORDS][:12]


def _stands_out(z, best_z):
    return z >= max(Z_MIN, best_z - Z_MARGIN)


def fuse(vec_hits, kw_rowids, chunk_file):
    """Combine a vector ranking [(chunk_id, score, z)] and a keyword ranking [rowid] (negative =
    a hit on file -rowid's name) into one per-file ranking, applying the cut-off."""
    vec_hits = [h for h in vec_hits if h[0] in chunk_file]
    kw_rowids = [r for r in kw_rowids if r < 0 or r in chunk_file]
    chunk_score, files = {}, {}

    def slot(fid):
        return files.setdefault(fid, {"file_id": fid, "fused": 0.0, "chunk": None, "vec": None, "z": None, "kw": False, "named": False})

    for rank, (cid, score, z) in enumerate(vec_hits):
        chunk_score[cid] = 1.0 / (RRF_K + rank + 1)
        f = slot(chunk_file[cid])
        if f["vec"] is None or score > f["vec"]:
            f["vec"], f["z"] = score, z
    name_bonus = {}
    for rank, rowid in enumerate(kw_rowids):
        r = 1.0 / (RRF_K + rank + 1)
        if rowid < 0:
            name_bonus[-rowid] = r
            slot(-rowid).update(kw=True, named=True)
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
    best_z = max((h[2] for h in vec_hits), default=0.0)
    order = lambda f: (f["fused"], f["vec"] if f["vec"] is not None else -1.0)                    # noqa: E731
    strong = [f for f in files.values()
              if top and (f["file_id"] == chunk_file[top[0]] or (f["z"] is not None and _stands_out(f["z"], best_z)))]
    kept = {f["file_id"] for f in strong}
    rest = sorted((f for f in files.values() if f["kw"] and f["file_id"] not in kept), key=order, reverse=True)
    named = [f for f in rest if f["named"]]
    mentions = [f for f in rest if not f["named"]][:KEYWORD_ONLY_MAX]
    out = sorted(strong + named + mentions, key=order, reverse=True)
    for f in out:
        f.pop("z"); f.pop("named")
    return out


class Searcher:
    def __init__(self, store, embedder):
        self.store, self.embedder = store, embedder

    def search(self, query, kind=None, folder=None, limit=RESULT_LIMIT):
        query = (query or "").strip()
        if not query:
            return []
        vec = self.store.vector_top(self.embedder.embed_query(query), POOL, file_kind=kind)
        kw = self.store.keyword_top(keyword_tokens(query), POOL)
        chunks = self.store.chunk_info([h[0] for h in vec] + [r for r in kw if r > 0])
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
            out.append({"file_id": r["file_id"], "chunk_id": r["chunk"], "path": f["path"], "name": f["name"], "kind": f["kind"],
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
        best_z = max(h[2] for h in vec)                      # no "always the closest" here: an unrelated excerpt in a prompt does harm
        hits = [c for c, _, z in vec if _stands_out(z, best_z)][:limit]
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


IMAGE_SIDE = 1024
MIN_IMAGE_SIDE = 64               # icons and spacers
SCAN_PAGES = 20
SAMPLE_RATE = 16000
SEGMENT_SECONDS = 30
MAX_SEGMENTS = 20                 # the first 10 minutes of a file


def _fit(im, side=IMAGE_SIDE):
    im = im.convert("RGB")
    im.thumbnail((side, side))
    return im


def _load_image(p, side=IMAGE_SIDE):
    from PIL import Image
    if p.suffix.lower() == ".heic":
        import pillow_heif
        pillow_heif.register_heif_opener()
    with Image.open(p) as im:
        if min(im.size) < MIN_IMAGE_SIDE:
            return None
        im.draft("RGB", (side, side))                     # lets the JPEG decoder downscale while decoding
        return _fit(im, side)


PDFIUM = threading.Lock()         # pdfium is not thread-safe: the indexer and the preview both render pages


def _pdf_items(p):
    from pypdf import PdfReader
    pages = [(pg.extract_text() or "").strip() for pg in PdfReader(str(p)).pages]
    chars = sum(len(t) for t in pages)
    if pages and chars < 80 * len(pages) and chars < 2000:            # scanned: same test as app.py's attachments
        import pypdfium2 as pdfium
        with PDFIUM:
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


# ---------------------------------------------------------------- indexer

TEXT_BATCH = 32
RESCAN_SECONDS = 300
IDLE_UNLOAD_SECONDS = 600


class EmbedFailure(Exception):
    """The embedder itself raised, as opposed to one file being unreadable."""


class Indexer:
    def __init__(self, store, embedder, config=load_config, app_url=APP_URL):
        self.store, self.embedder, self.config, self.app_url = store, embedder, config, app_url
        # "starting" until the first pass begins: an empty index must not read as "up to date"
        self.state, self.paused_reason, self.current, self.no_access, self.error = "starting", None, None, [], None
        self.problem = None                               # why the last pass stopped early; cleared by the next one
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

    def _embed(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            raise EmbedFailure(f"{type(e).__name__}: {e}"[:300]) from e

    def index_file(self, f):
        """Index one file. Returns "ok", "stopped", "error" (the file is unreadable; recorded) or
        ("embed_failed", message): the embedder raised and the file is left pending."""
        self.current = f["path"]
        self.store.set_meta("in_flight", str(f["id"]))     # if this file kills the process, skip it next time
        try:
            items = extract(f["path"], f["kind"]) if f["kind"] != "name" else []
            rows, texts = [], [i for i in items if i["kind"] == "text"]
            for i in range(0, len(texts), TEXT_BATCH):
                if not self.wait_clear():
                    return "stopped"
                batch = texts[i:i + TEXT_BATCH]
                vecs = self._embed(self.embedder.embed_texts, [doc_text(f["name"], t["text"]) for t in batch])
                rows += [("text", t["text"], t["locator"], v) for t, v in zip(batch, vecs)]
            for item in items:                            # images and audio: one per call
                if item["kind"] == "text":
                    continue
                if not self.wait_clear():
                    return "stopped"
                vec = (self._embed(self.embedder.embed_image, item["image"]) if item["kind"] == "image"
                       else self._embed(self.embedder.embed_audio, item["audio"]))
                rows.append((item["kind"], None, item["locator"], vec))
            self.store.replace_chunks(f["id"], rows)
            return "ok"
        except EmbedFailure as e:
            return "embed_failed", str(e)
        except Exception as e:                            # one unreadable file must not stop the crawl
            self.store.mark_error(f["id"], f"{type(e).__name__}: {e}"[:300])
            return "error"
        finally:
            self.current = None
            self.store.set_meta("in_flight", "")

    def run_once(self):
        crashed = self.store.get_meta("in_flight")
        if crashed:                                       # the process died with this file in flight
            self.store.mark_error(int(crashed), "Indexing this file stopped the service, so it was skipped.")
            self.store.set_meta("in_flight", "")
        cfg, no_access, failed = self.config(), [], []
        found = list(walk(cfg["roots"], cfg["removed"], no_access, failed))
        self.no_access = no_access
        self.store.sync(found, keep_prefixes=failed)
        for f in self.store.pending():
            if self.wake.is_set():                        # folders or settings changed: start over with them
                return
            if not self.wait_clear():
                return
            result = self.index_file(f)
            if isinstance(result, tuple):
                try:                                      # is it this file, or the embedder?
                    self.embedder.embed_query("probe")
                except Exception:
                    self.problem = f"The embedding model is failing ({result[1]}). Indexing will try again in a few minutes."
                    return
                self.store.mark_error(f["id"], result[1])

    def run_pass(self):
        """One crawl-and-index pass. A failure is kept for /status instead of passing for "up to date"."""
        self.problem = None
        try:
            self.run_once()
        except Exception as e:
            self.problem = f"Indexing failed: {type(e).__name__}: {e}"[:300]
            print(self.problem, file=sys.stderr, flush=True)

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            if self.error is None:
                self.run_pass()
            if self.wake.is_set():                        # changed during the pass: go again now
                continue
            self.state, self.paused_reason = "idle", None
            deadline = time.time() + RESCAN_SECONDS
            while not self.stop.is_set() and not self.wake.is_set() and time.time() < deadline:
                if self.embedder.loaded and time.time() - self.embedder.last_used > IDLE_UNLOAD_SECONDS:
                    self.embedder.unload()                # give the memory back until the next query or change
                self.wake.wait(5)

    def status(self):
        return {"state": self.state, "paused_reason": self.paused_reason, "current": self.current,
                "model_loaded": self.embedder.loaded, "roots": self.config()["roots"], "no_access": self.no_access,
                "error": self.error, "problem": self.problem, **self.store.counts()}


# ---------------------------------------------------------------- preview
# Shows a result at the place that matched: the PDF page with the passage marked, the passage in
# context for other documents, the picture, or the audio from the matching moment.

PREVIEW_WIDTH = 1100              # pixels
NEEDLE_CHARS = 200                # the longest line searched for whole
NEEDLE_START = 60                 # if the whole line isn't found (the page's text can differ slightly), look for its start


def _needles(text, limit=24):
    """What to look for on a PDF page to find where a passage sits: each of its lines."""
    out = []
    for line in text.split("\n"):
        line = " ".join(line.split())
        if len(line) >= 8:
            out.append(line[:NEEDLE_CHARS])
        if len(out) == limit:
            break
    return out


def preview_info(store, file_id, chunk_id=None):
    """What the UI needs to preview a result. Without a passage (a match on the file name) it
    describes the beginning of the file."""
    f = store.file_info([file_id]).get(file_id)
    if f is None:
        return None
    c = store.context(chunk_id) if chunk_id else None
    if c is None or c["file_id"] != file_id:              # an id from another file must not bring that file's text
        chunk_id = store.first_chunk(file_id)
        c = store.context(chunk_id) if chunk_id else None
    out = {"file_id": file_id, "name": f["name"], "path": f["path"], "kind": f["kind"], "type": "none",
           "chunk_id": chunk_id, "locator": c["locator"] if c else ""}
    if f["kind"] == "image":
        out["type"] = "image"
    elif f["kind"] == "audio":
        m = re.fullmatch(r"(\d+)s", out["locator"])
        out.update(type="audio", start=int(m.group(1)) if m else 0)
    elif f["kind"] == "document" and f["name"].lower().endswith(".pdf"):
        m = re.fullmatch(r"p(\d+)", out["locator"])
        out.update(type="page", page=int(m.group(1)) if m else 1, match=c["match"] if c else "")
    elif c and c["kind"] == "text":
        out.update(type="text", before=c["before"], match=c["match"], after=c["after"])
    return out


def render_page(path, page, needles=()):
    """One PDF page as a JPEG with the lines in `needles` marked. Returns (bytes, pages in the file,
    lines found). A passage that can't be located still gets its page, unmarked."""
    import io
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw
    with PDFIUM:
        doc = pdfium.PdfDocument(str(path))
        total = len(doc)
        pg = doc[max(1, min(page, total)) - 1]
        width, height = pg.get_size()
        scale = min(PREVIEW_WIDTH / width, 2.5)
        im = pg.render(scale=scale).to_pil().convert("RGB")
        rects = []
        if needles:
            tp = pg.get_textpage()
            for needle in needles:
                hit = tp.search(needle, match_case=False).get_next() or \
                    (len(needle) > NEEDLE_START and tp.search(needle[:NEEDLE_START], match_case=False).get_next())
                if hit:
                    rects += [tp.get_rect(i) for i in range(tp.count_rects(*hit))]
    if rects:
        overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for left, bottom, right, top in rects:            # PDF points, origin bottom-left
            draw.rectangle([left * scale - 2, (height - top) * scale - 2, right * scale + 2, (height - bottom) * scale + 2],
                           fill=(255, 214, 0, 96))
        im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue(), total, len(rects)


def large_image(path, side=1600):
    import io
    im = _load_image(Path(path), side)
    if im is None:
        return None
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


# ---------------------------------------------------------------- HTTP API

def _local_request(host, origin):
    """Only this Mac's own app calls the service: a loopback Host (no DNS rebinding) and no browser
    Origin (app.py calls it server-side, a web page never should)."""
    name = (host or "").rsplit(":", 1)[0].strip("[]")
    return name in ("127.0.0.1", "localhost", "::1") and not origin


def make_app(store, embedder, indexer, searcher):
    import io
    from contextlib import asynccontextmanager
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import PlainTextResponse, Response

    @asynccontextmanager
    async def lifespan(_):
        yield
        shutdown(indexer)                                 # runs on SIGTERM, before uvicorn lets the process die

    api = FastAPI(lifespan=lifespan)

    async def app(scope, receive, send):                  # plain ASGI wrapper: these routes return the user's file contents
        if scope["type"] == "http":
            h = dict(scope["headers"])
            if not _local_request(h.get(b"host", b"").decode("latin-1"), h.get(b"origin", b"").decode("latin-1")):
                return await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)
        await api(scope, receive, send)

    @api.get("/health")
    def health():
        return {"ok": True}

    @api.get("/status")
    def status():
        return indexer.status()

    @api.post("/search")
    def search(body: dict = Body(...)):
        if indexer.error:
            raise HTTPException(503, indexer.error)
        limit = max(1, min(int(body.get("limit") or RESULT_LIMIT), 100))
        return {"results": searcher.search(body.get("query"), body.get("kind") or None, body.get("folder") or None, limit)}

    @api.post("/passages")
    def passages(body: dict = Body(...)):
        if indexer.error:
            raise HTTPException(503, indexer.error)
        return {"passages": searcher.passages(body.get("query"), int(body.get("limit") or 6), int(body.get("max_chars") or 12000))}

    @api.post("/reload")
    def reload():
        indexer.wake.set()
        return {"ok": True}

    @api.get("/errors")
    def errors():
        return {"errors": store.errors()}

    @api.get("/file/{file_id}")
    def file(file_id: int):
        f = store.file_info([file_id]).get(file_id)
        if not f:
            raise HTTPException(404, "not in the index")
        return f

    @api.get("/preview/{file_id}")
    def preview(file_id: int, chunk_id: int | None = None):
        info = preview_info(store, file_id, chunk_id)
        if info is None:
            raise HTTPException(404, "not in the index")
        return info

    @api.get("/page/{file_id}/{page}")
    def page(file_id: int, page: int, chunk_id: int | None = None):
        info = preview_info(store, file_id, chunk_id)
        if info is None or info["type"] != "page":
            raise HTTPException(404, "no page preview for this file")
        marks = _needles(info["match"]) if chunk_id and page == info["page"] else ()      # only the matching page is marked
        try:
            data, total, _ = render_page(info["path"], page, marks)
        except Exception as e:
            raise HTTPException(404, f"This page couldn't be shown: {type(e).__name__}")
        return Response(data, media_type="image/jpeg", headers={"X-Pages": str(total), "Cache-Control": "no-store"})

    @api.get("/image/{file_id}")
    def image(file_id: int):
        f = store.file_info([file_id]).get(file_id)
        try:
            data = large_image(f["path"]) if f and f["kind"] == "image" else None
        except Exception:
            data = None
        if data is None:
            raise HTTPException(404, "image unavailable")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @api.get("/thumb/{file_id}")
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
        failed += _eval_previews(store, searcher)
        return 1 if failed or c["files_error"] else 0


def _eval_previews(store, searcher):
    """The preview for the top result of a few sample queries: the right page, located and rendered."""
    bad = 0
    for query, name, want_type, highlighted in (("warranty period for the washing machine", "warranty.pdf", "page", True),
                                                ("closing balance on my savings account", "bank_statement.pdf", "page", True),
                                                ("scanned parking fine", "scan_0042.pdf", "page", False),
                                                ("cover letter for the nursing job", "cover_letter.docx", "text", None),
                                                ("photo of a desert plant at night", "IMG_2001.jpg", "image", None),
                                                ("voice note about the dentist", "voice_memo_1.aiff", "audio", None)):
        top = searcher.search(query)[0]
        info = preview_info(store, top["file_id"], top["chunk_id"])
        ok, note = top["name"] == name and info["type"] == want_type, ""
        if ok and want_type == "page":
            data, total, marks = render_page(info["path"], info["page"], _needles(info.get("match", "")))
            ok = data[:2] == b"\xff\xd8" and total >= 1 and (marks > 0) == highlighted
            note = f"page {info['page']} of {total}, {len(data) // 1024} KB, {marks} highlighted line(s)"
        elif ok and want_type == "image":
            data = large_image(info["path"])
            ok, note = data[:2] == b"\xff\xd8", f"{len(data) // 1024} KB"
        elif ok and want_type == "text":
            ok, note = "nurse" in info["match"].lower(), f"{len(info['match'])} characters of passage"
        elif ok:
            note = f"starts at {info['start']}s"
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} preview of {name:22s} {info['type']:6s} {note}")
    return bad


def shutdown(indexer):
    """A normal stop (the app quitting) with a file in flight is not a crash: don't leave its marker behind."""
    indexer.stop.set()
    indexer.store.set_meta("in_flight", "")


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
    try:
        uvicorn.run(make_app(store, embedder, indexer, searcher), host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        shutdown(indexer)

if __name__ == "__main__":
    main()
