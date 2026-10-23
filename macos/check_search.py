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
import studio.search.config as search_config
import studio.search.crawler as search_crawler
import studio.search.extractors as search_extractors
import studio.search.store as search_store
import patch_guard
patch_guard.install(search)

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
        assert got == {"notes.txt": "document", "photo.jpg": "image", "song.mp3": "audio", "movie.mp4": "video",
                       "talk.key": "name", "sub/deep.md": "document", "big.log": "name"}, got
        assert no_access == [str(locked)], no_access
        row = next(search.walk([str(root)]))
        assert isinstance(row[1], int) and isinstance(row[2], float)
    assert search.classify("a.PDF", 10) == "document" and search.classify("a.pdf", 200 * 2 ** 20) == "name"
    assert search._dataless(types.SimpleNamespace(st_flags=0x40000000)) and not search._dataless(types.SimpleNamespace(st_flags=0))
    assert list(search.walk([str(search.STATE / "data")])) == [], "the app's own data is never walked"


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

    # hits are (chunk, score, z): z is how far the score stands out from this query's background
    r = search.fuse([(1, 0.80, 8.0), (3, 0.75, 6.2), (2, 0.70, 5.0), (4, 0.66, 4.8), (5, 0.55, 2.0)], [], chunk_file)
    assert files(r) == [10, 20], files(r)                  # 4.8 is more than Z_MARGIN below the best; 2.0 is background
    assert r[0]["chunk"] == 1 and r[0]["vec"] == 0.80 and r[0]["kw"] is False

    assert files(search.fuse([(1, 0.58, 3.9), (3, 0.57, 3.8), (4, 0.56, 3.7)], [], chunk_file)) == [10], "nothing stands out: the closest file still shows, alone"
    assert files(search.fuse([(1, 0.70, 5.0), (3, 0.69, 4.6), (4, 0.68, 4.4)], [], chunk_file)) == [10, 20], "Z_MIN applies even inside the margin"

    r = search.fuse([(1, 0.80, 8.0)], [4, -40], chunk_file)  # keyword-only files are kept after the vector match
    assert files(r) == [10, 30, 40] and r[1]["kw"] and r[1]["chunk"] == 4 and r[2]["chunk"] is None and r[2]["vec"] is None

    assert files(search.fuse([(1, 0.80, 8.0), (3, 0.79, 7.8)], [3], chunk_file)) == [20, 10], "matching both ways outranks a slightly better vector"
    assert files(search.fuse([(99, 0.90, 9.0), (1, 0.80, 8.0)], [98], chunk_file)) == [10], "ids deleted mid-search are ignored"
    assert len(search.fuse([(1, 0.80, 8.0)], [-i for i in range(100, 130)], chunk_file)) == 31, "every file whose NAME matches is returned"
    many = {**chunk_file, **{200 + i: 300 + i for i in range(30)}}
    assert len(search.fuse([(1, 0.80, 8.0)], list(range(200, 230)), many)) == 11, "files that only mention the word are capped"
    assert search.fuse([], [], chunk_file) == []


@check
def searcher():
    with tempfile.TemporaryDirectory() as d:
        st, s = _library(d)
        r = s.search("how much is the plumber invoice")
        assert r[0]["name"] == "plumber.txt" and r[0]["snippet"].startswith("plumber invoice") and r[0]["locator"] == "p1"
        assert set(r[0]) == {"file_id", "chunk_id", "path", "name", "kind", "score", "snippet", "locator", "mtime"}
        assert st.chunk_info([r[0]["chunk_id"]])[r[0]["chunk_id"]]["text"] == r[0]["snippet"], "chunk_id is the passage shown"
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
                                    "archive.zip": "not text", "long.txt": "\n\n".join(f"paragraph {i} " + "word " * 230 for i in range(300))})
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

        search_extractors.extract = flaky
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
            search_extractors.extract = real


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


# ---- checks added by the final review (each written to fail before its fix)

def _names(rows):
    return sorted(Path(r[0]).name for r in rows)


@check
def overlapping_roots():
    """Adding a folder that contains (or is inside) another indexed folder must not break indexing."""
    with tempfile.TemporaryDirectory() as d:
        root, st, ix, cfg = _tree(d, {"a.txt": "plumber invoice", "work/b.txt": "japan itinerary", "work/deep/c.txt": "dentist"})
        rows = list(search.walk([str(root), str(root / "work"), str(root / "work" / "deep")]))
        assert _names(rows) == ["a.txt", "b.txt", "c.txt"], "each file is yielded once"
        cfg["roots"] = [str(root), str(root / "work")]
        ix.run_once()
        assert st.counts()["files_done"] == 3
        (root / "work" / "new.txt").write_text("a new file in the nested folder")
        ix.run_once()
        assert st.counts()["files_done"] == 4
        assert st.sync([("/r/x.txt", 1, 1.0, "document"), ("/r/x.txt", 1, 1.0, "document")])["new"] == 1, "a repeated path is not an error"


