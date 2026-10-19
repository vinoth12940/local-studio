"""Search service: cli. Moved verbatim from search.py (P6)."""
import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from studio.search.api import make_app
from studio.search.config import INDEX_DIR
from studio.search.embedder import Embedder
from studio.search.indexer import Indexer, shutdown
from studio.search.preview import _needles, large_image, preview_info, render_page
from studio.search.ranking import Searcher
from studio.search.store import Store

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
        query_seconds = []
        for q in queries:
            query_start = time.perf_counter()
            res = searcher.search(q["query"])
            query_seconds.append(time.perf_counter() - query_start)
            top = res[0] if res else None
            ok = top is not None and top["name"] == q["expect"]
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {q['query']:48s} -> {top['name'] if top else '(nothing)'} "
                  f"{top['score'] if top else ''}{'' if ok else '   expected ' + q['expect']}")
        print(f"{len(queries) - failed}/{len(queries)} queries ranked the expected file first")
        if query_seconds:
            samples = sorted(query_seconds)
            print(f"candidate lookup warm p50={samples[len(samples)//2]*1000:.2f}ms "
                  f"p95={samples[min(len(samples)-1,round(.95*(len(samples)-1)))]*1000:.2f}ms max={samples[-1]*1000:.2f}ms")
        failed += _eval_previews(store, searcher)
        moments = [r[0] for r in store.db.execute("SELECT c.locator FROM chunks c JOIN files f ON f.id = c.file_id "
                                                  "WHERE f.name = 'MOV_0031.mp4' AND c.kind = 'image' ORDER BY c.ord")]
        ok = moments == ["0s", "5s", "10s"]                  # a 12-second clip with a single keyframe
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} video sampled at {moments}")
        return 1 if failed or c["files_error"] else 0


def _eval_previews(store, searcher):
    """The preview for the top result of a few sample queries: the right page, located and rendered."""
    bad = 0
    for query, name, want_type, highlighted in (("warranty period for the washing machine", "warranty.pdf", "page", True),
                                                ("closing balance on my savings account", "bank_statement.pdf", "page", True),
                                                ("scanned parking fine", "scan_0042.pdf", "page", False),
                                                ("cover letter for the nursing job", "cover_letter.docx", "text", None),
                                                ("photo of a desert plant at night", "IMG_2001.jpg", "image", None),
                                                ("voice note about the dentist", "voice_memo_1.aiff", "audio", None),
                                                ("misty blue mountain ridges", "MOV_0031.mp4", "video", None)):
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
