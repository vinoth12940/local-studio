"""Search service: store. Moved verbatim from search.py (P6)."""
import re
import sqlite3
import threading
import time
from pathlib import Path
import numpy as np

from studio.search.config import DIM, MODEL_REPO, _under
from studio.search.ranking import SIGMA_MIN, Z_GROUP_MIN, document_date, keyword_tokens

# ---------------------------------------------------------------- store
# SQLite holds files, chunks, a keyword index and each chunk's vector (float16 on disk). The vectors
# are also kept in one float32 matrix in memory, so a query is a single matrix product.

KIND_ORDER = {"document": 0, "image": 1, "audio": 2, "video": 3, "name": 4}
EXTRACT_VERSION = 3               # bump when a version can read files an earlier one could not: failed files get another go
REDO_KINDS = {3: ("video",)}      # version -> kinds it reads better, indexed again once (3: frames between sparse keyframes)
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
                was = int(row[0]) if row and row[0].isdigit() else 0
                for version, kinds in REDO_KINDS.items():
                    if was < version <= EXTRACT_VERSION and kinds:
                        self.db.execute("UPDATE files SET status='pending' WHERE kind IN (%s)" % ",".join("?" * len(kinds)), kinds)
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

    def sync(self, found, keep_prefixes=(), only_paths=None):
        """Bring the files table in line with a crawl. Files under `keep_prefixes` (folders that
        could not be listed this time) are kept even though the crawl did not see them."""
        with self.lock:
            result = self._sync(found, keep_prefixes, only_paths)
            if result["deleted"] >= VACUUM_AFTER:         # a folder was removed or excluded: hand the space back
                self.db.execute("VACUUM")
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return result

    def _sync(self, found, keep_prefixes, only_paths=None):
        with self.db:
            if only_paths is None:
                rows = self.db.execute("SELECT id, path, size, mtime, kind FROM files").fetchall()
            else:
                rows = []
                paths = list(only_paths)
                for offset in range(0, len(paths), 400):
                    batch = paths[offset:offset+400]
                    rows.extend(self.db.execute("SELECT id, path, size, mtime, kind FROM files WHERE path IN (%s)" % ",".join("?"*len(batch)), batch).fetchall())
            known = {p: (i, s, m, k) for i, p, s, m, k in rows}
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

    def name_matches(self, query, limit=200):
        # I-94 and I94 must match the same identifier, including on older FTS indexes.
        query = re.sub(r"\b([a-z]{1,3})[- ](\d{2,4}[a-z]?)\b", r"\1\2", query.casefold())
        tokens = [t for t in keyword_tokens(query) if t not in {"latest", "recent", "newest", "most", "find"}]
        if not tokens: return []
        compact = "lower(replace(replace(replace(name, '-', ''), '_', ''), ' ', ''))"
        where = " AND ".join(f"{compact} LIKE ? ESCAPE '\\'" for _ in tokens)
        values = ["%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in tokens]
        with self.lock:
            return [row[0] for row in self.db.execute(f"SELECT id FROM files WHERE {where} ORDER BY mtime DESC LIMIT ?", (*values, limit))]

    def text_evidence(self, file_id, max_chars=20000):
        info = self.file_info([file_id]).get(file_id)
        if info is None: return None
        with self.lock:
            rows = self.db.execute("SELECT text, locator FROM chunks WHERE file_id=? AND kind='text' ORDER BY ord LIMIT 30", (file_id,)).fetchall()
        text = "\n\n".join((f"[{loc}]\n" if loc else "") + (content or "") for content, loc in rows)
        return {"file_id": file_id, **info, "text": text[:max_chars], "truncated": len(text) > max_chars or len(rows) == 30,
                **document_date(info["name"], text), "note": "Indexed document text. mtime is a filesystem date, not a document/arrival date."}

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
