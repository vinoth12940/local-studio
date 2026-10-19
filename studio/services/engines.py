"""The chat engines: one at a time.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import json
import os
import signal
import subprocess
import threading
import time

import httpx

from studio import config as studio_config
from studio.config import BIN, DSPARK_PY, ENGINES, LLAMA_SERVER, LOGS
from studio.infra.system import _pid_alive, _read_pid
from studio.services.models import chat_models, snapshot_file


# ---------------------------------------------------------------- engines

ENGINE_STATE = LOGS / "engine.json"
engine_lock = threading.Lock()


def _proc_running(pattern):
    return bool(subprocess.run(["pgrep", "-f", pattern], capture_output=True).stdout.strip())


def running_mode():
    for mode, e in ENGINES.items():
        if _proc_running(e["pattern"]):
            return mode
    return None


def engine_running():
    return running_mode() is not None


def engine_state():
    """{mode, model_id} of the running engine (mode None when nothing runs)."""
    mode = running_mode()
    try:
        st = json.loads(ENGINE_STATE.read_text())
    except Exception:
        st = {}
    if not mode:
        return {"mode": None, "model_id": st.get("model_id")}
    if st.get("mode") != mode or not st.get("model_id"):
        # started outside this app version: ask the engine what it loaded, never guess from
        # settings (they may already name the model we're about to switch to)
        st = {"mode": mode, "model_id": _infer_running_model(mode)}
        if st["model_id"]:
            ENGINE_STATE.write_text(json.dumps(st))
    return st


def _infer_running_model(mode):
    try:
        if mode == "text":
            repo = httpx.get(ENGINES["text"]["url"] + "/health", timeout=3).json().get("target")
            return next((m["id"] for m in chat_models() if m["repo"] == repo), None)
        ids = [d.get("id") for d in httpx.get(ENGINES[mode]["url"] + "/v1/models", timeout=3).json().get("data", [])]
        return next((m["id"] for m in chat_models() if m["id"] in ids or m["repo"] in ids), None)
    except Exception:
        return None


def _engine_ready(mode):
    e = ENGINES[mode]
    try:
        r = httpx.get(e["url"] + e["ready"], timeout=3)
        return r.status_code == 200 and (mode != "text" or r.json().get("status") == "ok")
    except Exception:
        return False


def _uses_mlx_lm(repo):
    """mlx-dspark routes multimodal configs to mlx-vlm except qwen3_5-family; --kv-bits only
    applies on the mlx-lm route."""
    cfg = snapshot_file(repo, "config.json")
    try:
        c = json.loads(cfg.read_text()) if cfg else {}
    except Exception:
        c = {}
    return c.get("model_type") in ("qwen3_5", "qwen3_5_moe") or "vision_config" not in c


def _start_engine(mode, m):
    log = open(LOGS / f"{ENGINES[mode]['pid']}.log", "w")
    if mode == "text":
        cmd = [str(BIN / "mlx-dspark"), "serve", "--model", m["repo"], "--context-window", str(studio_config.CONTEXT),
               "--host", "127.0.0.1", "--port", "8080"]
        if m.get("drafter"):
            cmd += ["--drafter", m["drafter"], "--mode", m.get("mode", "auto")]
        if _uses_mlx_lm(m["repo"]):
            cmd += ["--kv-bits", "8"]
    elif mode == "vision":
        cmd = [str(DSPARK_PY), "-m", "mlx_vlm", "server", "--model", m["repo"], "--host", "127.0.0.1", "--port", "8081"]
        if m["id"] == "qwen3.8-27b":               # the only pairing verified with mlx-vlm's DFlash path
            cmd += ["--draft-model", m["drafter"], "--draft-kind", "dflash"]
    else:
        model = snapshot_file(m["repo"], m["file"])
        cmd = [LLAMA_SERVER, "-m", str(model), "-c", str(studio_config.CONTEXT), "-ngl", "999", "--jinja",
               "--host", "127.0.0.1", "--port", "8082", "-a", m["id"]]
        if m.get("mmproj") and snapshot_file(m["repo"], m["mmproj"]):
            cmd += ["--mmproj", str(snapshot_file(m["repo"], m["mmproj"]))]
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (LOGS / f"{ENGINES[mode]['pid']}.pid").write_text(str(proc.pid))
    ENGINE_STATE.write_text(json.dumps({"mode": mode, "model_id": m["id"]}))
    return proc.pid


def _stop_engine(mode):
    e = ENGINES[mode]
    pid = _read_pid(e["pid"])
    if pid and _pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
    subprocess.run(["pkill", "-f", e["pattern"]])
    (LOGS / f"{e['pid']}.pid").unlink(missing_ok=True)
    for _ in range(40):                       # wait for the weights to leave memory
        if not _proc_running(e["pattern"]):
            break
        time.sleep(0.25)


def ensure_engine(mode, m, timeout=300):
    """Make (mode, model) the running engine, stopping anything else; block until ready."""
    with engine_lock:
        st = engine_state()
        if st.get("mode") == mode and st.get("model_id") == m["id"] and _engine_ready(mode):
            return False
        for other in ENGINES:
            if _proc_running(ENGINES[other]["pattern"]) and (other != mode or st.get("model_id") != m["id"]):
                _stop_engine(other)
        if not _proc_running(ENGINES[mode]["pattern"]):
            _start_engine(mode, m)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _engine_ready(mode):
                return True
            if not _proc_running(ENGINES[mode]["pattern"]):
                tail = (LOGS / f"{ENGINES[mode]['pid']}.log").read_text()[-600:]
                raise RuntimeError(f"{m['name']} failed to start: {tail.strip().splitlines()[-1] if tail.strip() else 'see logs'}")
            time.sleep(1)
        raise RuntimeError(f"{m['name']} did not become ready in {timeout}s")


def _ensure_quiet(mode, m):
    try:
        ensure_engine(mode, m)
    except Exception:
        pass
