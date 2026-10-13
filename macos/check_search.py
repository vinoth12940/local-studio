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


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   search checks OK")
