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
    keys = {"role", "name", "input_type", "autocomplete", "in_form", "is_submit", "href", "box", "sensitive_unknown"}
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


@check
def unmatched_sensitive_fields_fail_safe():
    login, dom = _fx("browser_semantic_login"), _fx("browser_domrefs_login")
    for nm in ("Mot de passe", "Contraseña", "Passwort", "PIN", "Your secret", "", None):
        st = json.loads(json.dumps(login))
        next(r for r in st["refs"] if r["name"] == "Password")["name"] = nm
        v = app.browser_view(st, dom)
        f = next(x["facts"] for x in v["refs"].values() if x["ref"] == "p9:1")
        assert f["input_type"] == "password" or f["sensitive_unknown"] is True, (nm, f)
        if nm in ("Your secret", "", None):
            assert f["sensitive_unknown"] is True and "= " not in next(l for l in v["text"].splitlines() if l.startswith("[e2]"))
        assert all(set(x["facts"]) >= {"sensitive_unknown"} for x in v["refs"].values())
    # a matched field is not "unknown", and a page with no sensitive input flags nothing
    ok = app.browser_view(login, dom)
    assert {x["facts"]["name"]: x["facts"]["sensitive_unknown"] for x in ok["refs"].values()}["Password"] is False
    assert not any(x["facts"]["sensitive_unknown"] for x in app.browser_view(_fx("browser_semantic_form"))["refs"].values())
    assert not any(x["facts"]["sensitive_unknown"] for x in app.browser_view(login)["refs"].values()), "no DOM view, nothing to infer from"
    # names alone resolve the obvious ones
    for nm, ac in (("Verification code", "one-time-code"), ("Enter OTP", "one-time-code"), ("Card number", "cc-number"), ("CVV", "cc-csc"), ("Expiry date", "cc-exp")):
        st = {"page": {"title": "t", "url": "u"}, "outline": "", "refs": [{"ref": "p1:0", "role": "textbox", "name": nm, "actions": ["type"], "states": {}, "value": None, "visibility": "in_viewport"}]}
        assert app.browser_view(st)["refs"]["e1"]["facts"]["autocomplete"] == ac, nm
    # DOM autocomplete counts as sensitive evidence even without a password input
    d2 = {"refs": [{"node": "input", "label": "name=otp autocomplete=one-time-code type=text"}]}
    st = {"page": {"title": "t", "url": "u"}, "outline": "", "refs": [{"ref": "p1:0", "role": "textbox", "name": "Zzz", "actions": ["type"], "states": {}, "value": None, "visibility": "in_viewport"}]}
    assert app.browser_view(st, d2)["refs"]["e1"]["facts"]["autocomplete"] == "one-time-code"       # joined by order: one and one
    st["refs"].append({**st["refs"][0], "ref": "p1:1", "name": "Qqq"})
    assert [x["facts"]["sensitive_unknown"] for x in app.browser_view(st, d2)["refs"].values()] == [True, True]   # two vs one: no join


@check
def dom_attrs_cannot_be_spoofed():
    a = app._dom_attrs("name=password id=password type=password value=x type=text")
    assert a["type"] == "password" and a["value"] == "x type=text" and a["name"] == "password", a
    assert app._dom_attrs("type=text name=a type=password")["type"] == "password"
    assert app._dom_attrs("name=a type=text autocomplete=off autocomplete=one-time-code")["autocomplete"] == "one-time-code"
    assert app._dom_attrs("aria-label=Dismiss this message type=button") == {"aria-label": "Dismiss this message", "type": "button"}
    assert app._dom_attrs(None) == {}
    # autocomplete is a token list: any sensitive token counts, wherever it sits
    for ac in ("billing cc-number", "section-x cc-number", "current-password webauthn", "ON  Cc-Exp"):
        assert app._sensitive_ac(ac), ac
        got = app._dom_attrs(f"name=a type=text autocomplete={ac} id=z")["autocomplete"]
        assert app._sensitive_ac(got), (ac, got)
        assert app._dom_sensitive({"refs": [{"node": "input", "label": f"name=a type=text autocomplete={ac}"}]}), ac
    assert not app._sensitive_ac("off") and not app._sensitive_ac("section-a email") and not app._sensitive_ac(None)
    assert not app._dom_sensitive({"refs": [{"node": "input", "label": "name=a type=text autocomplete=section-a email"}]})
    # a DOM autocomplete with no sensitive token does not switch off the "Card number" name fallback
    st = {"page": {"title": "t", "url": "u"}, "outline": "", "refs": [
        {"ref": "p1:0", "role": "textbox", "name": "Card number", "actions": ["type"], "states": {}, "value": None, "visibility": "in_viewport"},
        {"ref": "p1:1", "role": "textbox", "name": "Verification code", "actions": ["type"], "states": {}, "value": None, "visibility": "in_viewport"},
        {"ref": "p1:2", "role": "textbox", "name": "Nickname", "actions": ["type"], "states": {}, "value": None, "visibility": "in_viewport"}]}
    d = {"refs": [{"node": "input", "label": "aria-label=Card number type=text autocomplete=off"},
                  {"node": "input", "label": "aria-label=Verification code type=text autocomplete=on"},
                  {"node": "input", "label": "aria-label=Nickname type=text autocomplete=nickname"}]}
    fs = {r["facts"]["name"]: r["facts"] for r in app.browser_view(st, d)["refs"].values()}
    assert fs["Card number"]["autocomplete"] == "cc-number" and fs["Verification code"]["autocomplete"] == "one-time-code"
    assert fs["Nickname"]["autocomplete"] == "nickname"
    d2 = {"refs": [{"node": "input", "label": "aria-label=Card number type=text autocomplete=billing cc-number"}]}
    assert app.browser_view(st, d2)["refs"]["e1"]["facts"]["autocomplete"] == "billing cc-number"


