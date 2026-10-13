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