@check
def failed_pass_is_reported():
    with tempfile.TemporaryDirectory() as d:
        _, st, ix, _ = _tree(d, {"a.txt": "plumber invoice"})

        def boom(*a, **k):
            raise RuntimeError("disk is full")

        st.sync = boom
        ix.run_pass()
        assert "disk is full" in (ix.status()["problem"] or ""), "a failed pass must not look like 'up to date'"
        del st.sync
        ix.run_pass()
        assert ix.status()["problem"] is None and st.counts()["files_done"] == 1


@check
def folder_removed_mid_pass():
    """Removing a folder takes effect right away, not after the remaining files have been read."""
    with tempfile.TemporaryDirectory() as d:
        root, st, ix, cfg = _tree(d, {**{f"keep/k{i}.txt": "plumber invoice" for i in range(3)},
                                      **{f"private/p{i:02d}.txt": "secret diary" for i in range(30)}})
        cfg["roots"] = [str(root / "keep"), str(root / "private")]
        seen, real = [], ix.index_file

        def index_file(f):
            seen.append(f["name"])
            if len(seen) == 5:                             # the user removes the folder while it is being indexed
                cfg["roots"] = [str(root / "keep")]
                ix.wake.set()
            return real(f)

        ix.index_file = index_file
        ix.run_once()
        assert len(seen) == 5, f"{len(seen)} files were read after the folder was removed"
        ix.wake.clear()
        ix.run_once()
        assert st.counts()["files_total"] == 3 and all(n.startswith("k") for n in seen[5:])
        hits = search.Searcher(st, ix.embedder).search("secret diary", limit=50)
        assert all(Path(r["path"]).parent.name != "private" for r in hits), "nothing from the removed folder is returned"


@check
def embedder_failure_is_not_a_file_error():
    with tempfile.TemporaryDirectory() as d:
        root, st, ix, _ = _tree(d, {f"f{i}.txt": "plumber invoice" for i in range(6)})
        emb, state = ix.embedder, {"broken": True}
        real_texts, real_query = emb.embed_texts, emb.embed_query

        def texts(t):
            if state["broken"]:
                raise RuntimeError("Metal: out of memory")
            return real_texts(t)

        def query(q):
            if state["broken"]:
                raise RuntimeError("Metal: out of memory")
            return real_query(q)

        emb.embed_texts, emb.embed_query = texts, query
        ix.run_pass()
        c = st.counts()
        assert c["files_error"] == 0 and c["files_done"] == 0, f"a failing embedder must not mark files as unreadable: {c}"
        assert "out of memory" in ix.status()["problem"]
        state["broken"] = False                            # memory comes back: everything indexes without a rebuild
        ix.run_pass()
        assert st.counts()["files_done"] == 6 and ix.status()["problem"] is None

        (root / "poison.txt").write_text("this one file upsets the model")
        emb.embed_texts = lambda t: (_ for _ in ()).throw(ValueError("bad input")) if any("upsets" in x for x in t) else real_texts(t)
        (root / "after.txt").write_text("dentist appointment")
        ix.run_pass()
        c = st.counts()
        assert c["files_error"] == 1 and c["files_done"] == 7, f"one bad file is an error for that file only: {c}"
        assert st.errors()[0]["path"].endswith("poison.txt") and ix.status()["problem"] is None


@check
def crash_on_a_file_does_not_loop():
    with tempfile.TemporaryDirectory() as d:
        root, st, ix, cfg = _tree(d, {"a.txt": "plumber invoice", "killer.pdf.txt": "crashes a native library"})
        st.sync(list(search.walk([str(root)])))             # real sizes and times, as the crawl records them
        killer = next(f for f in st.pending() if f["name"] == "killer.pdf.txt")
        st.set_meta("in_flight", str(killer["id"]))         # the service died while this file was in flight
        ix2 = search.Indexer(search.Store(Path(d) / "index.db"), FakeEmbedder(), config=lambda: cfg, app_url=None)
        calls, real = [], search.extract
        search_extractors.extract = lambda p, k: calls.append(Path(p).name) or real(p, k)
        try:
            ix2.run_once()
        finally:
            search_extractors.extract = real
        assert "killer.pdf.txt" not in calls, "the file that crashed the service is not tried again"
        errs = ix2.store.errors()
        assert len(errs) == 1 and errs[0]["path"].endswith("killer.pdf.txt") and "stopped the service" in errs[0]["error"]
        assert ix2.store.get_meta("in_flight") in (None, "") and ix2.store.counts()["files_done"] == 1


@check
def explicit_folders_win():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d).resolve() / "home"
        for f in ("Documents/a.txt", "Documents/Taxes/2025.txt", "Library/Preferences/p.txt", "Library/CloudStorage/Dropbox/d.txt"):
            (home / f).parent.mkdir(parents=True, exist_ok=True)
            (home / f).write_text("x")
        real_home, search_config.HOME = search.HOME, home
        try:
            assert _names(search.walk([str(home)])) == ["2025.txt", "a.txt"], "walking down from home never enters Library"
            assert _names(search.walk([str(home)], removed=[str(home / "Documents")])) == []
            got = _names(search.walk([str(home / "Documents" / "Taxes")], removed=[str(home / "Documents")]))
            assert got == ["2025.txt"], "a folder the user added is indexed even inside a removed one"
            both = _names(search.walk([str(home), str(home / "Documents" / "Taxes")], removed=[str(home / "Documents")]))
            assert both == ["2025.txt"]
            assert _names(search.walk([str(home / "Library" / "CloudStorage" / "Dropbox")])) == ["d.txt"], "an added cloud-storage folder is walked"
        finally:
            search_config.HOME = real_home