@check
def buttons_in_forms_default_to_submit():
    f = {r["facts"]["name"]: r["facts"] for r in app.browser_view(_fx("browser_semantic_form"))["refs"].values()}
    assert f["Show time picker Show time picker"]["in_form"] is True and f["Show time picker Show time picker"]["is_submit"] is True
    login, dom = _fx("browser_semantic_login"), _fx("browser_domrefs_login")
    g = {r["facts"]["name"]: r["facts"] for r in app.browser_view(login, dom)["refs"].values()}
    assert g["Manage cookies"]["in_form"] is False and g["Manage cookies"]["is_submit"] is False
    for nm in ("Pay now", "Buy", "Purchase", "Sign up", "Register", "Create account"):
        st = {"page": {"title": "t", "url": "u"}, "outline": "", "refs": [{"ref": "p1:0", "role": "button", "name": nm, "actions": ["click"], "states": {}, "value": None, "visibility": "in_viewport"}]}
        assert app.browser_view(st)["refs"]["e1"]["facts"]["is_submit"] is True, nm
    # a DOM type=button inside a form is not a submit
    st = {"page": {"title": "t", "url": "u"}, "outline": "- form\n  - button \"Menu\"\n", "refs": [{"ref": "p1:0", "role": "button", "name": "Menu", "actions": ["click"], "states": {}, "value": None, "visibility": "in_viewport"}]}
    d = {"refs": [{"node": "button", "label": "aria-label=Menu type=button"}]}
    assert app.browser_view(st)["refs"]["e1"]["facts"]["is_submit"] is True
    assert app.browser_view(st, d)["refs"]["e1"]["facts"]["is_submit"] is False


def _gf(role="button", name="", **kw):
    return app._facts(role, name, **kw)


def _gc(**kw):
    c = {"surface": "browser", "app": None, "tier": None, "domain": "example.com",
         "task_domains": {"example.com"}, "link_domains": {"docs.other.org"}, "url_from_page": False}
    c.update(kw)
    return c


def _ac(tier="allowed", app_name="Notes"):
    return _gc(surface="app", app=app_name, tier=tier, domain=None)


@check
def app_tiers():
    t = app.app_tier
    assert t("Chase Bank", {}) == "read" and t("Crypto Wallet", {}) == "read" and t("MyFinance", {}) == "read"
    assert t("Keychain Access", {}) == "read" and t("System Settings", {}) == "read" and t("iTerm2", {}) == "read"
    assert t("1Password", {}) == "read" and t("Terminal.app", {}) == "read"
    assert t("Mail", {}) == "ask" and t("Slack", {}) == "ask" and t("finder", {}) == "ask"
    assert t("Notes", {}) == "allowed" and t("Safari", {}) == "allowed"
    assert t("Mail", {"Mail": "allowed"}) == "allowed" and t("Notes", {"Notes": "ask"}) == "ask"
    assert t("Chase Bank", {"Chase Bank": "allowed"}) == "allowed"
    for n in ("Chase", "PayPal", "Coinbase", "Robinhood", "LastPass", "Dashlane", "Ghostty", "kitty", "Alacritty", "WezTerm",
              "Shortcuts", "Script Editor", "Automator", "E*TRADE", "Proton Pass", "KeePassXC", "Fidelity Investments", "My Broker",
              "Trading View", "Apple Pay", "Banking", "Tabby", "Hyper"):
        assert t(n, {}) == "read", n
    for n in ("Telegram", "Discord", "Signal", "Microsoft Teams", "Skype", "LINE", "WeChat", "Zoom", "zoom.us"):
        assert t(n, {}) == "ask", n
    assert t("Notes", {}) == "allowed" and t("Pages", {}) == "allowed" and t("Preview", {}) == "allowed"
    assert t("Mail", {"Mail": "bogus"}) == "ask"                       # a junk override is ignored
    assert app.RISKY_WORDS.search("Place order") and not app.RISKY_WORDS.search("Bold")


