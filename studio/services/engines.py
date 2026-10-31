"""The chat engines: one at a time.

Moved from app.py in the refactor's P1 split.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time

import httpx

from studio import config as studio_config
from studio.infra.logs import open_log
from studio.config import BIN, DSPARK_PY, ENGINES, LLAMA_SERVER, LOGS
from studio.storage.atomic import write_text_atomic
from studio.infra.system import _pid_alive, _read_pid
from studio.services.models import chat_models, effective_context, snapshot_file


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
            write_text_atomic(ENGINE_STATE, json.dumps(st))
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


_engine_versions: dict = {}        # engine -> installed version, read once per process (an engine update clears it)


def _installed_version(engine):
    """mlx-vlm's version in the mlx-dspark env, or llama.cpp's build ("b11960"); None when it can't be read."""
    if engine not in _engine_versions:
        version = None
        try:
            if engine == "mlx-vlm":
                out = subprocess.run([str(DSPARK_PY), "-c", "import importlib.metadata as m; print(m.version('mlx-vlm'))"],
                                     capture_output=True, text=True, timeout=20)
                version = out.stdout.strip() or None
            elif engine == "llama.cpp":
                out = subprocess.run([LLAMA_SERVER, "--version"], capture_output=True, text=True, timeout=10)
                version = _llama_build(out.stdout + out.stderr)
        except (OSError, subprocess.SubprocessError):
            pass
        _engine_versions[engine] = version
    return _engine_versions[engine]


def _llama_build(text):
    """The build in `llama-server --version`: "version: 0.7.0 (build 11669, …)", or "version: 6123 (…)" before 0.x."""
    found = re.search(r"build (\d+)", text) or re.search(r"version: (\d+) ", text)
    return f"b{found.group(1)}" if found else None


def engine_too_old(m):
    """Why this model can't run on the installed engines (its catalog `min_engine`), or None."""
    def key(v):
        return tuple(int(x) for x in re.findall(r"\d+", v))
    for engine, want in (m.get("min_engine") or {}).items():
        have = _installed_version(engine)
        if have and key(have) < key(want):
            return f"{m['name']} needs {engine} {want} or newer, and this Mac has {have}. Use Settings → Update engines, then try again."
    return None


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
    log = open_log(LOGS / f"{ENGINES[mode]['pid']}.log", "w")
    if mode == "text":
        cmd = [str(BIN / "mlx-dspark"), "serve", "--model", m["repo"], "--context-window", str(effective_context(m)),
               "--host", "127.0.0.1", "--port", "8080"]
        if m.get("drafter"):
            cmd += ["--drafter", m["drafter"], "--mode", m.get("mode", "auto")]
        if _uses_mlx_lm(m["repo"]):
            cmd += ["--kv-bits", "8"]
    elif mode == "vision":
        # No window argument on purpose: mlx-vlm's --max-kv-size is a rotating cache that silently drops the oldest tokens (the system
        # prompt first). The window still holds here: prompts and replies are budgeted against it by the chat route (_chat_fit_context).
        cmd = [str(DSPARK_PY), "-m", "mlx_vlm", "server", "--model", m["repo"], "--host", "127.0.0.1", "--port", "8081"]
        if m["id"] == "qwen3.8-27b":               # the only pairing verified with mlx-vlm's DFlash path
            cmd += ["--draft-model", m["drafter"], "--draft-kind", "dflash"]
    else:
        model = snapshot_file(m["repo"], m["file"])
        cmd = [LLAMA_SERVER, "-m", str(model), "-c", str(effective_context(m)), "-ngl", "999", "--jinja",
               "--host", "127.0.0.1", "--port", "8082", "-a", m["id"]]
        if m.get("mmproj") and snapshot_file(m["repo"], m["mmproj"]):
            cmd += ["--mmproj", str(snapshot_file(m["repo"], m["mmproj"]))]
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (LOGS / f"{ENGINES[mode]['pid']}.pid").write_text(str(proc.pid))
    write_text_atomic(ENGINE_STATE, json.dumps({"mode": mode, "model_id": m["id"], "context_window": effective_context(m)}))
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
        if os.environ.get("LOCAL_STUDIO_EVAL_ATTACH") == "1":
            # macos/evals borrows the engine the user already loaded. It must never stop or start one: engine
            # processes are matched machine-wide, so doing either would take the running app's model away.
            if _engine_ready(mode):
                return False
            raise RuntimeError(f"Eval attach mode: no {mode} engine is ready. Load {m['name']} in Local Studio first.")
        st = engine_state()
        # An engine keeps the window it was started with, so a changed setting needs a restart. Engines started before the window was
        # recorded all used the old fixed 32k.
        window_changed = st.get("context_window", studio_config.LEGACY_CONTEXT) != effective_context(m)
        if st.get("mode") == mode and st.get("model_id") == m["id"] and _engine_ready(mode) and not window_changed:
            return False
        too_old = engine_too_old(m)                   # before stopping anything: the running model keeps working
        if too_old:
            raise RuntimeError(too_old)
        for other in ENGINES:
            if _proc_running(ENGINES[other]["pattern"]) and (other != mode or st.get("model_id") != m["id"] or window_changed):
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