@check
def only_this_mac_may_call():
    ok = search._local_request
    assert ok("127.0.0.1:8083", None) and ok("localhost:8083", "") and ok("127.0.0.1", None)
    assert not ok("evil.example:8083", None), "DNS rebinding: a foreign Host is refused"
    assert not ok("127.0.0.1:8083", "https://evil.example"), "a web page (any Origin) is refused"
    assert not ok("", None) and not ok(None, None)
    # an iframe of /preview/N on a hostile page sends no Origin but does send Sec-Fetch-Site
    assert ok("127.0.0.1:8083", None, None) and ok("127.0.0.1:8083", None, ""), "app.py (httpx) sends no Sec-Fetch headers"
    assert not ok("127.0.0.1:8083", None, "cross-site"), "cross-site is refused"
    assert not ok("127.0.0.1:8083", None, "same-site"), "same-site is refused"
    assert not ok("127.0.0.1:8083", None, "Cross-Site"), "the value is case-insensitive"
    assert ok("127.0.0.1:8083", None, "none") and ok("127.0.0.1:8083", None, "same-origin")
    assert not ok("evil.example:8083", None, "none"), "the Host check still applies"


@check
def icloud_eviction_keeps_the_index():
    with tempfile.TemporaryDirectory() as d:
        st = search.Store(Path(d) / "index.db")
        st.sync([("/c/notes.txt", 10, 1.0, "document")])
        fid = st.pending()[0]["id"]
        st.replace_chunks(fid, [("text", "alpha", "", _unit(1))])
        r = st.sync([("/c/notes.txt", 10, 1.0, "placeholder"), ("/c/never-downloaded.pdf", 5, 1.0, "placeholder")])
        assert r == {"new": 1, "changed": 0, "deleted": 0}
        assert st.counts()["chunks"] == 1 and st.vector_top(_unit(1), 1)[0][1] > 0.99, "an evicted file keeps its passages"
        assert st.pending()[0]["kind"] == "name", "a file that was never downloaded is findable by name only"
        st.replace_chunks(st.pending()[0]["id"], [])
        assert st.sync([("/c/notes.txt", 10, 1.0, "placeholder"), ("/c/never-downloaded.pdf", 5, 1.0, "document")])["changed"] == 1, \
            "once downloaded, a name-only file is indexed properly"
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve()
        (root / "evicted.txt").write_text("x")
        (root / "local.txt").write_text("x")
        real = search._dataless
        try:
            marks = {"evicted.txt"}
            search_crawler._dataless = lambda st_: getattr(st_, "_n", None) in marks
            real_scandir = os.scandir

            class E:
                def __init__(self, e):
                    self._e, self.name, self.path = e, e.name, e.path

                def is_symlink(self):
                    return self._e.is_symlink()

                def is_dir(self, follow_symlinks=False):
                    return self._e.is_dir(follow_symlinks=follow_symlinks)

                def stat(self, follow_symlinks=False):
                    s_ = self._e.stat(follow_symlinks=follow_symlinks)
                    return types.SimpleNamespace(st_mode=s_.st_mode, st_size=s_.st_size, st_mtime=s_.st_mtime, st_flags=0, _n=self.name)

            search.os.scandir = lambda p: [E(e) for e in real_scandir(p)]
            got = {Path(p).name: k for p, _, _, k in search.walk([str(root)])}
        finally:
            search_crawler._dataless, search.os.scandir = real, real_scandir
        assert got == {"evicted.txt": "placeholder", "local.txt": "document"}, got


@check
def unreadable_subfolder_keeps_its_files():
    with tempfile.TemporaryDirectory() as d:
        root, st, ix, cfg = _tree(d, {"a.txt": "plumber invoice", "sub/b.txt": "japan itinerary"})
        cfg["roots"] = [str(root), str(Path(d) / "not-there")]
        ix.run_once()
        assert st.counts()["files_done"] == 2 and ix.no_access == [], "a folder that does not exist is not a permission problem"
        (root / "sub").chmod(0)
        try:
            ix.run_once()
        finally:
            (root / "sub").chmod(0o700)
        assert st.counts()["files_total"] == 2, "files in a folder that became unreadable stay in the index"


@check
def changing_file_does_not_leak():
    with tempfile.TemporaryDirectory() as d:
        st = search.Store(Path(d) / "index.db")
        st.sync([("/r/log.txt", 1, 1.0, "document"), ("/r/keep.txt", 1, 1.0, "document")])
        log, keep = (f["id"] for f in st.pending())
        st.replace_chunks(keep, [("text", "keep me", "", _unit(999))])
        for i in range(20):                                # a log file rewritten on every scan
            st.replace_chunks(log, [("text", f"line {j}", "", _unit(j)) for j in range(200)])
        assert st.n <= 1500, f"{st.n} rows held in memory for 201 live chunks"
        top = st.vector_top(_unit(999), 1)
        assert st.chunk_info([top[0][0]])[top[0][0]]["text"] == "keep me" and top[0][1] > 0.99
        assert len(st.vector_top(_unit(5), 300)) == 201


