"""Build guard for the search-related logic in app.py. Run by macos/build.sh, or by hand:

    uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_app.py [check_name ...]
"""
import ast
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
import patch_guard  # noqa: E402
import studio.config  # noqa: E402
import studio.services.engines  # noqa: E402
import studio.services.models  # noqa: E402
import studio.services.search  # noqa: E402
from fastapi import HTTPException  # noqa: E402

patch_guard.install(app)                                   # app.X = fake must reach the code that reads X

studio.config.SEARCH_URL = "http://127.0.0.1:9"                     # nothing listens here: the service is "down"
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
    real, studio.services.models._download = studio.services.models._download, lambda mid, parts, kind=None: seen.append((mid, kind, parts))
    try:
        app.start_download(app.search_model())
        app.start_download(app.CATALOG["image"][0])
        app.start_download(app.CATALOG["chat"][0])
        time.sleep(0.2)
    finally:
        studio.services.models._download = real
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
def my_files_uses_tools_without_rewriting_the_question():
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"}, {"role": "user", "content": "When is the I94 and passport expiring?"}]
    import copy
    original = copy.deepcopy(messages)
    result = app._with_agent_note(messages, "America/Chicago", use_files=True)
    assert messages == original and result[1:] == original[1:]
    assert app.FILES_PROMPT in result[0]["content"]
    assert "search_files and read_file" in result[0]["content"] and "does not prove" in result[0]["content"]
    assert "Answer using these excerpts" not in result[0]["content"]


