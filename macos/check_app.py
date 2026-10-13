"""Build guard for the search-related logic in app.py. Run by macos/build.sh, or by hand:

    uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py [check_name ...]
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")      # never touch real user data
os.environ["HF_HUB_CACHE"] = str(TMP / "hub")             # a Mac with no models downloaded
(TMP / "state").mkdir()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
from fastapi import HTTPException  # noqa: E402

app.SEARCH_URL = "http://127.0.0.1:9"                     # nothing listens here: the service is "down"
CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def _fake_install(repo):
    d = app.HF_HUB / ("models--" + repo.replace("/", "--")) / "snapshots" / "abc"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text("{}")


@check
def settings_defaults():
    assert app.load_settings()["search"] == {"enabled": False, "folders": [], "removed": [], "paused": False, "on_battery": False}
    app.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    app.SETTINGS.write_text(json.dumps({"search": {"enabled": True, "folders": ["/x"]}}))
    s = app.load_settings()["search"]
    assert s["enabled"] is True and s["folders"] == ["/x"] and s["on_battery"] is False, s
    app.SETTINGS.unlink()


@check
def embedder_is_not_a_chat_model():
    m = app.search_model()
    assert m["repo"] == "mlx-community/embeddinggemma-2-8bit" and app._find_any("embeddinggemma-2") is m
    assert m in app.all_models() and m not in app.chat_models()
    before = app.load_settings()["chat_model"]
    app._activate_first("embeddinggemma-2", "search")      # no chat model is installed on this "Mac"
    assert app.load_settings()["chat_model"] == before and not app.SETTINGS.exists()
    app._activate_first("gemma-4-12b", "chat")             # the same call for a chat model does switch: the check is live
    assert app.load_settings()["chat_model"] == "gemma-4-12b"
    app.SETTINGS.unlink()


@check
def embedder_is_a_known_model():
    seen = []
    real, app._download = app._download, lambda mid, parts, kind=None: seen.append((mid, kind, parts))
    try:
        app.start_download(app.search_model())
        app.start_download(app.CATALOG["image"][0])
        app.start_download(app.CATALOG["chat"][0])
        time.sleep(0.2)
    finally:
        app._download = real
        app.downloads.clear()
    assert [(m, k) for m, k, _ in seen] == [("embeddinggemma-2", "search"), (app.CATALOG["image"][0]["id"], "image"),
                                             (app.CATALOG["chat"][0]["id"], "chat")], seen
    assert seen[0][2] == [("mlx-community/embeddinggemma-2-8bit", None)]

    _fake_install("mlx-community/embeddinggemma-2-8bit")
    _fake_install("someone/unrelated-model")
    lib = app.models()
    assert [o["repo"] for o in lib["other"]] == ["someone/unrelated-model"], "the embedder is not an unknown cached repo"
    assert lib["search"][0]["id"] == "embeddinggemma-2" and lib["search"][0]["installed"] is True


@check
def busy_counter():
    async def gen():
        yield "a"
        await asyncio.sleep(30)
        yield "b"

    async def main():
        g = app._counted(gen())
        assert await g.__anext__() == "a" and app.active_chats == 1 and app.api_busy() == {"busy": True}
        await g.aclose()
        assert app.active_chats == 0 and app.api_busy() == {"busy": False}

        async def consume():
            async for _ in app._counted(gen()):
                pass

        t = asyncio.create_task(consume())                 # the user stops a reply: Starlette cancels the stream
        await asyncio.sleep(0.05)
        assert app.active_chats == 1
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert app.active_chats == 0, "a stopped reply must not leave indexing paused"

    asyncio.run(main())
    app.jobs["x"] = {"status": "running"}
    assert app.api_busy() == {"busy": True}
    app.jobs["x"] = {"status": "done"}
    assert app.api_busy() == {"busy": False}
    app.jobs.clear()


@check
def search_status_when_off():
    assert app.search_deps_installed() is False
    app.SEARCH_DEPS_OK.write_text(hashlib.sha256(app.SEARCH_REQS.read_bytes()).hexdigest() + "\n")
    assert app.search_deps_installed() is True
    app.SEARCH_DEPS_OK.write_text("an older requirements file")
    assert app.search_deps_installed() is False
    app.SEARCH_DEPS_OK.unlink()

    s = app.search_status()
    assert s["enabled"] is False and s["running"] is False and s["installed"] is False and s["index_on_disk"] is False
    assert s["model"] == {"name": "EmbeddingGemma 2", "size_gb": 1.3} and s["setup"] == {"status": "idle", "step": None, "error": None}
    assert app.search_start() == {"status": "off"}
    try:
        app.search_query({"query": "anything"})
        raise AssertionError("expected a 503")
    except HTTPException as e:
        assert e.status_code == 503 and "Settings" in e.detail


@check
def search_settings_route():
    folder = TMP / "My Folder"
    folder.mkdir()
    s = app.search_settings({"folders": [str(folder), str(folder)], "removed": ["~/Music"], "on_battery": True, "paused": 1})
    assert s == {"enabled": False, "folders": [str(folder)], "removed": [str(Path.home() / "Music")], "paused": True, "on_battery": True}
    assert app.load_settings()["search"] == s
    assert app.search_settings({"paused": False})["folders"] == [str(folder)], "keys that are not sent are kept"
    for bad in ("relative/path", str(TMP / "does-not-exist")):
        try:
            app.search_settings({"folders": [bad]})
            raise AssertionError("expected a 400")
        except HTTPException as e:
            assert e.status_code == 400
    app.SETTINGS.unlink()


@check
def open_requires_matching_path():
    class Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"path": "/r/now-a-different-file.txt", "name": "now-a-different-file.txt", "kind": "document", "mtime": 1.0}

    real, app.httpx.request = app.httpx.request, lambda *a, **k: Resp()
    try:
        app.search_open(file_id=5, path="/r/the-file-this-chat-cited.txt", reveal=False)
        raise AssertionError("expected a 404")
    except HTTPException as e:
        assert e.status_code == 404
    finally:
        app.httpx.request = real


@check
def file_passages():
    class Resp:
        status_code = 200

        def __init__(self, passages):
            self._p = passages

        def json(self):
            return {"passages": self._p}

    found = [{"file_id": 3, "name": "lease.pdf", "path": "/r/lease.pdf", "locator": "p2", "text": "Rent is 2,450 dollars per month."},
             {"file_id": 4, "name": "notes.txt", "path": "/r/notes.txt", "locator": "", "text": "Bond is four weeks' rent."}]
    calls = []
    real = app.httpx.post
    try:
        app.httpx.post = lambda url, **kw: calls.append((url, kw["json"])) or Resp(found)
        msgs = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"}, {"role": "user", "content": "what is my rent?"}]
        sources = app._add_file_passages(msgs)
        assert calls == [(app.SEARCH_URL + "/passages", {"query": "what is my rent?", "limit": 6, "max_chars": 12000})]
        assert sources == [{"file_id": 3, "name": "lease.pdf", "path": "/r/lease.pdf", "locator": "p2"},
                           {"file_id": 4, "name": "notes.txt", "path": "/r/notes.txt", "locator": ""}]
        text = msgs[-1]["content"]
        assert text.startswith(app.FILES_PROMPT) and text.endswith("Question: what is my rent?")
        assert "[1] lease.pdf (p2)\nRent is 2,450 dollars per month." in text and "[2] notes.txt\nBond is four weeks' rent." in text
        assert msgs[1]["content"] == "earlier question", "only the latest user message is changed"

        app.httpx.post = lambda url, **kw: Resp([])                         # nothing matched
        msgs = [{"role": "user", "content": "hello"}]
        assert app._add_file_passages(msgs) == [] and msgs[0]["content"] == "hello"

        def down(url, **kw):
            raise app.httpx.ConnectError("refused")

        app.httpx.post = down                                               # Search is not running
        assert app._add_file_passages(msgs) == [] and msgs[0]["content"] == "hello"
        assert app._add_file_passages([{"role": "assistant", "content": "no user turn"}]) == []
    finally:
        app.httpx.post = real


# ---- checks added by the final review (each written to fail before its fix)

@check
def my_files_uses_only_the_latest_turn():
    class Resp:
        status_code = 200

        def json(self):
            return {"passages": [{"file_id": 1, "name": "lease.pdf", "path": "/r/lease.pdf", "locator": "", "text": "Rent is 2,450."}]}

    posts, real = [], app.httpx.post
    app.httpx.post = lambda url, **kw: posts.append(kw["json"]["query"]) or Resp()
    try:
        msgs = [{"role": "user", "content": "what is my rent?"}, {"role": "assistant", "content": "2,450 dollars."},
                {"role": "user", "content": "", "attachments": ["0123456789abcdef"]}]      # a photo sent with no text
        assert app._add_file_passages(msgs) == [] and posts == [], "an attachment-only turn must not search with an older question"
        assert msgs[0]["content"] == "what is my rent?", "an earlier turn is never rewritten"
        assert app._add_file_passages([{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]) == [] and posts == []
    finally:
        app.httpx.post = real


@check
def foreign_pages_cannot_drive_search():
    from fastapi.testclient import TestClient
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    assert c.post("/api/search/start").json() == {"status": "off"}
    assert c.post("/api/search/start", headers={"origin": "http://127.0.0.1:7860"}).status_code == 200, "the app's own page"
    assert c.post("/api/search/start", headers={"origin": "https://evil.example"}).status_code == 403, "another web page"
    assert c.post("/api/search/start", headers={"origin": "null"}).status_code == 403
    assert c.post("/api/search/start", headers={"host": "evil.example:7860"}).status_code == 403, "DNS rebinding"
    assert c.post("/api/chat", json={"messages": []}, headers={"origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/busy", headers={"origin": "https://evil.example"}).status_code == 200, "routes without file contents are unchanged"


@check
def search_is_not_started_twice():
    """While the service is still loading (process up, /health not yet answering), a second
    "turn on" must not launch another copy."""
    started, real = [], (app._search_running, app._search_process, app.subprocess.Popen)

    class P:
        pid = 4242

    app._search_running, app.subprocess.Popen = (lambda: False), (lambda *a, **k: started.append(a) or P())
    try:
        app._search_process = lambda: True
        app._start_search()
        assert started == [], "a copy is already starting"
        app._search_process = lambda: False
        app._start_search()
        assert len(started) == 1 and started[0][0][-4:] == ["python", "search.py", "--port", "8083"]
    finally:
        app._search_running, app._search_process, app.subprocess.Popen = real
        (app.LOGS / "search.pid").unlink(missing_ok=True)


@check
def the_page_is_never_served_from_a_stale_cache():
    """After an update the app window must load the new index.html, not a cached copy of the old one."""
    from fastapi.testclient import TestClient
    r = TestClient(app.app, base_url="http://127.0.0.1:7860").get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert r.headers.get("cache-control") == "no-cache", f"cache-control is {r.headers.get('cache-control')!r}"


@check
def search_sets_itself_up_again_after_an_update():
    """A new version can change the search components. With Search already on, the app installs
    them at launch by itself; the user should not have to turn Search off and on."""
    calls, real = [], (app._start_search, app._enable_search, app.model_installed)
    app.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    app.SETTINGS.write_text(json.dumps({"search": {"enabled": True}}))
    app._start_search, app._enable_search, app.model_installed = (lambda: calls.append("start")), (lambda: calls.append("setup")), (lambda m: True)
    try:
        app.SEARCH_DEPS_OK.write_text("the previous version's components")
        app.search_job.clear(); app.search_job.update(status="idle")
        assert app.search_start() == {"status": "setting up"}
        time.sleep(0.2)
        assert calls == ["setup"], calls
        app.SEARCH_DEPS_OK.write_text(hashlib.sha256(app.SEARCH_REQS.read_bytes()).hexdigest())
        assert app.search_start() == {"status": "starting"} and calls == ["setup", "start"]
    finally:
        app._start_search, app._enable_search, app.model_installed = real
        app.SETTINGS.unlink(); app.SEARCH_DEPS_OK.unlink(missing_ok=True)
        app.search_job.clear(); app.search_job.update(status="idle")


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   app checks OK")
