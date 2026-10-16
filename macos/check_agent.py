"""Build guard for the Local Agent logic in app.py. Run by macos/build.sh, or by hand:

    uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_agent.py [check_name ...]
"""
import json
import os
import sys


def _fake_cua(script_path):
    """A stand-in for `cua-driver mcp`: newline-delimited JSON-RPC on stdio, driven by a JSON script:
    {"log": path to append received lines to, "die_after_calls": N (exit after answering the Nth tools/call),
     "die_without_reply": true (exit on the first tools/call without answering),
     "max_spawns": N (the Nth+1 start exits at once, never answering), "tools": {name: result}}.
    Built in: echo (returns its arguments as JSON text) and hang (never answers)."""
    script = json.load(open(script_path))
    spawn_file = script_path + ".spawns"
    spawns = int(open(spawn_file).read()) + 1 if os.path.exists(spawn_file) else 1
    open(spawn_file, "w").write(str(spawns))
    if spawns > script.get("max_spawns", 10 ** 9):
        sys.exit(1)
    calls = 0
    for line in sys.stdin:
        if script.get("log"):
            open(script["log"], "a").write(line)
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method, params = msg["method"], msg.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-cua", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": []}
        elif method == "tools/call":
            calls += 1
            if script.get("die_without_reply"):
                sys.exit(1)
            name = params["name"]
            if name == "hang":
                continue
            if name == "echo":
                result = {"content": [{"type": "text", "text": json.dumps(params.get("arguments") or {})}]}
            else:
                result = script["tools"][name]
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no such method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
        if script.get("die_after_calls") and method == "tools/call" and calls >= script["die_after_calls"]:
            sys.stdout.close()
            sys.exit(0)


if "--fake-cua" in sys.argv:
    _fake_cua(sys.argv[sys.argv.index("--fake-cua") + 1])
    sys.exit()

import re
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")      # never touch real user data
os.environ["HF_HUB_CACHE"] = str(TMP / "hub")             # a Mac with no models downloaded
(TMP / "state").mkdir()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import base64  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def agent_settings_defaults():
    assert app.load_settings()["agent"] == {"enabled": False, "max_steps": 40, "max_minutes": 15, "tiers": {}}
    assert app.AGENT_SOCK == app.STATE / "data" / "agent" / "cua.sock" and len(str(app.AGENT_SOCK).encode()) < 104
    assert app.AGENT_RUNS == app.STATE / "data" / "agent" / "runs" and app.CUA_MIN == (0, 28, 2)
    app.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    app.SETTINGS.write_text('{"agent": {"enabled": true}}')
    s = app.load_settings()["agent"]
    assert s["enabled"] is True and s["max_steps"] == 40, s
    app.SETTINGS.unlink()


@check
def chat_and_engine_routes_respect_agent_lock():
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    app.agent_lock.acquire(); app.agent_engine.update(mode="text", model_id="qwen3.8-27b")
    try:
        r = c.post("/api/chat", json={"model_id": "gemma-4-12b", "messages": [{"role": "user", "content": "hi"}]})
        assert app.AGENT_BUSY_MSG in r.text and "downloaded" not in r.text, r.text
        assert c.post("/api/engine/stop").status_code == 409
        assert c.post("/api/engine/start").status_code == 409
        assert c.post("/api/image", data={"prompt": "x"}).status_code == 409
        assert c.get("/api/busy").json()["busy"] is True
    finally:
        app.agent_lock.release(); app.agent_engine.update(mode=None, model_id=None)
    assert c.get("/api/busy").json()["busy"] is False