@check
def found_by_name_before_indexing():
    with tempfile.TemporaryDirectory() as d:
        st = search.Store(Path(d) / "index.db")
        st.sync([("/r/holiday-2024.mp4", 1, 1.0, "name"), ("/r/report.pdf", 1, 1.0, "document")])
        fid = {f["name"]: f["id"] for f in st.pending()}
        assert st.keyword_top(["holiday"], 5) == [-fid["holiday-2024.mp4"]], "a file is findable by name as soon as the crawl has seen it"
        assert st.keyword_top(["report"], 5) == [-fid["report.pdf"]]
        st.replace_chunks(fid["report.pdf"], [("text", "quarterly numbers", "p1", _unit(1))])
        assert st.keyword_top(["report"], 5) == [-fid["report.pdf"]], "and still exactly once afterwards"


# ---- found on the first real index: two Python environments were half of all files

@check
def python_environments_are_skipped():
    """A virtual environment is recognised by what it contains, not by its folder name."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve() / "Desktop"
        files = ["proj/app.py", "proj/notes.md",
                 "proj/newenv/pyvenv.cfg", "proj/newenv/lib/python3.12/site-packages/numpy/core.py", "proj/newenv/bin/activate.sh",
                 "proj/myconda/conda-meta/history.json", "proj/myconda/lib/python3.12/os.py",
                 "elsewhere/site-packages/pkg/mod.py", "elsewhere/dist-packages/pkg/mod.py", "elsewhere/keep.py"]
        for f in files:
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).write_text("x")
        got = sorted(p[len(str(root)) + 1:] for p, _, _, _ in search.walk([str(root)]))
        assert got == ["elsewhere/keep.py", "proj/app.py", "proj/notes.md"], got
        added = sorted(Path(p).name for p, _, _, _ in search.walk([str(root / "proj" / "newenv")]))
        assert "activate.sh" in added, "an environment the user adds on purpose is still indexed"


@check
def a_normal_quit_is_not_a_crash():
    """Quitting the app while a file is being indexed must not record that file as one that crashed the service."""
    with tempfile.TemporaryDirectory() as d:
        _, st, ix, _ = _tree(d, {"a.txt": "plumber invoice"})
        st.set_meta("in_flight", "7")
        search.shutdown(ix)
        assert ix.stop.is_set() and st.get_meta("in_flight") in (None, "")


@check
def copied_node_modules_are_skipped():
    """The contents of a node_modules folder copied somewhere under another name (found at the top
    of an iCloud Drive) are library code. A project the user is working on is never skipped."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d).resolve() / "drive"
        files = ["notes.txt"]
        for pkg in ("es-abstract", "caniuse-lite", "eslint", "aria-query", "resolve", "axe-core"):
            files += [f"{pkg}/package.json", f"{pkg}/index.js", f"{pkg}/lib/rules/a.js", f"{pkg}/README.md"]
        files += ["@swc/core/package.json", "@swc/core/index.js", "@swc/helpers/package.json", "@swc/helpers/h.js"]
        # an installed package often carries its own nested dependencies: still library code
        files += ["eslint-plugin-react/package.json", "eslint-plugin-react/lib/rules/r.js", "eslint-plugin-react/node_modules/resolve/index.js"]
        files += ["my-app/package.json", "my-app/package-lock.json", "my-app/src/app.js"]        # a project with a lockfile
        files += ["site/package.json", "site/main.js", "site/.git/HEAD"]                          # a project under git
        files += ["experiments/tiny/package.json", "experiments/tiny/a.js"]                       # one package.json on its own
        files += ["@home/photos/list.txt"]                                                        # an @folder that is not a package scope
        for f in files:
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).write_text("x")
        got = sorted(p[len(str(root)) + 1:] for p, _, _, _ in search.walk([str(root)]))
        assert got == ["@home/photos/list.txt", "experiments/tiny/a.js", "experiments/tiny/package.json", "my-app/package-lock.json",
                       "my-app/package.json", "my-app/src/app.js", "notes.txt", "site/main.js", "site/package.json"], got
        assert len(list(search.walk([str(root / "eslint")]))) == 4, "a package folder the user adds on purpose is indexed"

        few = Path(d).resolve() / "few"                    # four look-alikes are not enough to call it a copied node_modules
        for pkg in ("a", "b", "c", "d"):
            (few / pkg).mkdir(parents=True)
            (few / pkg / "package.json").write_text("{}")
            (few / pkg / "x.js").write_text("x")
        assert len(list(search.walk([str(few)]))) == 8


# ---- found when the first real index finished

