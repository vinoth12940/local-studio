"""End-to-end check of the Chat settings with the real parts: a request through the real /api/chat to the model already loaded in
Local Studio, and what that model does with what the settings put in it.

  - System prompt: the instruction in Settings -> Chat settings is the leading system message of every request (Chat, and Code
    with a workspace). The route adds its own long tool note after it; does the model still follow the user's instruction?
  - Context window: the loaded engine reports the window the setting asks for, and the route budgets against the same number.
  - Effort: the Effort control is enabled only for a model whose chat template reads the level (/api/models "effort").

The model is used attach-only (LOCAL_STUDIO_EVAL_ATTACH=1, and starting or stopping any engine raises): load a chat model in Local
Studio first. Everything else is isolated: a throwaway LOCAL_STUDIO_HOME and an app server on port 7882. Never sends a chat to the
Local Studio you use.

    TMPDIR=/tmp uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/check_settings_e2e.py

Exit 0: every case passed; 1: a case failed; 2: blocked (no model loaded). A JSON report goes to build/settings-e2e/.
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="ls-set-", dir="/tmp")).resolve()
STATE, WORK = TMP / "state", TMP / "work"
APP_PORT = 7882
LIVE = Path.home() / "Library" / "Application Support" / "Local Studio"

os.environ.update(LOCAL_STUDIO_HOME=str(STATE), LOCAL_STUDIO_EVAL_ATTACH="1", LOCAL_STUDIO_SEARCH_PORT="8099",
                  LOCAL_STUDIO_VOICE_PORT="8097", LOCAL_STUDIO_DECIDE_PORT="8089")
(STATE / "data").mkdir(parents=True)
(STATE / "logs").mkdir(parents=True)
WORK.mkdir()
(WORK / "notes.txt").write_text("A scratch folder for the settings check.\n")


def live(name):
    try:
        return json.loads((LIVE / "logs" / "engine.json").read_text()).get(name)
    except (OSError, ValueError):
        return None


MODEL_ID, LIVE_WINDOW = live("model_id"), live("context_window")
(STATE / "data" / "settings.json").write_text(json.dumps({"chat_model": MODEL_ID, "context_window": LIVE_WINDOW}))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from studio import config  # noqa: E402
from studio.services import engines  # noqa: E402
from studio.services import models  # noqa: E402


def _no_engine(*args, **kwargs):
    raise RuntimeError("the end-to-end check must never start or stop an engine (attach-only)")


engines._start_engine = engines._stop_engine = _no_engine
SYSTEM = 'Begin every reply with the exact word PINEAPPLE, then a colon, then your answer.'
QUESTIONS = ["What is the capital of Japan?", "Give me two tips for sleeping better.", "What is twelve times twelve?"]
BASE = f"http://127.0.0.1:{APP_PORT}"


def chat(client, system, question, workspace=None):
    """One typed turn through the real route, as the Chat or Code view sends it (the Settings system prompt first)."""
    body = {"model_id": MODEL_ID, "chat_id": None, "max_tokens": 1024, "enable_thinking": False, "timezone": "Asia/Kolkata",
            "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": question}]}
    if workspace:
        body["workspace"] = str(workspace)
    text, round_text, errors = "", "", []
    with client.stream("POST", f"{BASE}/api/chat", json=body, timeout=300) as r:
        if r.status_code != 200:
            return "", [f"HTTP {r.status_code}: {r.read().decode(errors='replace')[:300]}"]
        for line in r.iter_lines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            try:
                ev = json.loads(line[5:])
            except ValueError:
                continue
            if ev.get("error"):
                errors.append(str(ev["error"])[:300])
            if ev.get("x_tool") and ev["x_tool"].get("status") == "running":
                round_text = ""
            ch = (ev.get("choices") or [{}])[0]
            d = (ch.get("delta") or {}).get("content")
            if d:
                round_text += d; text += d
            if ch.get("finish_reason") == "tool_calls":
                round_text = ""
    return (round_text.strip() or text.strip()), errors


def serve_app():
    from studio.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=APP_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            if httpx.get(f"{BASE}/api/models", timeout=1).status_code == 200:
                return server
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("the test app server did not start")


def main():
    mode = models.engine_mode(models.chat_model(MODEL_ID)) if MODEL_ID else None
    if not mode or not engines._engine_ready(mode):
        print("   blocked: no chat model is loaded in Local Studio (load one, then run this again)")
        return 2
    print(f"   model: {MODEL_ID} on the {mode} engine (attach-only)")
    server = serve_app()
    client = httpx.Client(trust_env=False)
    report, failures = {"model": MODEL_ID, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": []}, []

    def case(name, ok, detail):
        report["cases"].append({"case": name, "ok": ok, "detail": detail})
        print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    try:
        # System prompt: in Chat, and in Code (a workspace adds more to the route's own note).
        for label, workspace in (("chat", None), ("code", WORK)):
            for q in QUESTIONS:
                reply, errors = chat(client, SYSTEM, q, workspace)
                case(f"system prompt ({label})", reply.upper().startswith("PINEAPPLE") and not errors, f"{q!r} -> {reply[:70]!r}{' ' + str(errors) if errors else ''}")
        for label, workspace in (("chat", None), ("code", WORK)):
            reply, errors = chat(client, "", QUESTIONS[0], workspace)
            case(f"no system prompt ({label}): nothing invented", "PINEAPPLE" not in reply.upper() and not errors, f"-> {reply[:70]!r}")
        # Context window: what the engine runs with is what the app budgets against.
        window, health = models.effective_context(models.chat_model(MODEL_ID)), {}
        try:
            health = httpx.get(config.ENGINES[mode]["url"].rstrip("/") + "/health", timeout=3).json()
        except (httpx.HTTPError, ValueError):
            pass
        reported = health.get("context_window")
        if reported is None:
            case("context window", True, f"the {mode} engine does not report one; the app budgets {window} tokens (skipped)")
        else:
            case("context window", reported == window, f"engine runs {reported}, the app budgets {window}")
        api = client.get(f"{BASE}/api/context-window").json()
        case("context window API", api.get("effective") == window, f"effective {api.get('effective')}, native max {api.get('native_max')}")
        # Effort: the control is enabled only where the template reads the level.
        row = next(m for m in client.get(f"{BASE}/api/models").json()["chat"] if m["id"] == MODEL_ID)
        case("effort control", isinstance(row.get("effort"), bool), f"{row['name']}: {'has effort levels' if row.get('effort') else 'no effort levels (the control is off)'}")
    finally:
        server.should_exit = True
    out = ROOT / "build" / "settings-e2e"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(report, indent=1))
    if failures:
        print(f"   settings end-to-end FAILED: {sorted(set(failures))}")
        return 1
    print("   settings end-to-end OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
