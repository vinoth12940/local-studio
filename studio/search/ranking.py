"""Search service: ranking. Moved verbatim from search.py (P6)."""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from studio.search import config as config_module
from studio.search.config import _under

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



def document_date(name, text):
    """Return explicit document-date evidence, never an expiry or arbitrary maximum year."""
    is_i94 = "i94" in re.sub(r"[^a-z0-9]", "", name.casefold()) or bool(re.search(r"\bi[- ]?94\b", text, re.I))
    labels = (r"most recent date of entry|date of entry|arrival(?:/issued)? date|date of arrival" if is_i94
              else r"document date|date of issue|issue date|issued on|issued|date")
    formats = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%Y %B %d", "%Y %b %d")
    date_pattern = r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}\s+[A-Za-z]{3,9}\s+\d{1,2})"
    matches = []
    normalized = " ".join(text.split())
    for match in re.finditer(r"\b(" + labels + r")\s*[:\-]?\s*(" + date_pattern + r")", normalized, re.I):
        if match[1].casefold() == "date" and re.search(r"\b(expiry|expiration|expired?|due|birth|admit until|valid until)\s*$", normalized[:match.start()], re.I):
            continue
        raw = " ".join(match[2].split())
        for fmt in formats:
            try:
                date = datetime.strptime(raw, fmt).date().isoformat()
                matches.append((date, f"{match[1]}: {raw}"))
                break
            except ValueError: pass
    if matches:
        date, evidence = max(matches)
        return {"document_date": date, "date_precision": "day", "date_source": "document_text", "date_evidence": evidence}
    for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})[-_ .]?([01]\d)[-_ .]?([0-3]\d)(?!\d)", name):
        try:
            date = datetime(int(match[1]), int(match[2]), int(match[3])).date().isoformat()
            return {"document_date": date, "date_precision": "day", "date_source": "filename", "date_evidence": match[0]}
        except ValueError: pass
    years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", name)
    if years:
        return {"document_date": max(years), "date_precision": "year", "date_source": "filename", "date_evidence": max(years)}
    return {"document_date": None, "date_precision": None, "date_source": None, "date_evidence": None}


def read_file_evidence(store, file_id, start_page=1, max_pages=2):
    evidence = store.text_evidence(file_id)
    if evidence is None: return None
    path = Path(evidence["path"])
    if path.suffix.casefold() not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".webp"}:
        return evidence
    if not path.is_file() or path.is_symlink(): raise ValueError("The indexed file is missing or changed; search again")
    if not config_module.DOCUMENT_READER.is_file(): raise ValueError("The on-device document reader is unavailable; scanned pages and filled fields have NOT been verified")
    result = subprocess.run([str(config_module.DOCUMENT_READER), str(path), str(start_page), str(max_pages)], capture_output=True, text=True, timeout=45)
    try: document = json.loads(result.stdout)
    except ValueError: raise ValueError("The document could not be read; this does not mean its fields are blank")
    if result.returncode or document.get("error"): raise ValueError(document.get("error") or "Document reader failed")
    pages = document.get("pages", [])
    text = "\n\n".join(f"[p{page['page']}, {page['method']}]\n{page['text']}" for page in pages)
    if not text.strip() or not any(page.get("text", "").strip() for page in pages):
        raise ValueError("No readable text was recognized on these pages. This does NOT establish that the document or field is blank")
    return {**evidence, "text": text[:16000], "truncated": len(text) > 16000 or document["has_more_pages"],
            **document_date(evidence["name"], text), "pages_total": document["pages_total"], "start_page": start_page,
            "pages_read": len(pages), "has_more_pages": document["has_more_pages"],
            "next_page": start_page + len(pages) if document["has_more_pages"] else None,
            "read_method": sorted({page["method"] for page in pages}),
            "note": "Read from the actual local file, including scanned pages and filled PDF values. OCR may contain errors; check labels and person. I94 personal expiry is Admit Until Date, NEVER the OMB/form expiration. Passport expiry is Date of Expiry, not a visa date. Read further pages/other candidates before claiming a field is absent. Do not infer legal status from these dates."}

class Searcher:
    def __init__(self, store, embedder):
        self.store, self.embedder = store, embedder

    def search(self, query, kind=None, folder=None, limit=RESULT_LIMIT, sort="relevance"):
        query = (query or "").strip()
        if not query:
            return []
        vec = self.store.vector_top(self.embedder.embed_query(query), POOL, file_kind=kind)
        kw = list(dict.fromkeys([-fid for fid in self.store.name_matches(query, POOL)] + self.store.keyword_top(keyword_tokens(query), POOL)))
        chunks = self.store.chunk_info([h[0] for h in vec] + [r for r in kw if r > 0])
        chunk_file = {c: i["file_id"] for c, i in chunks.items()}
        files = self.store.file_info(set(chunk_file.values()) | {-r for r in kw if r < 0})
        if kind:                                          # the vector side is already filtered; filter the keyword side
            kw = [r for r in kw if files.get(-r if r < 0 else chunk_file.get(r), {}).get("kind") == kind]
        out = []
        for r in fuse(vec, kw, chunk_file):
            # Once there are literal matches, recency must compare those candidates. A semantic
            # near-match with a newer date must not outrank the document actually requested.
            if sort == "newest" and kw and not r["kw"]:
                continue
            f = files.get(r["file_id"])
            if f is None or (folder and not _under(f["path"], folder)):
                continue
            c = chunks.get(r["chunk"]) or {}
            out.append({"file_id": r["file_id"], "chunk_id": r["chunk"], "path": f["path"], "name": f["name"], "kind": f["kind"],
                        "score": round(r["vec"], 3) if r["vec"] is not None else None,
                        "snippet": c.get("text") or "", "locator": c.get("locator") or "", "mtime": f["mtime"]})
            if sort == "newest":
                evidence = self.store.text_evidence(r["file_id"])
                out[-1].update({k: evidence[k] for k in ("document_date", "date_precision", "date_source", "date_evidence")})
            if sort != "newest" and len(out) == limit:
                break
        if sort == "newest":
            out.sort(key=lambda row: (row.get("document_date") or "", row.get("mtime") or 0), reverse=True)
        return out[:limit]

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