@check
def unreadable_files_are_retried_after_an_upgrade():
    """A file that failed because the app could not read its format yet gets another go when a new
    version can, without the user having to rebuild."""
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "index.db"
        st = search.Store(db)
        st.sync([("/r/statement.pdf", 1, 1.0, "document"), ("/r/ok.txt", 1, 1.0, "document")])
        fid = {f["name"]: f["id"] for f in st.pending()}
        st.mark_error(fid["statement.pdf"], "DependencyError: cryptography>=3.1 is required for AES algorithm")
        st.replace_chunks(fid["ok.txt"], [("text", "fine", "", _unit(1))])
        assert search.Store(db).pending() == [], "same version: a failed file is left alone"
        real = search.EXTRACT_VERSION
        search_store.EXTRACT_VERSION = real + 1
        try:
            st2 = search.Store(db)
            assert [f["name"] for f in st2.pending()] == ["statement.pdf"] and st2.errors() == []
            assert search.Store(db).pending() == [f for f in st2.pending()], "and only once per version"
            st2.mark_error(fid["statement.pdf"], "still broken")
            assert search.Store(db).pending() == []
        finally:
            search_store.EXTRACT_VERSION = real


@check
def index_shrinks_after_a_large_removal():
    with tempfile.TemporaryDirectory() as d:
        st = search.Store(Path(d) / "index.db")
        files = [(f"/r/lib/f{i}.py", 1, 1.0, "document") for i in range(1500)] + [("/r/mine.txt", 1, 1.0, "document")]
        st.sync(files)
        for f in st.pending():
            st.replace_chunks(f["id"], [("text", "some library code " * 20, "", _unit(f["id"]))])
        before = st.counts()["index_bytes"]
        assert st.sync([files[-1]])["deleted"] == 1500
        after = st.counts()["index_bytes"]
        assert after < before * 0.5, f"index is {after} bytes after dropping 1,500 of 1,501 files (was {before})"
        assert st.counts()["chunks"] == 1 and len(search.Store(Path(d) / "index.db").vector_top(_unit(1), 5)) == 1


# ---- found on the real index: typing a name returned the named files plus ~70 unrelated code files

@check
def scores_are_judged_against_their_background():
    """On a real index unrelated text scores ~0.6 against almost any query, so a fixed threshold is
    useless. A score counts when it stands out from the scores of everything else for THAT query."""
    with tempfile.TemporaryDirectory() as d:
        st = search.Store(Path(d) / "index.db")
        st.sync([(f"/r/f{i}.txt", 1, 1.0, "document") for i in range(400)] + [(f"/r/p{i}.jpg", 1, 1.0, "image") for i in range(3)])
        q = _unit(12345)
        near = q + 0.35 * _unit(777); near /= np.linalg.norm(near)
        for f in st.pending():
            if f["kind"] == "image":
                st.replace_chunks(f["id"], [("image", None, "", near if f["name"] == "p0.jpg" else _unit(9000 + f["id"]))])
            else:
                st.replace_chunks(f["id"], [("text", "x", "", near if f["name"] == "f7.txt" else _unit(f["id"]))])
        top = st.vector_top(q, 50)
        assert len(top[0]) == 3, "hits carry (chunk, score, z)"
        z = [h[2] for h in top]
        assert z[0] > 8 and z[1] > 8 and z[2] < search.Z_MIN, z[:4]       # the two planted matches, then background
        kinds = {st.chunk_info([h[0]])[h[0]]["kind"] for h in top[:2]}
        assert kinds == {"text", "image"}, "a kind with only a few chunks is judged against the whole index"


@check
def a_name_finds_the_files_with_that_name():
    with tempfile.TemporaryDirectory() as d:
        st, emb = search.Store(Path(d) / "index.db"), FakeEmbedder()
        named = ["Mithran Visa Page.pdf", "I-539A_Mithran.pdf", "ITIN Application-Mithran (1).pdf", "Mithran Birthcertificate.pdf",
                 "W5850171_MithranPassport.pdf", "I94 - Official Website_mithran.pdf"] + [f"Mithran school form {i}.pdf" for i in range(8)]
        code = [f"module_{i}.js" for i in range(60)]
        st.sync([(f"/r/docs/{n}", 1, 1.0, "document") for n in named] + [(f"/r/code/{n}", 1, 1.0, "document") for n in code])
        for f in st.pending():
            text = "scanned page with stamps and signatures" if f["name"] in named else f"export function handler{f['id']}(request, response) rules"
            st.replace_chunks(f["id"], [("text", text, "p1", emb.embed_texts([search.doc_text(f["name"], text)])[0])])
        got = [r["name"] for r in search.Searcher(st, emb).search("Mithran", limit=50)]
        assert set(named) <= set(got), f"missing: {sorted(set(named) - set(got))}"
        assert not [n for n in got if n in code], "unrelated code files must not be listed for a name"


# ---- preview: show the file at the place that matched