@check
def my_files_preserves_attachment_only_turns():
    messages = [{"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "", "attachments": ["0123456789abcdef"]}]
    result = app._with_agent_note(messages, use_files=True)
    assert result[-1] == messages[-1] and result[1:]==messages


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
    assert c.get("/api/busy", headers={"origin": "https://evil.example"}).status_code == 403, "every route is the app's own (wave 2)"


@check
def private_data_routes_are_local_only():
    """Chats, attachments and the gallery hold the user's data: a page of another site, another local port (same-site) or a
    rebound DNS name gets 403; the app's own page (same-origin, or a navigation: none) still works."""
    from fastapi.testclient import TestClient
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    app.GALLERY.mkdir(parents=True, exist_ok=True)
    (app.GALLERY / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    for path in ("/api/chats", "/api/chats/0123456789abcdef", "/api/gallery", "/gallery/x.png", "/api/attachments/0123456789abcdef/a.png"):
        own = c.get(path).status_code
        assert own != 403, (path, own)
        assert c.get(path, headers={"sec-fetch-site": "same-origin"}).status_code == own, path
        assert c.get(path, headers={"sec-fetch-site": "none"}).status_code == own, path
        assert c.get(path, headers={"host": "evil.example:7860"}).status_code == 403, ("DNS rebinding", path)
        assert c.get(path, headers={"origin": "https://evil.example"}).status_code == 403, path
        for site in ("cross-site", "same-site", "Cross-Site"):
            assert c.get(path, headers={"sec-fetch-site": site}).status_code == 403, (site, path)
    assert c.get("/api/search/status", headers={"sec-fetch-site": "same-site"}).status_code == 403, "a page on another local port"
    assert c.get("/api/agent/status", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.get("/api/busy", headers={"sec-fetch-site": "cross-site"}).status_code == 403, "every route is the app's own (wave 2)"


@check
def every_route_is_local_only():
    """Public DNS names that resolve to 127.0.0.1 (127.0.0.1.nip.io, localtest.me, lvh.me) must not reach any route: the page, storage,
    uninstall, model delete, settings and the gallery all answer 403 to a non-loopback Host or a cross-site/same-site fetch. The app's
    own page and local scripts (loopback Host, no or loopback Origin, Sec-Fetch-Site absent/none/same-origin) still work."""
    from fastapi.testclient import TestClient
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    app.GALLERY.mkdir(parents=True, exist_ok=True)
    (app.GALLERY / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    ran, real = [], app.subprocess.Popen
    app.subprocess.Popen = lambda *a, **k: ran.append(a)              # nothing here may ever start uninstall.sh
    try:
        for host in ("127.0.0.1.nip.io:7860", "localtest.me:7860", "lvh.me", "evil.example:7860", "192.168.1.10:7860"):
            h = {"host": host}
            for method, path in (("GET", "/"), ("GET", "/api/storage"), ("POST", "/api/storage/clear"), ("POST", "/api/uninstall"),
                                 ("DELETE", "/api/models"), ("GET", "/gallery/x.png"), ("GET", "/api/system"),
                                 ("GET", "/api/busy"), ("POST", "/api/models/active"), ("GET", "/no/such/route")):
                r = c.request(method, path, headers=h)
                assert r.status_code == 403, (host, method, path, r.status_code)
        for site in ("cross-site", "same-site"):
            for method, path in (("GET", "/"), ("GET", "/api/storage"), ("POST", "/api/uninstall"), ("GET", "/gallery/x.png")):
                assert c.request(method, path, headers={"sec-fetch-site": site}).status_code == 403, (site, path)
        assert c.post("/api/uninstall", headers={"origin": "https://evil.example"}).status_code == 403
        assert ran == [], ran
        for host in ("127.0.0.1:7860", "localhost:7860", "[::1]:7860"):
            for site in (None, "none", "same-origin"):
                h = {"host": host, **({"sec-fetch-site": site} if site else {})}
                r = c.get("/", headers=h)
                assert r.status_code == 200 and "<html" in r.text.lower(), (host, site, r.status_code)
                assert c.get("/gallery/x.png", headers=h).status_code == 200, (host, site)
                assert c.get("/api/busy", headers={**h, "origin": f"http://{host}"}).status_code == 200, (host, site)
    finally:
        app.subprocess.Popen = real


@check
def local_only_characterization():
    """LocalOnly at the ASGI level, as it behaves today (refactor safety net: these outcomes must not change in the split).
    Covers what the TestClient checks above cannot: no Host at all, websockets, lifespan, header case and padding."""
    def call(scope_type="http", **headers):
        seen, sent = [], []

        async def inner(scope, receive, send):
            seen.append(scope["type"])

        async def receive():
            return {"type": "http.request"}

        async def send(msg):
            sent.append(msg)
        scope = {"type": scope_type, "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]}
        if scope_type == "http":
            scope.update(method="GET", path="/", raw_path=b"/", query_string=b"", root_path="", scheme="http", http_version="1.1")
        asyncio.run(app.LocalOnly(inner)(scope, receive, send))
        if seen:
            return "passed"
        if scope_type == "websocket":
            return sent[0]["type"], sent[0].get("code")
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    assert call() == 403, "no Host header at all"
    assert call(host="127.0.0.1:7860") == "passed", "a local script: loopback Host, no Origin"
    assert call(host="localhost") == "passed" and call(host="[::1]:7860", origin="http://[::1]:7860") == "passed"
    assert call(host="127.0.0.1:7860", origin="http://127.0.0.1:9999") == "passed", \
        "today a loopback Origin on another port passes LocalOnly; browsers mark such a page Sec-Fetch-Site: same-site"
    assert call(host="127.0.0.1:7860", origin="http://127.0.0.1:9999", sec_fetch_site="same-site") == 403
    assert call(host="127.0.0.1:7860", sec_fetch_site=" Cross-Site ") == 403, "Sec-Fetch-Site is trimmed and lower-cased"
    assert call(host="127.0.0.1:7860", sec_fetch_site="same-origin") == "passed"
    assert call(host="127.0.0.1:7860", sec_fetch_site="none") == "passed"
    assert call(host="LOCALHOST:7860") == 403, "today the Host name is compared case-sensitively"
    assert call(host="127.0.0.1.evil.example:7860") == 403 and call(host="evil.example", origin="http://127.0.0.1:7860") == 403
    assert call(host="127.0.0.1:7860", origin="null") == 403 and call(host="127.0.0.1:7860", origin="https://evil.example") == 403
    assert call("websocket", host="127.0.0.1:7860", origin="https://evil.example") == ("websocket.close", 1008)
    assert call("websocket", host="127.0.0.1:7860") == "passed"
    assert call("lifespan") == "passed", "lifespan events are never filtered"


@check
def health_reports_readiness_without_locks():
    """GET /api/health: {ok, version, engine: {mode, model_id}, search: up|down}, answered while every lock is held."""
    import threading
    from fastapi.testclient import TestClient
    import studio.services.engines as engines
    real = engines.engine_state
    engines.engine_state = lambda: {"mode": "text", "model_id": "m1"}
    held = [lk for lk in (app.engine_lock, app.agent_lock, app.image_lock) if lk.acquire(timeout=1)]
    try:
        c = TestClient(app.app, base_url="http://127.0.0.1:7860")
        out = {}
        t = threading.Thread(target=lambda: out.update(r=c.get("/api/health")), daemon=True)
        t.start(); t.join(10)
        assert "r" in out, "health waited on a lock"
        assert out["r"].status_code == 200 and out["r"].json() == {
            "ok": True, "version": app.APP_VERSION, "engine": {"mode": "text", "model_id": "m1"}, "search": "down"}, out["r"].text
        assert c.get("/api/health", headers={"origin": "https://evil.example"}).status_code == 403, "LocalOnly applies"
    finally:
        for lk in held:
            lk.release()
        engines.engine_state = real


@check
def request_log_is_off_by_default_and_minimal_when_on():
    """Flag logging.json_requests: off, nothing is written; on, slow/failing/non-GET requests get one JSON line with no
    query string, body or headers."""
    from fastapi.testclient import TestClient
    log = app.LOGS / "requests.jsonl"
    log.unlink(missing_ok=True)
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    c.put("/api/chats/0123456789abcdef", json={"title": "t", "messages": []})
    assert not log.exists(), "off by default: the old behavior"
    app.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    app.SETTINGS.write_text(json.dumps({"logging": {"json_requests": True}}))
    try:
        c.get("/api/chats")                                                  # fast GET: not logged
        c.put("/api/chats/0123456789abcdef?secret=x", json={"title": "secret title", "messages": []})
        lines = [json.loads(x) for x in log.read_text().splitlines()]
        assert [(e["method"], e["path"], e["status"]) for e in lines] == [("PUT", "/api/chats/0123456789abcdef", 200)], lines
        assert set(lines[0]) == {"ts", "request_id", "method", "path", "status", "ms", "engine"}
        assert "secret" not in log.read_text()
    finally:
        app.SETTINGS.unlink(missing_ok=True)
        log.unlink(missing_ok=True)
        c.delete("/api/chats/0123456789abcdef")


@check
def search_is_not_started_twice():
    """While the service is still loading (process up, /health not yet answering), a second
    "turn on" must not launch another copy."""
    started, real = [], (app._search_running, app._search_process, app.subprocess.Popen)

    class P:
        pid = 4242

    studio.services.search._search_running, app.subprocess.Popen = (lambda: False), (lambda *a, **k: started.append(a) or P())
    try:
        studio.services.search._search_process = lambda: True
        app._start_search()
        assert started == [], "a copy is already starting"
        studio.services.search._search_process = lambda: False
        app._start_search()
        assert len(started) == 1 and started[0][0][-4:] == ["python", "search.py", "--port", "8083"]
    finally:
        studio.services.search._search_running, studio.services.search._search_process, app.subprocess.Popen = real
        (app.LOGS / "search.pid").unlink(missing_ok=True)


@check
def the_page_is_never_served_from_a_stale_cache():
    """After an update the app window must load the new index.html, not a cached copy of the old one."""
    from fastapi.testclient import TestClient
    r = TestClient(app.app, base_url="http://127.0.0.1:7860").get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert r.headers.get("cache-control") == "no-cache", f"cache-control is {r.headers.get('cache-control')!r}"


@check
def ui_modules_are_served_fresh_as_javascript():
    """frontend/ holds the UI's ES modules: index.html loads frontend/main.mjs. Like the page, every module revalidates (an
    updated index.html never runs old modules), is served as JavaScript, and is the app's own (LocalOnly)."""
    from fastapi.testclient import TestClient
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    assert 'type="module" src="frontend/main.mjs"' in c.get("/").text
    for p in sorted((REPO / "frontend").glob("**/*.mjs")):
        url = "/" + p.relative_to(REPO).as_posix()
        r = c.get(url)
        assert r.status_code == 200 and r.content == p.read_bytes(), url
        assert r.headers["content-type"].startswith("text/javascript"), (url, r.headers["content-type"])
        assert r.headers.get("cache-control") == "no-cache", (url, r.headers.get("cache-control"))
        assert c.get(url, headers={"host": "evil.example:7860"}).status_code == 403, url
        assert c.get(url, headers={"sec-fetch-site": "cross-site"}).status_code == 403, url
    assert c.get("/frontend/../app.py").status_code == 404, "nothing outside frontend/"


@check
def search_sets_itself_up_again_after_an_update():
    """A new version can change the search components. With Search already on, the app installs
    them at launch by itself; the user should not have to turn Search off and on."""
    calls, real = [], (app._start_search, app._enable_search, app.model_installed)
    app.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    app.SETTINGS.write_text(json.dumps({"search": {"enabled": True}}))
    studio.services.search._start_search, studio.services.search._enable_search, studio.services.models.model_installed = (lambda: calls.append("start")), (lambda: calls.append("setup")), (lambda m: True)
    try:
        app.SEARCH_DEPS_OK.write_text("the previous version's components")
        app.search_job.clear(); app.search_job.update(status="idle")
        assert app.search_start() == {"status": "setting up"}
        time.sleep(0.2)
        assert calls == ["setup"], calls
        app.SEARCH_DEPS_OK.write_text(hashlib.sha256(app.SEARCH_REQS.read_bytes()).hexdigest())
        assert app.search_start() == {"status": "starting"} and calls == ["setup", "start"]
    finally:
        studio.services.search._start_search, studio.services.search._enable_search, studio.services.models.model_installed = real
        app.SETTINGS.unlink(); app.SEARCH_DEPS_OK.unlink(missing_ok=True)
        app.search_job.clear(); app.search_job.update(status="idle")


@check
def preview_serves_only_indexed_audio():
    """The preview player gets the audio file itself. Its path comes from the index, and only audio is served."""
    from fastapi.testclient import TestClient
    audio = TMP / "memo.wav"
    audio.write_bytes(b"RIFF" + bytes(2000))
    answers = {7: {"path": str(audio), "name": "memo.wav", "kind": "audio", "mtime": 1.0},
               6: {"path": str(audio), "name": "holiday.mp4", "kind": "video", "mtime": 1.0},
               8: {"path": str(TMP / "state" / "data" / "settings.json"), "name": "settings.json", "kind": "document", "mtime": 1.0}}

    class Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b

    def fake(method, url, **kw):
        fid = int(url.rsplit("/", 1)[-1])
        if fid not in answers:
            r = Resp({"detail": "not in the index"}); r.status_code = 404
            return r
        return Resp(answers[fid])

    real, app.httpx.request = app.httpx.request, fake
    try:
        c = TestClient(app.app, base_url="http://127.0.0.1:7860")
        r = c.get("/api/search/media/7", headers={"range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100, (r.status_code, len(r.content))
        assert c.get("/api/search/media/6", headers={"range": "bytes=0-9"}).status_code == 206, "a video plays in the preview too"
        assert c.get("/api/search/media/8").status_code == 404, "a document is not served as media"
        assert c.get("/api/search/media/9").status_code == 404
    finally:
        app.httpx.request = real


@check
def test_runs_use_their_own_search_port():
    """A development server must never reach (or stop) the search service of the app the user is
    running. LOCAL_STUDIO_SEARCH_PORT moves everything: the URL, the launch and the stop patterns."""
    import importlib, subprocess as sp
    code = ("import os, sys; os.environ['LOCAL_STUDIO_SEARCH_PORT'] = '8093'; os.environ['LOCAL_STUDIO_HOME'] = sys.argv[1]; "
            "sys.path.insert(0, '.'); import app; calls = []; "
            "app.subprocess.run = lambda cmd, **k: calls.append(cmd) or type('R', (), {'returncode': 1})(); "
            "import studio.services.search; studio.services.search._search_running = lambda: False; app._stop_search(); app._search_process(); "
            "print(app.SEARCH_URL); print(all('8093' in c[-1] and '8083' not in c[-1] for c in calls), len(calls))")
    out = sp.run([sys.executable, "-c", code, str(TMP / "port-state")], capture_output=True, text=True,
                 cwd=str(Path(__file__).resolve().parent.parent)).stdout.split()
    assert out == ["http://127.0.0.1:8093", "True", "3"], out
    assert app.SEARCH_PORT == 8083, "the default is unchanged"



@check
def engine_status_survives_an_answer_that_is_not_json():
    import httpx
    from fastapi.testclient import TestClient
    real_client, real_state = app.httpx.AsyncClient, app.engine_state
    transport = httpx.MockTransport(lambda request: httpx.Response(502, text="<html>Bad gateway</html>"))
    app.httpx.AsyncClient = lambda *a, **k: real_client(transport=transport, timeout=k.get("timeout"))
    studio.services.engines.engine_state = lambda: {"mode": "text", "model_id": None}
    try:
        r = TestClient(app.app, base_url="http://127.0.0.1:7860").get("/api/llm/metrics")
        assert r.status_code == 503 and r.json()["status"] == "starting", (r.status_code, r.text)
    finally:
        app.httpx.AsyncClient, studio.services.engines.engine_state = real_client, real_state


# ---------------------------------------------------------------- contracts (refactor safety net)
# The studio/ split must not change routes, SSE events or what the check suites patch. Each contract is a committed fixture
# under macos/fixtures/; a change that is meant must update the fixture in the same commit:
#     LOCAL_STUDIO_UPDATE_FIXTURES=1 uv run ... python macos/check_app.py route_contract_is_unchanged sse_contract_is_unchanged
REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "macos" / "fixtures"
UPDATE_FIXTURES = os.environ.get("LOCAL_STUDIO_UPDATE_FIXTURES") == "1"


def _contract(name, actual):
    path = FIXTURES / name
    text = json.dumps(actual, indent=1) + "\n"
    if UPDATE_FIXTURES:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    assert path.exists(), f"{path.relative_to(REPO)} is missing: run with LOCAL_STUDIO_UPDATE_FIXTURES=1 once"
    expected = json.loads(path.read_text())
    if expected != actual:
        import difflib
        diff = "\n".join(difflib.unified_diff(json.dumps(expected, indent=1).splitlines(), json.dumps(actual, indent=1).splitlines(),
                                              f"{name} (committed)", f"{name} (code)", lineterm="", n=2))
        raise AssertionError(f"{name} changed. If this is intended, update the fixture "
                             f"(LOCAL_STUDIO_UPDATE_FIXTURES=1) in the same commit:\n{diff}")


def _flat_routes(routes):
    for r in routes:
        if type(r).__name__ == "_IncludedRouter":                     # FastAPI >= 0.146: an included APIRouter, matched in place
            assert not r.include_context.prefix, "routers are included without a prefix"
            yield from _flat_routes(r.original_router.routes)
        else:
            yield r


def _routes():
    """In order: Starlette matches the first route whose path fits, so moving routes into routers must keep this order."""
    out = []
    for r in _flat_routes(app.app.routes):
        if hasattr(r, "methods") and r.methods is not None:
            out.append({"methods": sorted(r.methods), "path": r.path, "name": r.name})
        else:                                                             # a Mount (/gallery) or a websocket route
            out.append({"kind": type(r).__name__, "path": r.path, "name": r.name})
    return out


def _route_key(r):
    return json.dumps(r, sort_keys=True)


def _may_match_same_request(a, b):
    """Conservative: True when some request could reach both routes (so their order decides which one answers). A Mount
    covers its whole prefix, {x:path} matches any rest, {x} any one segment, and GET also serves HEAD."""
    def segs(p):
        return [x for x in p.split("/") if x]
    if "methods" not in a or "methods" not in b:
        mount, other = (a, b) if "methods" not in a else (b, a)
        return other["path"] == mount["path"] or other["path"].startswith(mount["path"].rstrip("/") + "/") or \
            ("methods" not in other and (mount["path"].startswith(other["path"].rstrip("/") + "/")))
    ma, mb = set(a["methods"]), set(b["methods"])
    if not (ma & mb or ("GET" in ma and "HEAD" in mb) or ("HEAD" in ma and "GET" in mb)):
        return False
    sa, sb = segs(a["path"]), segs(b["path"])
    for i in range(max(len(sa), len(sb))):
        x, y = (sa[i] if i < len(sa) else None), (sb[i] if i < len(sb) else None)
        if (x and x.endswith(":path}")) or (y and y.endswith(":path}")):
            return True
        if x is None or y is None:
            return False
        if not (x.startswith("{") or y.startswith("{") or x == y):
            return False
    return True


def _route_order_problems(expected, actual):
    """Same routes, and every pair that could answer the same request keeps its committed order. Order between routes that
    can never match the same request does not change behavior, so moving routes into per-area routers may change it."""
    exp, act = [_route_key(r) for r in expected], [_route_key(r) for r in actual]
    problems = [f"route removed: {k}" for k in exp if k not in act] + [f"route added: {k}" for k in act if k not in exp]
    if len(set(act)) != len(act):
        problems.append("a route is registered twice")
    if problems:
        return problems
    pos = {k: i for i, k in enumerate(act)}
    for i, a in enumerate(expected):
        for b in expected[i + 1:]:
            if _may_match_same_request(a, b) and pos[_route_key(a)] > pos[_route_key(b)]:
                problems.append(f"order changed for routes that can match the same request: {a['path']} must stay before {b['path']}")
    return problems


@check
def route_contract_is_unchanged():
    routes = _routes()
    assert sum(1 for r in routes if "methods" in r and r["path"].startswith("/api/") or r.get("path") == "/") >= 90, len(routes)
    if UPDATE_FIXTURES:
        _contract("routes.json", routes)
        return
    expected = json.loads((FIXTURES / "routes.json").read_text())
    problems = _route_order_problems(expected, routes)
    assert not problems, "routes.json changed. If this is intended, update the fixture (LOCAL_STUDIO_UPDATE_FIXTURES=1) in the " \
                         "same commit:\n" + "\n".join(problems)


@check
def route_order_check_catches_what_matters():
    """The order rule above: swapping two routes that can match the same request fails; swapping unrelated ones passes."""
    r = lambda m, p, n: {"methods": m, "path": p, "name": n}                                           # noqa: E731
    base = [r(["GET", "HEAD"], "/api/chats/{cid}", "get_chat"), r(["GET", "HEAD"], "/api/chats/recent", "recent"),
            r(["POST"], "/api/x", "x"), {"kind": "Mount", "path": "/gallery", "name": "gallery"},
            r(["GET", "HEAD"], "/gallery/{f}", "g"), r(["GET", "HEAD"], "/api/llm/{route:path}", "llm"),
            r(["GET", "HEAD"], "/api/llm/health/x", "h")]
    assert _route_order_problems(base, base) == []
    assert _route_order_problems(base, [base[1], base[0]] + base[2:]), "a literal and a {param} on the same level overlap"
    assert _route_order_problems(base, base[:2] + [base[3], base[2]] + base[4:]) == [], "unrelated routes may move"
    assert _route_order_problems(base, base[:3] + [base[4], base[3]] + base[5:]), "a mount covers its prefix"
    assert _route_order_problems(base, base[:5] + [base[6], base[5]]), "{route:path} matches any rest"
    assert _route_order_problems(base, base[:-1]), "a removed route fails"
    assert _route_order_problems(base, base + [r(["GET"], "/new", "n")]), "an added route fails"
    assert not _may_match_same_request(r(["POST"], "/a/{x}", "a"), r(["GET"], "/a/b", "b")), "different methods never collide"
    assert _may_match_same_request(r(["HEAD"], "/a/{x}", "a"), r(["GET"], "/a/b", "b")), "GET also serves HEAD"


def _backend_sources():
    return [REPO / "app.py"] + sorted((REPO / "studio").glob("**/*.py"))


# "type" values of dict literals that are not agent-run events: tool schemas, OpenAI content parts, ASGI messages, file entries
_NOT_RUN_EVENTS = {"string", "object", "function", "integer", "boolean", "array", "number", "null", "text", "image_url",
                   "websocket.close", "dir", "file"}


def _dict_shape(d):
    keys = sorted(k.value for k in d.keys if isinstance(k, ast.Constant))
    return keys + (["**"] if any(k is None for k in d.keys) else [])


def _sse_contract():
    chat, run, other = {}, {}, {}
    terminal = []
    for path in _backend_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
                if fn == "_sse" and node.args and isinstance(node.args[0], ast.Dict):
                    d = node.args[0]
                    keys = _dict_shape(d)
                    if len(keys) == 1 and keys[0].startswith("x_"):
                        v = d.values[0]
                        shape = _dict_shape(v) if isinstance(v, ast.Dict) else ["<" + type(v).__name__ + ">"]
                        chat.setdefault(keys[0], [])
                        if shape not in chat[keys[0]]:
                            chat[keys[0]].append(shape)
                    else:
                        other.setdefault("+".join(keys), 0)
                        other["+".join(keys)] += 1
            if isinstance(node, ast.Dict):
                kv = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
                t = kv.get("type")
                if isinstance(t, ast.Constant) and isinstance(t.value, str) and t.value not in _NOT_RUN_EVENTS:
                    shape = [k for k in _dict_shape(node) if k != "type"]
                    run.setdefault(t.value, [])
                    if shape not in run[t.value]:
                        run[t.value].append(shape)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_terminal_stream":
                for d in ast.walk(node):
                    if isinstance(d, ast.Dict) and _dict_shape(d) not in terminal:
                        terminal.append(_dict_shape(d))
    sort = lambda m: {k: sorted(v) for k, v in sorted(m.items())}                                         # noqa: E731
    return {"chat_x_events": sort(chat), "chat_other_payloads": dict(sorted(other.items())),
            "agent_run_events": sort(run), "terminal_stream": sorted(terminal)}


@check
def sse_contract_is_unchanged():
    """Every SSE event key and its fields, as the backend writes them, and the UI still reads each one."""
    contract = _sse_contract()
    _contract("sse_contract.json", contract)
    ui = "\n".join(p.read_text() for p in [REPO / "index.html"] + sorted((REPO / "frontend").glob("**/*.mjs")))
    for key in contract["chat_x_events"]:
        assert f"j.{key}" in ui, f"index.html no longer handles the chat event {key}"
    for kind in contract["agent_run_events"]:
        assert f'e.type === "{kind}"' in ui, f"index.html no longer handles the agent run event {kind}"
    for field in ("j.d", "j.o", "j.exit"):
        assert field in ui, f"index.html no longer reads the terminal stream field {field}"
    # The framing the UI parses byte for byte
    assert app._sse({"x_plan": []}) == 'data: {"x_plan": []}\n\n'
    assert app._agent_sse({"i": 3, "type": "note", "text": "t"}) == 'id: 3\ndata: {"i": 3, "type": "note", "text": "t"}\n\n'


@check
def patch_points_are_inventoried():
    """macos/fixtures/patch_points.json lists every app.X the suites rebind or mutate and where it moves in the split."""
    sys.path.insert(0, str(REPO / "macos"))
    import inventory_patches
    inv = inventory_patches.inventory(REPO)
    missing = [p["name"] for p in inv["points"] if not p["destination"]]
    assert not missing, f"add a destination module for {missing} in macos/inventory_patches.py"
    _contract("patch_points.json", inv)


@check
def rebound_names_are_read_through_their_owner():
    """P1 import rule: a name rebound at run time (a suite's `app.X = fake`, or `global X`) lives in its destination module,
    and other modules read it as owner.X, so the rebind reaches them."""
    sys.path.insert(0, str(REPO / "macos"))
    import inventory_patches
    problems = inventory_patches.import_rule_violations(REPO)
    assert not problems, "\n".join(problems)


@check
def code_root_and_version_stay_in_app_folder():
    """ROOT is the folder of app.py wherever the code that defines it lives (index.html, catalog.json, setup.sh and the search
    service's cwd are found from it), and APP_VERSION is the literal in app.py that build.sh and release.sh read."""
    import re
    app_dir = Path(app.__file__).resolve().parent
    assert app.ROOT == app_dir, (app.ROOT, app_dir)
    literal = re.search(r'^APP_VERSION = "(.*)"$', (app_dir / "app.py").read_text(), re.M).group(1)
    assert app.APP_VERSION == literal
    for mod in [m for name, m in sys.modules.items() if name == "studio" or name.startswith("studio.")]:
        if "ROOT" in vars(mod):
            assert mod.ROOT == app_dir, (mod.__name__, mod.ROOT)
        if "APP_VERSION" in vars(mod):
            assert mod.APP_VERSION == literal, (mod.__name__, mod.APP_VERSION)


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   app checks OK")
