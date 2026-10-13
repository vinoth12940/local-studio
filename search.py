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