@check
def preview_describes_the_match():
    with tempfile.TemporaryDirectory() as d:
        st = search.Store(Path(d) / "index.db")
        st.sync([("/r/lease.pdf", 1, 1.0, "document"), ("/r/notes.docx", 1, 1.0, "document"), ("/r/scan.pdf", 1, 1.0, "document"),
                 ("/r/pic.heic", 1, 1.0, "image"), ("/r/memo.m4a", 1, 1.0, "audio"), ("/r/clip.mp4", 1, 1.0, "name")])
        fid = {f["name"]: f["id"] for f in st.pending()}
        st.replace_chunks(fid["lease.pdf"], [("text", "page one text", "p1", _unit(1)), ("text", "Rent is 2,450 dollars.\nDue on the first.", "p2", _unit(2)),
                                             ("text", "page three text", "p3", _unit(3))])
        st.replace_chunks(fid["notes.docx"], [("text", "first part", "", _unit(4)), ("text", "second part", "", _unit(5))])
        st.replace_chunks(fid["scan.pdf"], [("image", None, "p1", _unit(6)), ("image", None, "p2", _unit(7))])
        st.replace_chunks(fid["pic.heic"], [("image", None, "", _unit(8))])
        st.replace_chunks(fid["memo.m4a"], [("audio", None, "0s", _unit(9)), ("audio", None, "30s", _unit(10)), ("audio", None, "60s", _unit(11))])
        st.replace_chunks(fid["clip.mp4"], [])
        ids = lambda name: [r[0] for r in st.db.execute("SELECT id FROM chunks WHERE file_id=? ORDER BY ord", (fid[name],))]   # noqa: E731

        c = st.context(ids("lease.pdf")[1])
        assert (c["before"], c["match"], c["after"], c["locator"]) == ("page one text", "Rent is 2,450 dollars.\nDue on the first.", "page three text", "p2")
        assert st.context(ids("lease.pdf")[0])["before"] == "" and st.context(999999) is None

        p = search.preview_info(st, fid["lease.pdf"], ids("lease.pdf")[1])
        assert (p["type"], p["page"], p["chunk_id"], p["name"], p["path"]) == ("page", 2, ids("lease.pdf")[1], "lease.pdf", "/r/lease.pdf")
        p = search.preview_info(st, fid["lease.pdf"])                      # matched by name: open at the beginning
        assert (p["type"], p["page"]) == ("page", 1)
        p = search.preview_info(st, fid["scan.pdf"], ids("scan.pdf")[1])
        assert (p["type"], p["page"]) == ("page", 2)
        p = search.preview_info(st, fid["notes.docx"], ids("notes.docx")[1])
        assert (p["type"], p["before"], p["match"], p["after"]) == ("text", "first part", "second part", "")
        assert search.preview_info(st, fid["notes.docx"])["match"] == "first part"
        assert search.preview_info(st, fid["pic.heic"], ids("pic.heic")[0])["type"] == "image"
        p = search.preview_info(st, fid["memo.m4a"], ids("memo.m4a")[2])
        assert (p["type"], p["start"]) == ("audio", 60)
        assert search.preview_info(st, fid["clip.mp4"])["type"] == "none"
        assert search.preview_info(st, 424242) is None
        # a chunk id from another file must not leak that file's text into this preview
        assert search.preview_info(st, fid["notes.docx"], ids("lease.pdf")[1])["match"] == "first part"

    n = search._needles("Rent is 2,450 dollars per month, due on the first of every month without exception.\n\nshort\n   Bond of   four weeks' rent.  ")
    assert n == ["Rent is 2,450 dollars per month, due on the first of every month without exception.", "Bond of four weeks' rent."], n
    assert search._needles("x" * 500) == ["x" * search.NEEDLE_CHARS], "a very long line is cut"


# ---- video: frames every few seconds plus the soundtrack

@check
def videos_are_indexed_by_picture_and_sound():
    assert search.classify("holiday.MP4", 10) == "video" and search.classify("clip.mov", 5 * 2 ** 30) == "video"
    assert search._frame_times(0) == [0] and search._frame_times(12.4) == [0, 5, 10]
    assert search._frame_times(3600)[-1] == search.VIDEO_SECONDS - search.FRAME_SECONDS and len(search._frame_times(3600)) == 120
    with tempfile.TemporaryDirectory() as d:
        root, st, ix, _ = _tree(d, {"notes.txt": "plumber invoice", "holiday.mp4": "not really a video", "song.mp3": "x"})
        real = search.extract

        def fake(path, kind):                              # stands in for the decoder: two frames and one spoken segment
            if kind == "video":
                return [{"kind": "image", "image": "beach sunset waves", "locator": "0s"},
                        {"kind": "image", "image": "birthday cake candles", "locator": "35s"},
                        {"kind": "audio", "audio": "happy birthday singing", "locator": "30s"}]
            if kind == "audio":
                return [{"kind": "audio", "audio": "piano music", "locator": "0s"}]
            return real(path, kind)

        search_extractors.extract = fake
        try:
            assert [f["kind"] for f in (st.sync(list(search.walk([str(root)]))), st.pending())[1]] == ["document", "audio", "video"], "videos come after audio"
            ix.run_once()
        finally:
            search_extractors.extract = real
        s = search.Searcher(st, ix.embedder)
        hit = s.search("birthday cake candles", kind="video")
        assert [(r["name"], r["kind"], r["locator"], r["snippet"]) for r in hit] == [("holiday.mp4", "video", "35s", "")], hit
        assert s.search("happy birthday singing")[0]["locator"] == "30s", "the soundtrack is searchable too"
        assert s.search("birthday cake", kind="image") == [] or all(r["kind"] == "image" for r in s.search("birthday cake", kind="image"))
        p = search.preview_info(st, hit[0]["file_id"], hit[0]["chunk_id"])
        assert (p["type"], p["start"]) == ("video", 35)

    with tempfile.TemporaryDirectory() as d:               # an index made before videos were read: they were name-only
        st = search.Store(Path(d) / "index.db")
        st.sync([("/r/old.mp4", 5, 1.0, "name")])
        st.replace_chunks(st.pending()[0]["id"], [])
        assert st.pending() == []
        assert st.sync([("/r/old.mp4", 5, 1.0, "video")])["changed"] == 1 and st.pending()[0]["kind"] == "video", "they are picked up without a rebuild"