@check
def agent_routes_are_local_only():
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    assert c.get("/api/agent/status", headers={"origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/agent/status").json() == {"enabled": False}


def fake_client(script, **kw):
    """A McpClient on the fake server; `script` is the dict described in _fake_cua."""
    path = TMP / f"fake-{len(list(TMP.glob('fake-*.json')))}.json"
    script = {"log": str(path) + ".log", **script}
    path.write_text(json.dumps(script))
    c = app.McpClient([sys.executable, __file__, "--fake-cua", str(path)],
                      allowed=app.AGENT_CUA_TOOLS | {"echo", "hang", "boom", "shot", "kill_app_probe"}, **kw)
    c.log = Path(script["log"])
    return c


def wait_dead(c, secs=5):
    end = time.time() + secs
    while c.alive and time.time() < end:
        time.sleep(0.02)
    assert not c.alive, "the fake server should have exited"


@check
def mcp_roundtrip():
    c = fake_client({})
    try:
        r = c.call("echo", {"a": 1})
        assert r["ok"] is True and r["data"] == {"a": 1} and r["image"] is None, r
        assert r["text"] == '{"a": 1}'
        init = [json.loads(l) for l in c.log.read_text().splitlines()]
        assert [m["method"] for m in init][:2] == ["initialize", "notifications/initialized"], init
        assert init[0]["params"]["protocolVersion"] == "2025-06-18" and init[0]["params"]["clientInfo"] == {"name": "local-studio", "version": app.APP_VERSION}
        assert "id" not in init[1]
    finally:
        c.close()
    assert not c.alive


@check
def mcp_error_and_image():
    png = b"\x89PNG fake"
    c = fake_client({"tools": {
        "boom": {"content": [{"type": "text", "text": "nope"}], "isError": True},
        "shot": {"content": [{"type": "text", "text": "a"}, {"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"},
                             {"type": "text", "text": "b"}], "structuredContent": {"w": 3}}}})
    try:
        r = c.call("boom", {})
        assert r["ok"] is False and r["text"] == "nope" and r["data"] is None, r
        r = c.call("shot", {})
        assert r["ok"] is True and r["image"] == png and r["text"] == "a\nb" and r["data"] == {"w": 3}, r
    finally:
        c.close()


@check
def mcp_restarts_once():
    c = fake_client({"die_after_calls": 1, "max_spawns": 2})
    try:
        assert c.call("echo", {"n": 1})["data"] == {"n": 1}
        wait_dead(c)
        assert c.call("echo", {"n": 2})["data"] == {"n": 2}          # restarted
        wait_dead(c)
        try:
            c.call("echo", {"n": 3})                                  # the third start never answers
        except app.McpError:
            pass
        else:
            raise AssertionError("expected McpError")
    finally:
        c.close()
    c = fake_client({"die_without_reply": True})                      # dies mid-call: reported, not replayed
    try:
        try:
            c.call("echo", {})
        except app.McpError:
            pass
        else:
            raise AssertionError("expected McpError")
        assert c.log.read_text().count('"tools/call"') == 1
    finally:
        c.close()


@check
def mcp_timeout():
    c = fake_client({})
    try:
        t0 = time.time()
        try:
            c.call("hang", {}, timeout=0.5)
        except app.McpError as e:
            assert "timed out" in str(e), e
        else:
            raise AssertionError("expected McpError")
        assert time.time() - t0 < 3
        assert c.call("echo", {"ok": 1})["data"] == {"ok": 1}          # still usable afterwards
    finally:
        c.close()


@check
def mcp_allowlist():
    c = fake_client({})
    try:
        c.start()
        for name in ("kill_app", "clipboard_read", "set_config"):
            try:
                c.call(name, {})
            except app.McpError as e:
                assert f"{name} is not allowed" in str(e), e
            else:
                raise AssertionError("expected McpError")
        time.sleep(0.2)
        assert "tools/call" not in c.log.read_text()
    finally:
        c.close()


@check
def daemon_pattern_is_ours_only():
    pat = app.agent_daemon_pattern()
    assert re.search(pat, f"/Applications/CuaDriver.app/Contents/MacOS/cua-driver serve --socket {app.AGENT_SOCK} --permission-mode standard")
    assert not re.search(pat, "/Applications/CuaDriver.app/Contents/MacOS/cua-driver serve")      # Hermes's daemon, default socket
    assert not re.search(pat, "cua-driver serve --socket /Users/x/Library/Caches/cua-driver/cua-driver.sock")
    assert {"kill_app", "clipboard_read", "clipboard_write", "set_config", "get_desktop_state", "browser_set_input_files"}.isdisjoint(app.AGENT_CUA_TOOLS)
    assert len(app.AGENT_CUA_TOOLS) == 27 and {"start_session", "get_window_state", "bring_to_front"} <= app.AGENT_CUA_TOOLS
    # a path with spaces (the Mac app's STATE) still matches, and pgrep accepts the pattern
    spaced = "/Users/x/Library/Application Support/Local Studio/data/agent/cua.sock"
    assert re.search("cua-driver serve --socket " + re.escape(spaced), f"cua-driver serve --socket {spaced} --permission-mode standard")
    import subprocess as sp
    assert sp.run(["pgrep", "-f", pat], capture_output=True).returncode == 1        # read-only: nothing of ours is running


class Stub:
    """Replace app.subprocess.run (and the daemon-running probe) for the duration of a check."""

    def __init__(self, responder):
        self.calls, self.responder = [], responder

    def __enter__(self):
        self.real = app.subprocess.run
        app.subprocess.run = self
        return self

    def __exit__(self, *exc):
        app.subprocess.run = self.real

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = self.responder(cmd)
        return type("R", (), {"returncode": out[0], "stdout": out[1], "stderr": ""})()


@check
def cua_status_parses_the_cli():
    def cli(cmd):
        return (0, "cua-driver 0.28.2 \u2014 cross-platform computer-use automation driver\nUSAGE: ...") if "--help" in cmd else (0, "Telemetry: enabled (source: default)\n")
    real = app.CUA_DRIVER
    app.CUA_DRIVER = sys.executable                         # any file that exists
    try:
        with Stub(cli) as s:
            assert app.cua_status() == {"installed": True, "version": "0.28.2", "ok_version": True, "telemetry": True}
            assert all(c[0] == sys.executable for c in s.calls) and len(s.calls) == 2
        with Stub(lambda cmd: (0, "cua-driver 0.28.1 \u2014 x") if "--help" in cmd else (0, "Telemetry: disabled (source: config)")):
            assert app.cua_status() == {"installed": True, "version": "0.28.1", "ok_version": False, "telemetry": False}
        app.CUA_DRIVER = str(TMP / "missing-cua-driver")
        with Stub(lambda cmd: (_ for _ in ()).throw(AssertionError("must not run a missing binary"))):
            assert app.cua_status() == {"installed": False, "version": None, "ok_version": False, "telemetry": None}
    finally:
        app.CUA_DRIVER = real


@check
def daemon_start_and_stop_use_open_and_pkill():
    import threading as th
    app.AGENT_DIR.mkdir(parents=True, exist_ok=True)
    app.AGENT_SOCK.unlink(missing_ok=True)
    state = {"up": False}

    def responder(cmd):
        if cmd[0] == "open":
            state["up"] = True
            th.Timer(0.3, app.AGENT_SOCK.touch).start()             # the daemon creates its socket a moment later
            return 0, ""
        if cmd[0] == "pgrep":
            return (0 if state["up"] else 1), ""
        if cmd[0] == "pkill":
            state["up"] = False
            return 0, ""
        raise AssertionError(cmd)
    with Stub(responder) as s:
        assert app._agent_daemon_running() is False
        app.start_agent_daemon()
        assert app.AGENT_SOCK.exists() and app._agent_daemon_running() is True
        assert ["open", "-n", "-g", "-a", "CuaDriver", "--args", "serve", "--socket", str(app.AGENT_SOCK), "--permission-mode", "standard"] in s.calls
        n = len(s.calls)
        app.start_agent_daemon()                                    # already running: no second launch
        assert not any(c[0] == "open" for c in s.calls[n:])
        app.stop_agent_daemon()
        assert ["pkill", "-f", app.agent_daemon_pattern()] in s.calls
        assert not app.AGENT_SOCK.exists() and app._agent_daemon_running() is False
    # a daemon that never makes its socket is an error, not a hang
    real = app.AGENT_SOCK_WAIT_S
    app.AGENT_SOCK_WAIT_S = 0.3
    try:
        with Stub(lambda cmd: (0, "") if cmd[0] == "open" else (1, "")):
            try:
                app.start_agent_daemon()
            except app.McpError as e:
                assert "socket" in str(e), e
            else:
                raise AssertionError("expected McpError")
    finally:
        app.AGENT_SOCK_WAIT_S = real


@check
def idle_reaper_stops_only_idle_daemons():
    stopped = []
    real_stop, real_last = app.stop_agent_daemon, app._agent_last_use
    app.stop_agent_daemon = lambda: stopped.append(1)
    try:
        app._agent_last_use = None
        assert app.agent_reap(now=10 ** 9) is False and not stopped                       # never used
        app._agent_last_use = 1000.0
        assert app.agent_reap(now=1000.0 + app.AGENT_IDLE_S - 1) is False and not stopped  # recently used
        app.agent_lock.acquire()
        try:
            assert app.agent_reap(now=1000.0 + app.AGENT_IDLE_S + 1) is False and not stopped   # a run is active
        finally:
            app.agent_lock.release()
        assert app.agent_reap(now=1000.0 + app.AGENT_IDLE_S + 1) is True and stopped == [1]
        assert app._agent_last_use is None and app.agent_reap(now=10 ** 9) is False
        t = time.time(); app.agent_touch()
        assert t <= app._agent_last_use <= time.time()
    finally:
        app.stop_agent_daemon, app._agent_last_use = real_stop, real_last


@check
def agent_client_is_a_singleton_that_ensures_the_daemon():
    made, started = [], []
    saved = (app.McpClient, app._agent_daemon_running, app.start_agent_daemon, app._agent_reaper_start, app._agent_cli)
    class Rec:
        def __init__(self, argv, **kw):
            made.append((argv, kw)); self.alive = True
        def close(self):
            self.alive = False
    app.McpClient, app._agent_cli = Rec, None
    app._agent_daemon_running = lambda: False
    app.start_agent_daemon = lambda: started.append(1)
    app._agent_reaper_start = lambda: None
    try:
        a = app.agent_client(); b = app.agent_client()
        assert a is b and len(made) == 1 and len(started) == 2
        assert made[0][0] == [app.CUA_DRIVER, "mcp", "--socket", str(app.AGENT_SOCK)]
        assert made[0][1]["prepare"] is app._ensure_agent_daemon and made[0][1]["touch"] is app.agent_touch
    finally:
        app.McpClient, app._agent_daemon_running, app.start_agent_daemon, app._agent_reaper_start, app._agent_cli = saved


FIX = Path(__file__).resolve().parent / "fixtures" / "agent"


def _fx(name):
    return json.load(open(FIX / f"{name}.json"))


@check
def views_fit_budget():
    for name in ("form", "login", "wikipedia"):
        v = app.browser_view(_fx(f"browser_semantic_{name}"))
        assert app.est_tokens(v["text"]) <= 6000, name
        print(f"      {name}: {app.est_tokens(v['text'])} tokens, {v['interactive']} interactive, sparse={v['sparse']}")
    big = _fx("browser_semantic_wikipedia")
    big = {**big, "refs": big["refs"] * 20, "outline": big["outline"] * 20}
    v = app.browser_view(big)
    assert app.est_tokens(v["text"]) <= 6000 and "more not shown" in v["text"], app.est_tokens(v["text"])
    print(f"      wikipedia x20: {app.est_tokens(v['text'])} tokens, {v['interactive']} interactive")
    n = app.app_view(_fx("window_state_notes"))
    assert app.est_tokens(n["text"]) <= 6000
    print(f"      notes: {app.est_tokens(n['text'])} tokens, {n['interactive']} interactive, sparse={n['sparse']}")
    assert len(app.app_view(_fx("window_state_notes"), budget=300)["text"]) / 3.2 <= 300


@check
def views_keep_interactive_first():
    st = _fx("browser_semantic_login")
    full = app.browser_view(st)
    vis = [r["name"] for r in st["refs"] if r["visibility"] == "in_viewport" and r["role"] in ("textbox", "button", "link")]
    small = app.browser_view(st, budget=110)
    assert app.est_tokens(small["text"]) <= 110
    got = [f["facts"]["name"] for f in small["refs"].values()]
    assert all(n in got for n in vis), (vis, got)
    assert full["text"].count('text "') > small["text"].count('text "') and "more not shown" in small["text"]
    assert len(small["refs"]) < len(full["refs"]) or small["text"].count('text "') == 0
    # app: visible controls survive, frameless (hidden-menu) ones go first
    n = _fx("window_state_notes")
    tiny = app.app_view(n, budget=400)
    assert tiny["refs"] and all(f["facts"]["box"] for f in tiny["refs"].values()), tiny["text"]


@check
def refs_are_short_and_stable():
    for name in ("form", "wikipedia"):
        st = _fx(f"browser_semantic_{name}")
        a, b = app.browser_view(st), app.browser_view(st)
        assert a == b
        assert list(a["refs"]) == [f"e{i}" for i in range(1, len(a["refs"]) + 1)]
        assert all(k in a["text"] for k in ("[e1]", f"[e{len(a['refs'])}]"))
        assert a["refs"]["e1"]["ref"] == st["refs"][0]["ref"]
        order = [e["ref"] for e in a["refs"].values()]
        assert order == sorted(order, key=[r["ref"] for r in st["refs"]].index)    # document order
    n = app.app_view(_fx("window_state_notes"))
    assert list(n["refs"]) == [f"e{i}" for i in range(1, len(n["refs"]) + 1)]
    e1 = n["refs"]["e1"]["ref"]
    assert set(e1) == {"element_index", "window_id", "pid"} and e1["window_id"] == 480 and e1["pid"] == 12282


@check
def facts_from_fixture():
    keys = {"role", "name", "input_type", "autocomplete", "in_form", "is_submit", "href", "box"}
    login, dom = _fx("browser_semantic_login"), _fx("browser_domrefs_login")
    v = app.browser_view(login, dom)
    by = {r["facts"]["name"]: r["facts"] for r in v["refs"].values()}
    assert all(set(r["facts"]) == keys for r in v["refs"].values())
    assert by["Password"]["input_type"] == "password" and by["Password"]["role"] == "textbox"
    assert by["Sign in"]["is_submit"] is True and by["Username or email address"]["input_type"] in (None, "text")
    assert all(r["facts"]["box"] is None for r in v["refs"].values())
    assert "password" in v["text"].split("Password")[1].split("\n")[0]
    # an unrelated button does not inherit submit from a DOM button it was not matched to
    assert by["Manage cookies"]["is_submit"] is False and by["Forgot password?"]["is_submit"] is False
    f = app.browser_view(_fx("browser_semantic_form"))
    fb = {r["facts"]["name"]: r["facts"] for r in f["refs"].values()}
    assert fb["Submit order"]["is_submit"] is True and fb["Submit order"]["in_form"] is True
    assert fb["Customer name:"]["input_type"] is None and fb["Customer name:"]["autocomplete"] is None and fb["Customer name:"]["is_submit"] is False
    assert fb["Customer name:"]["href"] is None and fb["Customer name:"]["box"] is None
    # app: box = frame in screenshot pixels
    n = _fx("window_state_notes")
    av = app.app_view(n)
    wb = n["window_bounds"]; sx, sy = n["screenshot_width"] / wb["width"], n["screenshot_height"] / wb["height"]
    el = next(e for e in n["elements"] if e["element_index"] == 6)
    want = [round((el["frame"]["x"] - wb["x"]) * sx), round((el["frame"]["y"] - wb["y"]) * sy), round(el["frame"]["w"] * sx), round(el["frame"]["h"] * sy)]
    hit = [r for r in av["refs"].values() if r["ref"]["element_index"] == 6]
    assert hit and hit[0]["facts"]["box"] == want and hit[0]["facts"]["role"] == "textfield" and hit[0]["facts"]["name"] == "Text 2", hit
    assert "AXRaise" not in av["text"] and "Notes" in av["text"] and "All iCloud" in av["text"]


@check
def sparse_detection():
    v = app.app_view({"elements": [{"element_index": 0, "role": "AXGroup", "label": "canvas", "actions": [], "frame": {"x": 0, "y": 0, "w": 10, "h": 10}}]})
    assert v["sparse"] is True and v["interactive"] == 0 and v["refs"] == {}
    assert app.browser_view({"page": {"title": "x", "url": "https://x"}, "outline": "", "refs": []})["sparse"] is True
    assert app.browser_view(_fx("browser_semantic_wikipedia"))["sparse"] is False


@check
def unicode_labels():
    st = {"page": {"title": "Café — 東京", "url": "https://x.test/東京"}, "outline": '- button "Café — 東京" [focusable=true]\n',
          "refs": [{"ref": "p1:0", "role": "button", "name": "Café — 東京", "actions": ["click"], "states": {}, "value": None, "visibility": "in_viewport", "frame": "main"}]}
    v = app.browser_view(st)
    assert '[e1] button "Café — 東京"' in v["text"] and v["refs"]["e1"]["facts"]["name"] == "Café — 東京"
    a = app.app_view({"elements": [{"element_index": 3, "role": "AXButton", "label": "Café — 東京", "actions": ["AXPress"]}], "pid": 1, "window_id": 2, "app_name": "Café"})
    assert 'button "Café — 東京"' in a["text"]


@check
def fold_history_budget():
    lines = [f"step {i}: click e{i} -> ok " + "x" * 120 for i in range(200)]
    out = app.fold_history(lines)
    assert app.est_tokens("\n".join(out)) <= 3000 and out[0].startswith("Earlier:")
    assert out[-1] == lines[-1], "newest line is kept whole"
    short = lines[:3]
    assert app.fold_history(short) == short
    huge = ["y" * 50000, "z" * 50000]
    assert app.est_tokens("\n".join(app.fold_history(huge))) <= 3000
    assert app.est_tokens("\n".join(app.fold_history(lines, budget=100))) <= 100


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   agent checks OK")
