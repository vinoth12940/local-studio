"""Build guard for the Local Agent logic in app.py. Run by macos/build.sh, or by hand:

    uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_agent.py [check_name ...]
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")      # never touch real user data
os.environ["HF_HUB_CACHE"] = str(TMP / "hub")             # a Mac with no models downloaded
(TMP / "state").mkdir()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
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


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   agent checks OK")