@check
def a_better_reader_redoes_only_that_kind():
    """When a new version reads one kind of file better (videos with sparse keyframes), files of
    that kind are indexed again by themselves; everything else is left alone."""
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "index.db"
        real = (search.EXTRACT_VERSION, dict(search.REDO_KINDS))
        search_store.EXTRACT_VERSION, search_store.REDO_KINDS = 50, {}
        try:
            st = search.Store(db)
            st.sync([("/r/clip.mp4", 1, 1.0, "video"), ("/r/notes.txt", 1, 1.0, "document")])
            for f in st.pending():
                st.replace_chunks(f["id"], [("image" if f["kind"] == "video" else "text", None if f["kind"] == "video" else "x", "0s", _unit(f["id"]))])
            search_store.EXTRACT_VERSION, search_store.REDO_KINDS = 52, {51: ("video",), 52: ()}
            st2 = search.Store(db)
            assert [f["name"] for f in st2.pending()] == ["clip.mp4"], st2.pending()
            assert st2.counts()["chunks"] == 2, "its old frames stay searchable until it is redone"
            assert search.Store(db).pending() == st2.pending() and search.Store(db).get_meta("extract_version") == "52"
        finally:
            search_store.EXTRACT_VERSION, search_store.REDO_KINDS = real[0], real[1]


@check
def newest_i94_uses_entry_date_not_copy_time_or_expiry():
    with tempfile.TemporaryDirectory() as d:
        st = search.Store(Path(d) / "index.db")
        emb = FakeEmbedder()
        st.sync([("/docs/I-94 2024.pdf", 1, 1900000000.0, "document"),
                 ("/docs/I94 2025.pdf", 1, 1700000000.0, "document"),
                 ("/docs/unrelated 2026.pdf", 1, 1900000001.0, "document")])
        files = {f["name"]: f for f in st.pending()}
        for name, year in (("I-94 2024.pdf", 2024), ("I94 2025.pdf", 2025)):
            text = f"I-94 Arrival Record. Most Recent Date of Entry: 03/15/{year}\nAdmit Until Date: 03/15/2028"
            st.replace_chunks(files[name]["id"], [("text", text, "p1", emb.embed_texts([text])[0])])
        text = "Document Date: 10/17/2026. Unrelated record."
        st.replace_chunks(files["unrelated 2026.pdf"]["id"], [("text", text, "p1", emb.embed_texts([text])[0])])
        # Even a spurious strong semantic hit must not become the newest literal I94 match.
        original_top = st.vector_top
        unrelated = st.first_chunk(files["unrelated 2026.pdf"]["id"])
        st.vector_top = lambda *args, **kwargs: [(unrelated, .9, 8)] + original_top(*args, **kwargs)
        service = search.Searcher(st, emb)
        for query in ("I94", "I-94"):
            result = service.search(query, limit=1, sort="newest")
            assert result[0]["name"] == "I94 2025.pdf", result
            assert result[0]["document_date"] == "2025-03-15" and result[0]["date_source"] == "document_text"
            assert "2028" not in result[0]["date_evidence"]
        evidence = st.text_evidence(files["I94 2025.pdf"]["id"])
        assert evidence["document_date"] == "2025-03-15" and "03/15/2028" in evidence["text"]
        assert st.text_evidence(99999) is None
        st.db.close()


@check
def read_file_uses_the_actual_scanned_or_filled_document():
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/"I94 2029.pdf"; path.write_bytes(b"synthetic PDF stub")
        st=search.Store(Path(d)/"index.db")
        st.sync([(str(path),path.stat().st_size,1.0,"document")])
        fid=st.pending()[0]["id"]
        reader, run=search.DOCUMENT_READER, search.subprocess.run
        search_config.DOCUMENT_READER=path
        calls=[]
        def native(args, **kw):
            calls.append(args)
            value={"pages_total":3,"has_more_pages":True,"pages":[{"page":1,"method":"on_device_ocr","text":"Arrival/Issued Date: 2029 June 29\nAdmit Until Date: 2030 April 10\nOMB Expiration Date: 01/31/2024"}]}
            return types.SimpleNamespace(returncode=0,stdout=json.dumps(value))
        search.subprocess.run=native
        try:
            evidence=search.read_file_evidence(st,fid,1,1)
            assert calls[0]==[str(path),str(path),"1","1"]
            assert evidence["document_date"]=="2029-06-29" and evidence["next_page"]==2
            assert evidence["truncated"] and evidence["read_method"]==["on_device_ocr"]
            assert "Admit Until" in evidence["text"] and "NEVER the OMB" in evidence["note"]
            search.subprocess.run=lambda *a,**kw:types.SimpleNamespace(returncode=0,stdout=json.dumps({"pages_total":1,"has_more_pages":False,"pages":[{"page":1,"method":"on_device_ocr","text":""}]}))
            try: search.read_file_evidence(st,fid)
            except ValueError as e: assert "does NOT establish" in str(e)
            else: raise AssertionError("Empty OCR must not silently become a blank-document claim")
        finally:
            search_config.DOCUMENT_READER,search.subprocess.run=reader,run
            st.db.close()


