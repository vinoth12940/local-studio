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