@check
def gate_table():
    G = app.gate
    link = {"docs.other.org"}
    rows = [
        # (label, tool, args, facts, ctx, expected)
        ("submit button", "click", {"ref": "e1"}, _gf(name="Go", is_submit=True), _gc(), "risky"),
        ("risky link", "click", {"ref": "e1"}, _gf("link", "Place order"), _gc(), "risky"),
        ("risky word button", "click", {"ref": "e1"}, _gf(name="Delete account"), _gc(), "risky"),
        ("plain link", "click", {"ref": "e1"}, _gf("link", "Pricing"), _gc(), "safe"),
        ("plain button", "click", {"ref": "e1"}, _gf(name="Next page"), _gc(), "safe"),
        ("click without facts", "click", {"ref": "e9"}, None, _gc(), "risky"),
        ("submit in a form", "type", {"ref": "e2", "text": "hi", "submit": True}, _gf("textbox", "Message", in_form=True), _gc(), "risky"),
        ("submit outside a form", "type", {"ref": "e2", "text": "hi", "submit": True}, _gf("textbox", "Message"), _gc(), "risky"),
        ("type no submit in a form", "type", {"ref": "e2", "text": "hi", "submit": False}, _gf("textbox", "Message", in_form=True), _gc(), "safe"),
        ("search submit by role", "type", {"ref": "e2", "text": "cats", "submit": True}, _gf("searchbox", "", in_form=True), _gc(), "safe"),
        ("search submit by name", "type", {"ref": "e2", "text": "cats", "submit": True}, _gf("textbox", "Search products", in_form=True), _gc(), "safe"),
        ("find submit by name", "type", {"ref": "e2", "text": "cats", "submit": True}, _gf("textbox", "Find a store", in_form=True), _gc(), "safe"),
        ("query is not a search field", "type", {"ref": "e2", "text": "cats", "submit": True}, _gf("textbox", "Your QUERY", in_form=True), _gc(), "risky"),
        ("Research is not a search field", "type", {"ref": "e2", "text": "cats", "submit": True}, _gf("textbox", "Research topic", in_form=True), _gc(), "risky"),
        ("search name with leading space", "type", {"ref": "e2", "text": "cats", "submit": True}, _gf("textbox", "  SEARCH the web", in_form=True), _gc(), "safe"),
        ("search with newline text", "type", {"ref": "e2", "text": "cats\n"}, _gf("searchbox", "", in_form=True), _gc(), "safe"),
        ("newline text, no submit, in a form", "type", {"ref": "e2", "text": "hi\n", "submit": False}, _gf("textbox", "Message", in_form=True), _gc(), "risky"),
        ("newline text outside a form", "type", {"ref": "e2", "text": "hi\r\n"}, _gf("textbox", "Message"), _gc(), "risky"),
        ("search with sensitive autocomplete", "type", {"ref": "e2", "text": "4111", "submit": True}, _gf("searchbox", "Search", autocomplete="billing cc-number"), _gc(), "blocked"),
        ("search but sensitive_unknown", "type", {"ref": "e2", "text": "x", "submit": True}, _gf("searchbox", "Search", in_form=True, sensitive_unknown=True), _gc(), "risky"),
        ("sensitive_unknown field", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Code", sensitive_unknown=True), _gc(), "risky"),
        ("type without facts", "type", {"ref": "e9", "text": "x"}, None, _gc(), "risky"),
        ("download", "download", {"name": "report.pdf"}, None, _gc(), "risky"),
        ("dialog", "dialog", {"accept": True}, None, _gc(), "risky"),
        ("open_url task domain", "open_url", {"url": "https://example.com/a"}, None, _gc(url_from_page=True), "safe"),
        ("open_url task subdomain", "open_url", {"url": "https://shop.example.com/a"}, None, _gc(url_from_page=True), "safe"),
        ("open_url www prefix", "open_url", {"url": "https://www.example.com/a"}, None, _gc(url_from_page=True, task_domains={"www.Example.com"}), "safe"),
        ("open_url linked domain", "open_url", {"url": "https://docs.other.org/x"}, None, _gc(url_from_page=True), "safe"),
        ("open_url new domain from page", "open_url", {"url": "https://evil.net/x"}, None, _gc(url_from_page=True), "risky"),
        ("open_url lookalike domain", "open_url", {"url": "https://badexample.com/"}, None, _gc(url_from_page=True), "risky"),
        ("open_url parent of task domain", "open_url", {"url": "https://com/"}, None, _gc(url_from_page=True), "risky"),
        ("open_url new domain, url_from_page False", "open_url", {"url": "https://evil.net/x"}, None, _gc(url_from_page=False), "risky"),
        ("open_url current page domain", "open_url", {"url": "https://foo.org/p"}, None, _gc(domain="foo.org", task_domains=set(), link_domains=set()), "safe"),
        ("open_url search engine", "open_url", {"url": "https://www.google.com/search?q=x"}, None, _gc(task_domains=set(), link_domains=set()), "safe"),
        ("open_url duckduckgo", "open_url", {"url": "https://duckduckgo.com/?q=x"}, None, _gc(url_from_page=True, task_domains=set()), "safe"),
        ("open_url single-label task domain", "open_url", {"url": "http://localhost/x"}, None, _gc(task_domains={"localhost"}, domain=None), "risky"),
        ("open_url bare tld task domain", "open_url", {"url": "https://evil.com/"}, None, _gc(task_domains={"com"}, link_domains={"com"}, domain="com"), "risky"),
        ("open_url tab inside host", "open_url", {"url": "https://exam\tple.com/"}, None, _gc(), "safe"),
        ("open_url control chars around", "open_url", {"url": "\x01 https://evil.net/ \x02"}, None, _gc(), "risky"),
        ("open_url idn lookalike", "open_url", {"url": "https://exаmple.com/"}, None, _gc(), "risky"),
        ("open_url port", "open_url", {"url": "https://example.com:8443/x"}, None, _gc(), "safe"),
        ("open_url host:port no scheme", "open_url", {"url": "evil.net:8080/x"}, None, _gc(), "risky"),
        ("open_url userinfo trick", "open_url", {"url": "https://example.com@evil.net/"}, None, _gc(), "risky"),
        ("open_url control-char javascript", "open_url", {"url": "\x01javascript:alert(1)"}, None, _gc(), "blocked"),
        ("open_url tab-split javascript", "open_url", {"url": "java\tscript:alert(1)"}, None, _gc(), "blocked"),
        ("open_url newline-split javascript", "open_url", {"url": "  java\nscript:alert(1)"}, None, _gc(), "blocked"),
        ("open_url file scheme", "open_url", {"url": "file:///etc/passwd"}, None, _gc(), "blocked"),
        ("open_url data scheme", "open_url", {"url": "DATA:text/html,x"}, None, _gc(), "blocked"),
        ("open_url no scheme", "open_url", {"url": "evil.net/x"}, None, _gc(url_from_page=True), "risky"),
        ("open_url backslash trick", "open_url", {"url": "https://evil.net\\@example.com/"}, None, _gc(url_from_page=True), "risky"),
        ("open_url junk from page", "open_url", {"url": "https://"}, None, _gc(url_from_page=True), "risky"),
        ("read_page", "read_page", {}, None, _gc(), "safe"),
        ("scroll", "scroll", {"direction": "down"}, None, _gc(), "safe"),
        ("back", "back", {}, None, _gc(), "safe"),
        ("note", "note", {"text": "x"}, None, _gc(), "safe"),
        ("ask_user", "ask_user", {"question": "?"}, None, _gc(), "safe"),
        ("done", "done", {"answer": "ok"}, None, _gc(), "safe"),
        ("list_apps", "list_apps", {}, None, _gc(surface="app"), "safe"),
        # blocked
        ("type password", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Password", input_type="password"), _gc(), "blocked"),
        ("type cc-number", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Card", autocomplete="cc-number"), _gc(), "blocked"),
        ("type one-time-code", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Code", autocomplete="one-time-code"), _gc(), "blocked"),
        ("type current-password", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Pw", autocomplete="current-password"), _gc(), "blocked"),
        ("type new-password", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Pw", autocomplete="New-Password"), _gc(), "blocked"),
        ("type_app secure field", "type_app", {"ref": "e2", "text": "x"}, _gf("textfield", "Secret", input_type="password"), _ac(), "blocked"),
        ("type password with submit", "type", {"ref": "e2", "text": "x", "submit": True}, _gf("textbox", "Search", input_type="password", in_form=True), _gc(), "blocked"),
        ("read tier click_app", "click_app", {"ref": "e1"}, _gf(name="OK"), _ac("read", "Terminal"), "blocked"),
        ("read tier type_app", "type_app", {"ref": "e1", "text": "ls"}, _gf("textfield", "cmd"), _ac("read", "Terminal"), "blocked"),
        ("read tier menu", "menu", {"app": "Terminal", "path": ["File", "New"]}, None, _ac("read", "Terminal"), "blocked"),
        ("read tier hotkey", "hotkey", {"keys": ["cmd", "t"]}, None, _ac("read", "Terminal"), "blocked"),
        # apps
        ("read_app in read tier", "read_app", {"name": "Terminal"}, None, _ac("read", "Terminal"), "safe"),
        ("open_app in read tier", "open_app", {"name": "Terminal"}, None, _ac("read", "Terminal"), "safe"),
        ("click_app in ask tier", "click_app", {"ref": "e1"}, _gf(name="Bold"), _ac("ask", "Mail"), "risky"),
        ("type_app in ask tier", "type_app", {"ref": "e1", "text": "x"}, _gf("textfield", "Body"), _ac("ask", "Mail"), "risky"),
        ("menu in ask tier", "menu", {"app": "Mail", "path": ["View", "Zoom"]}, None, _ac("ask", "Mail"), "risky"),
        ("hotkey in ask tier", "hotkey", {"keys": ["cmd", "b"]}, None, _ac("ask", "Mail"), "risky"),
        ("read_app in ask tier", "read_app", {"name": "Mail"}, None, _ac("ask", "Mail"), "safe"),
        ("click Send, allowed app", "click_app", {"ref": "e1"}, _gf(name="Send"), _ac(), "risky"),
        ("click is_submit, allowed app", "click_app", {"ref": "e1"}, _gf(name="Go", is_submit=True), _ac(), "risky"),
        ("click Bold, allowed app", "click_app", {"ref": "e1"}, _gf(name="Bold"), _ac(), "safe"),
        ("type_app plain, allowed app", "type_app", {"ref": "e1", "text": "x"}, _gf("textfield", "Title"), _ac(), "safe"),
        ("type_app risky name, allowed app", "type_app", {"ref": "e1", "text": "x"}, _gf("textfield", "Order notes"), _ac(), "risky"),
        ("menu plain, allowed app", "menu", {"app": "Notes", "path": ["Format", "Bold"]}, None, _ac(), "safe"),
        ("menu risky item, allowed app", "menu", {"app": "Notes", "path": ["File", "Delete Note"]}, None, _ac(), "risky"),
        ("hotkey plain, allowed app", "hotkey", {"keys": ["cmd", "b"]}, None, _ac(), "safe"),
        ("hotkey return, allowed app", "hotkey", {"keys": ["cmd", "Return"]}, None, _ac(), "risky"),
        ("hotkey enter, allowed app", "hotkey", {"keys": ["enter"]}, None, _ac(), "risky"),
        ("hotkey delete, allowed app", "hotkey", {"keys": ["cmd", "delete"]}, None, _ac(), "risky"),
        ("hotkey backspace, allowed app", "hotkey", {"keys": ["Backspace"]}, None, _ac(), "risky"),
        ("hotkey spoofed tier missing", "hotkey", {"keys": ["cmd", "b"]}, None, _gc(surface="app", tier=None), "risky"),
        ("unknown tool", "kill_app", {}, None, _gc(), "blocked"),
        ("javascript url", "open_url", {"url": "javascript:alert(1)"}, None, _gc(), "blocked"),
    ]
    rows += [
        # hotkeys: every spelling of Enter / Delete
        ("hotkey string keys return", "hotkey", {"keys": "return"}, None, _ac(), "risky"),
        ("hotkey combo string", "hotkey", {"keys": ["cmd+return"]}, None, _ac(), "risky"),
        ("hotkey dash combo", "hotkey", {"keys": ["cmd-return"]}, None, _ac(), "risky"),
        ("hotkey kp_enter", "hotkey", {"keys": ["kp_enter"]}, None, _ac(), "risky"),
        ("hotkey keypad_enter", "hotkey", {"keys": ["keypad_enter"]}, None, _ac(), "risky"),
        ("hotkey numpad enter", "hotkey", {"keys": ["numpad enter"]}, None, _ac(), "risky"),
        ("hotkey arrow symbol", "hotkey", {"keys": ["↩"]}, None, _ac(), "risky"),
        ("hotkey return symbol", "hotkey", {"keys": ["⏎"]}, None, _ac(), "risky"),
        ("hotkey space", "hotkey", {"keys": ["space"]}, None, _ac(), "risky"),
        ("hotkey newline", "hotkey", {"keys": ["\n"]}, None, _ac(), "risky"),
        ("hotkey CR", "hotkey", {"keys": ["\r"]}, None, _ac(), "risky"),
        ("hotkey backspace symbol", "hotkey", {"keys": ["⌫"]}, None, _ac(), "risky"),
        ("hotkey fwd delete symbol", "hotkey", {"keys": ["⌦"]}, None, _ac(), "risky"),
        ("hotkey forward_delete", "hotkey", {"keys": ["forward_delete"]}, None, _ac(), "risky"),
        ("hotkey forwarddelete", "hotkey", {"keys": ["cmd", "forwarddelete"]}, None, _ac(), "risky"),
        ("hotkey del", "hotkey", {"keys": ["del"]}, None, _ac(), "risky"),
        ("hotkey Return case", "hotkey", {"keys": ["CMD", "RETURN"]}, None, _ac(), "risky"),
        ("hotkey plain string combo", "hotkey", {"keys": "cmd+b"}, None, _ac(), "safe"),
        ("hotkey cmd minus", "hotkey", {"keys": ["cmd+-"]}, None, _ac(), "safe"),
        ("hotkey not a list", "hotkey", {"keys": 5}, None, _ac(), "risky"),
        ("hotkey non-string item", "hotkey", {"keys": ["cmd", 5]}, None, _ac(), "risky"),
        ("hotkey empty", "hotkey", {"keys": []}, None, _ac(), "risky"),
        ("hotkey missing", "hotkey", {}, None, _ac(), "risky"),
        ("hotkey dict", "hotkey", {"keys": {"a": 1}}, None, _ac(), "risky"),
        ("hotkey in a browser ctx", "hotkey", {"keys": ["cmd", "b"]}, None, _gc(), "risky"),
        # typing text in apps
        ("type_app newline", "type_app", {"ref": "e1", "text": "hi\n"}, _gf("textfield", "Title"), _ac(), "risky"),
        ("type_app crlf", "type_app", {"ref": "e1", "text": "hi\r\nthere"}, _gf("textfield", "Title"), _ac(), "risky"),
        ("type_app line separator", "type_app", {"ref": "e1", "text": "hi "}, _gf("textfield", "Title"), _ac(), "risky"),
        ("type_app search with newline", "type_app", {"ref": "e1", "text": "cats\n"}, _gf("searchfield", "Search"), _ac(), "safe"),
        ("type_app in read tier with newline", "type_app", {"ref": "e1", "text": "ls\n"}, _gf("textfield", "cmd"), _ac("read", "Terminal"), "blocked"),
        # sensitive autocomplete is a token list
        ("autocomplete billing cc-number", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Card", autocomplete="billing cc-number"), _gc(), "blocked"),
        ("autocomplete section cc-number", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Card", autocomplete="section-x cc-number"), _gc(), "blocked"),
        ("autocomplete webauthn", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Pw", autocomplete="current-password webauthn"), _gc(), "blocked"),
        ("autocomplete spaced upper", "type_app", {"ref": "e2", "text": "x"}, _gf("textfield", "Pw", autocomplete="  ON   Cc-Number "), _ac(), "blocked"),
        ("autocomplete harmless", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Email", autocomplete="section-a email"), _gc(), "safe"),
        ("autocomplete off", "type", {"ref": "e2", "text": "x"}, _gf("textbox", "Note", autocomplete="off"), _gc(), "safe"),
        # menu: target app and path shape
        ("menu for another app", "menu", {"app": "Terminal", "path": ["File", "New"]}, None, _ac(), "risky"),
        ("menu app same, other case", "menu", {"app": "notes", "path": ["Format", "Bold"]}, None, _ac(), "safe"),
        ("menu app with .app", "menu", {"app": "Notes.app", "path": ["Format", "Bold"]}, None, _ac(), "safe"),
        ("menu string path risky", "menu", {"app": "Notes", "path": "File > Delete Note"}, None, _ac(), "risky"),
        ("menu string path arrow risky", "menu", {"app": "Notes", "path": "File › Delete Note"}, None, _ac(), "risky"),
        ("menu string path plain", "menu", {"app": "Notes", "path": "Format > Bold"}, None, _ac(), "safe"),
        ("menu joined item risky", "menu", {"app": "Notes", "path": ["File > Delete Note"]}, None, _ac(), "risky"),
        ("menu path of wrong type", "menu", {"app": "Notes", "path": 7}, None, _ac(), "risky"),
        ("menu empty path", "menu", {"app": "Notes", "path": []}, None, _ac(), "risky"),
        ("menu no app arg", "menu", {"path": ["Format", "Bold"]}, None, _ac(), "safe"),
        ("menu other app, ctx has none", "menu", {"app": "Notes", "path": ["Format", "Bold"]}, None, _gc(surface="app", tier="allowed", app=None), "risky"),
        # the tier follows the surface, not the tool name
        ("click on app surface, read tier", "click", {"ref": "e1"}, _gf(name="OK"), _ac("read", "Terminal"), "blocked"),
        ("type on app surface, read tier", "type", {"ref": "e1", "text": "x"}, _gf("textfield", "cmd"), _ac("read", "Terminal"), "blocked"),
        ("click on app surface, ask tier", "click", {"ref": "e1"}, _gf(name="Bold"), _ac("ask", "Mail"), "risky"),
        ("type on app surface, ask tier", "type", {"ref": "e1", "text": "x"}, _gf("textfield", "Body"), _ac("ask", "Mail"), "risky"),
        ("click on app surface, no tier", "click", {"ref": "e1"}, _gf(name="Bold"), _gc(surface="app", tier=None, app="Notes"), "risky"),
        ("click on app surface, allowed", "click", {"ref": "e1"}, _gf(name="Bold"), _ac(), "safe"),
        ("click Send on app surface, allowed", "click", {"ref": "e1"}, _gf(name="Send"), _ac(), "risky"),
        ("type risky field on app surface", "type", {"ref": "e1", "text": "x"}, _gf("textfield", "Order notes"), _ac(), "risky"),
        ("risky word 'Make payment'", "click", {"ref": "e1"}, _gf(name="Make payment"), _gc(), "risky"),
        ("risky word 'Payment'", "click", {"ref": "e1"}, _gf(name="Payment"), _gc(), "risky"),
        ("risky word 'Complete purchase'", "click", {"ref": "e1"}, _gf(name="Complete purchase"), _gc(), "risky"),
        ("risky word 'Purchase'", "click", {"ref": "e1"}, _gf(name="Purchase"), _gc(), "risky"),
        ("risky word 'Donate $50'", "click", {"ref": "e1"}, _gf(name="Donate $50"), _gc(), "risky"),
        ("risky word 'Move to Trash'", "click", {"ref": "e1"}, _gf(name="Move to Trash"), _gc(), "risky"),
        ("risky word 'Reply'", "click", {"ref": "e1"}, _gf(name="Reply"), _gc(), "risky"),
        ("risky word 'Share'", "click", {"ref": "e1"}, _gf(name="Share"), _gc(), "risky"),
        ("risky word 'Authorize'", "click", {"ref": "e1"}, _gf(name="Authorize"), _gc(), "risky"),
        ("risky word 'Authorise'", "click", {"ref": "e1"}, _gf(name="Authorise"), _gc(), "risky"),
        ("risky word 'Sends'", "click", {"ref": "e1"}, _gf(name="Sends"), _gc(), "risky"),
        ("risky word 'Deleting...'", "click", {"ref": "e1"}, _gf(name="Deleting..."), _gc(), "risky"),
        ("risky word 'Deleted'", "click", {"ref": "e1"}, _gf(name="Deleted"), _gc(), "risky"),
        ("risky word 'Unsubscribe'", "click", {"ref": "e1"}, _gf(name="Unsubscribe"), _gc(), "risky"),
        ("risky word 'Sign  up'", "click", {"ref": "e1"}, _gf(name="Sign  up"), _gc(), "risky"),
        ("risky word 'Signup'", "click", {"ref": "e1"}, _gf(name="Signup"), _gc(), "risky"),
        ("risky word 'Check\\nout'", "click", {"ref": "e1"}, _gf(name="Check\nout"), _gc(), "risky"),
        ("risky word 'Checkout'", "click", {"ref": "e1"}, _gf(name="Checkout"), _gc(), "risky"),
        ("risky word 'Approve'", "click", {"ref": "e1"}, _gf(name="Approve"), _gc(), "risky"),
        ("risky word 'Accept all'", "click", {"ref": "e1"}, _gf(name="Accept all"), _gc(), "risky"),
        ("risky word 'Grant access'", "click", {"ref": "e1"}, _gf(name="Grant access"), _gc(), "risky"),
        ("risky word 'S\\u200bend'", "click", {"ref": "e1"}, _gf(name="S\u200bend"), _gc(), "risky"),
        ("risky word '\\uff33\\uff45\\uff4e\\uff44'", "click", {"ref": "e1"}, _gf(name="\uff33\uff45\uff4e\uff44"), _gc(), "risky"),
        ("risky word 'Place   order'", "click", {"ref": "e1"}, _gf(name="Place   order"), _gc(), "risky"),
        ("risky word 'Subscription'", "click", {"ref": "e1"}, _gf(name="Subscription"), _gc(), "risky"),
        ("risky word 'Posting'", "click", {"ref": "e1"}, _gf(name="Posting"), _gc(), "risky"),
        ("risky word 'Removed'", "click", {"ref": "e1"}, _gf(name="Removed"), _gc(), "risky"),
        ("risky word 'Replies'", "click", {"ref": "e1"}, _gf(name="Replies"), _gc(), "risky"),
        ("risky word 'Transferred'", "click", {"ref": "e1"}, _gf(name="Transferred"), _gc(), "risky"),
        ("risky word 'Publishing'", "click", {"ref": "e1"}, _gf(name="Publishing"), _gc(), "risky"),
        ("safe word 'Bold'", "click", {"ref": "e1"}, _gf(name="Bold"), _gc(), "safe"),
        ("safe word 'Italic'", "click", {"ref": "e1"}, _gf(name="Italic"), _gc(), "safe"),
        ("safe word 'Sharp'", "click", {"ref": "e1"}, _gf(name="Sharp"), _gc(), "safe"),
        ("safe word 'Border'", "click", {"ref": "e1"}, _gf(name="Border"), _gc(), "safe"),
        ("safe word 'Postal map'", "click", {"ref": "e1"}, _gf(name="Postal map"), _gc(), "safe"),
        ("safe word 'Bookmarks'", "click", {"ref": "e1"}, _gf(name="Bookmarks"), _gc(), "safe"),
        ("safe word 'Pricing'", "click", {"ref": "e1"}, _gf(name="Pricing"), _gc(), "safe"),
        ("safe word 'Next page'", "click", {"ref": "e1"}, _gf(name="Next page"), _gc(), "safe"),
        ("safe word 'Reading list'", "click", {"ref": "e1"}, _gf(name="Reading list"), _gc(), "safe"),
        ("safe word 'Ordinary'", "click", {"ref": "e1"}, _gf(name="Ordinary"), _gc(), "safe"),
        ("safe word 'Replace font'", "click", {"ref": "e1"}, _gf(name="Replace font"), _gc(), "safe"),
        ("safe word 'Sentence'", "click", {"ref": "e1"}, _gf(name="Sentence"), _gc(), "safe"),
        ("safe word 'Payroll tab'", "click", {"ref": "e1"}, _gf(name="Payroll tab"), _gc(), "safe"),
    ]
    bad = []
    for label, tool, args, facts, ctx, want in rows:
        got, reason = G(tool, args, facts, ctx)
        if got != want or not reason:
            bad.append(f"{label}: wanted {want}, got {got} ({reason!r})")
    assert not bad, "\n".join(bad)
    # the gate never mutates what it is given
    c = _gc(url_from_page=True)
    before = {k: set(v) if isinstance(v, set) else v for k, v in c.items()}
    G("open_url", {"url": "https://shop.example.com"}, None, c)
    assert c == before


@check
def approval_titles():
    T = app.approval_title
    assert T("click", _gf("link", "Place order"), _gc(domain="shop.example.com")) == "Click **Place order** on shop.example.com"
    assert T("click", _gf("button", ""), _gc()) == "Click **button** on example.com"
    assert T("type", _gf("textbox", "Message"), _gc()) == "Type into **Message** on example.com"
    assert T("type_app", _gf("textfield", "Body"), _ac("ask", "Mail")) == "Type into **Body** in Mail"
    assert T("click_app", _gf("button", "Send"), _ac("ask", "Mail")) == "Click **Send** in Mail"
    assert T("download", _gf("link", "Report.pdf"), _gc()) == "Download **Report.pdf** on example.com"
    assert T("open_url", None, _gc(), {"url": "https://evil.net/x"}) == "Open **evil.net** (https\\://evil.net/x)"
    assert T("dialog", None, _gc()) == "Answer a page dialog on example.com"
    assert T("menu", None, _ac("ask", "Mail"), {"path": ["File", "Delete Note"]}) == "Use menu **File \u203a Delete Note** in Mail"
    assert T("hotkey", None, _ac("ask", "Mail"), {"keys": ["cmd", "return"]}) == "Press **\u2318+Return** in Mail"
    long = T("click", _gf("button", "x" * 300), _gc())
    assert long.startswith("Click **xxx") and "\u2026** on example.com" in long and len(long) < 120
    assert "\n" not in T("click", _gf("button", "a\nb"), _gc())
    # a page-controlled label cannot rewrite the card
    t = T("click", _gf("button", "OK** on example.com ![x](https://evil.net/t.png) **"), _gc())
    assert t.startswith("Click **OK\\*\\* on example.com \\!\\[x\\]\\(https\\://evil.net/t.png\\) \\*\\***") and t.endswith("** on example.com"), t
    t = T("type_app", _gf("textfield", "`a` <b>x</b> # | _y_ [l](u)"), _ac("ask", "Mail"))
    assert not any(c in t[len("Type into **"):-len("** in Mail")].replace("\\" + c, "") for c in "`<>#|_[]()"), t
    assert T("click", _gf("button", "S\u200be\u202ent"), _gc()) == "Click **Sent** on example.com"
    assert T("click", _gf("button", "x"), _gc(domain="a_b*c.com")) == "Click **x** on a\\_b\\*c.com"
    # a URL's real host is shown, unclipped and in ASCII
    t = T("open_url", None, _gc(), {"url": "https://" + "a" * 60 + ".evil.net/" + "p" * 200})
    assert t.startswith("Open **" + "a" * 60 + ".evil.net** (https\\://") and t.endswith("\u2026)"), t
    assert T("open_url", None, _gc(), {"url": "https://\u0435xample.com/"}).startswith("Open **xn--xample-2of.com** ("), T("open_url", None, _gc(), {"url": "https://\u0435xample.com/"})
    assert T("open_url", None, _gc(), {"url": "https://www.example.com\t/x"}).startswith("Open **example.com** (")
    assert T("open_url", None, _gc(), {"url": "https://"}).startswith("Open **an address with no host**")
    assert T("hotkey", None, _ac(), {"keys": "cmd+return"}) == "Press **\u2318+Return** in Notes"
    assert T("hotkey", None, _ac(), {"keys": 5}) == "Press **?** in Notes"
    assert T("menu", None, _ac(), {"path": "File > Delete Note"}) == "Use menu **File \u203a Delete Note** in Notes"
    assert T("menu", None, _ac(), {"path": 3}) == "Use menu **?** in Notes"


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   agent checks OK")