@check
def document_dates_keep_their_evidence_and_uncertainty():
    assert search.document_date("I94.pdf", "Admit Until Date: 03/15/2028")["document_date"] is None
    assert search.document_date("record.pdf", "Issued on: 03/15/2025. Expiration Date: 03/15/2028")["document_date"] == "2025-03-15"
    assert search.document_date("record.pdf", "Birth Date: 01/01/2000. Expiry Date: 03/15/2028")["document_date"] is None
    assert search.document_date("I94.pdf", "Most Recent Date of Entry:\n2025 March 15")["document_date"] == "2025-03-15"
    year = search.document_date("I94 2025.pdf", "scanned page without recognized dates")
    assert year["document_date"] == "2025" and year["date_precision"] == "year" and year["date_source"] == "filename"
    explicit = search.document_date("invoice 20251017.pdf", "")
    assert explicit["document_date"] == "2025-10-17" and explicit["date_source"] == "filename"



@check
def incremental_edits_are_debounced_and_do_not_reembed_unchanged_files():
    with tempfile.TemporaryDirectory() as d:
        root,st,ix,_=_tree(d,{'one.txt':'plumber invoice','two.txt':'tokyo itinerary'})
        original_config=ix.config
        ix.config=lambda:{**original_config(),'incremental':True}
        ix.run_once()
        calls=[];real=search_extractors.extract
        search_extractors.extract=lambda path,kind:calls.append(Path(path).name) or real(path,kind)
        try:
            (root/'one.txt').write_text('electrician quote updated')
            assert ix.poll_changes(now=10)==[] and calls==[]
            (root/'one.txt').write_text('electrician quote final revision')
            assert ix.poll_changes(now=10.2)==[] and calls==[]
            result=ix.poll_changes(now=11)
            assert result==[str(root/'one.txt')] and calls==['one.txt'],(result,calls)
            assert st.counts()['files_done']==2
            assert search.Searcher(st,ix.embedder).search('electrician final revision')[0]['name']=='one.txt'
            assert ix.poll_changes(now=12)==[] and calls==['one.txt']
            (root/'two.txt').unlink();ix.poll_changes(now=13);ix.poll_changes(now=14)
            assert st.counts()['files_total']==1 and calls==['one.txt']
            (root/'new.txt').write_text('dentist appointment');ix.poll_changes(now=15);ix.poll_changes(now=16)
            assert calls==['one.txt','new.txt'] and st.counts()['files_done']==2
            offline=root/'offline';offline.mkdir();(offline/'old.txt').write_text('keep this evidence')
            ix.poll_changes(now=17);ix.poll_changes(now=18)
            offline.chmod(0)
            try:
                ix.poll_changes(now=19);ix.poll_changes(now=20)
                assert st.counts()['files_total']==3, 'unreadable directories must retain indexed files'
            finally:offline.chmod(0o700)
            # A full pass is visibly busy for the entire pass, and always clears its flag.
            real_run=ix.run_once
            def full():
                assert ix.full_indexing
                raise RuntimeError('synthetic full-index failure')
            ix.run_once=full;ix.run_pass();assert not ix.full_indexing and ix.problem
            ix.run_once=real_run
        finally:search_extractors.extract=real

@check
def incremental_queued_edits_survive_a_disconnected_folder():
    with tempfile.TemporaryDirectory() as d:
        root, st, ix, cfg = _tree(d, {'volume/notes.txt': 'original indexed evidence'})
        cfg['incremental'] = True
        volume, disconnected = root / 'volume', Path(d) / 'disconnected'
        cfg['roots'] = [str(volume)]  # The configured mount disappears; it was not deleted from a readable parent.
        path = volume / 'notes.txt'
        try:
            ix.run_pass()
            fid = st.db.execute("SELECT id FROM files WHERE path=?", (str(path),)).fetchone()[0]
            original = st.text_evidence(fid)['text']
            path.write_text('updated evidence after reconnection')
            assert ix.poll_changes(now=10) == []
            volume.rename(disconnected)
            for now in (11, 12):
                assert ix.poll_changes(now=now) == []
                assert st.text_evidence(fid)['text'] == original
                assert st.errors() == [], 'a disconnected folder must not invalidate queued files'
            disconnected.rename(volume)
            assert ix.poll_changes(now=13) == [str(path)]
            assert 'updated evidence after reconnection' in st.text_evidence(fid)['text']
            assert ix.poll_changes(now=14) == []
            assert st.counts()['files_done'] == 1 and st.errors() == []
        finally:
            st.db.close()


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   search checks OK")
