"""Local Studio: a local web app for on-device chat and image generation on Apple Silicon.

Chat runs on one of three engines (only one at a time, to keep memory free):
  text   — mlx-dspark: MLX models with DFlash/DSpark speculative decoding (text only)
  vision — mlx-vlm server: the same MLX weights when a chat contains images
  gguf   — llama.cpp's llama-server: GGUF models (vision when an mmproj file is present)
Image jobs run the mflux CLI one at a time. Also manages models, storage, updates and uninstall.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

APP_VERSION = "1.2.2"

ROOT = Path(__file__).resolve().parent                       # code (inside the .app when bundled)
STATE = Path(os.environ.get("LOCAL_STUDIO_HOME", ROOT))      # user data: images, chats, logs
GALLERY = STATE / "gallery"
UPLOADS = STATE / "uploads"
LOGS = STATE / "logs"
DATA = STATE / "data"
CHATS = DATA / "chats"
SETTINGS = DATA / "settings.json"
for d in (GALLERY, UPLOADS, LOGS, CHATS):
    d.mkdir(parents=True, exist_ok=True)

HOME = Path.home()
BIN = HOME / ".local/bin"
UV = shutil.which("uv") or str(BIN / "uv")
HF_HUB = Path(os.environ.get("HF_HUB_CACHE", HOME / ".cache/huggingface/hub"))
HF_CLI = HOME / ".local/share/uv/tools/mlx-dspark/bin/hf"
DSPARK_PY = HOME / ".local/share/uv/tools/mlx-dspark/bin/python"   # its env also ships mlx-vlm
APP_SUPPORT = HOME / "Library/Application Support/Local Studio"
LLAMA_SERVER = next((p for p in (shutil.which("llama-server"), "/opt/homebrew/bin/llama-server",
                                 str(APP_SUPPORT / "llama.cpp/llama-server")) if p and Path(p).exists()),
                    "/opt/homebrew/bin/llama-server")
BREW = shutil.which("brew") or "/opt/homebrew/bin/brew"
CONTEXT = 32768
# mflux editing needs a complete checkpoint; the q4 export used here is text-to-image only.
EDIT_MODEL = os.environ.get("EDIT_MODEL")
SEARCH_URL = "http://127.0.0.1:8083"                         # search.py, see the search section
SEARCH_DIR = DATA / "search"                                 # its index
SEARCH_REQS = ROOT / "search-requirements.txt"
SEARCH_DEPS_OK = DATA / "search-deps.ok"                     # written by setup.sh --search
SEARCH_DEFAULTS = {"enabled": False, "folders": [], "removed": [], "paused": False, "on_battery": False}

CATALOG = json.loads((ROOT / "catalog.json").read_text())

app = FastAPI()
app.mount("/gallery", StaticFiles(directory=GALLERY), name="gallery")

# ---------------------------------------------------------------- helpers


def _sysctl(name):
    try:
        return int(subprocess.check_output(["sysctl", "-n", name], text=True).strip())
    except Exception:
        return None


def system_memory():
    """Used = app (anonymous - purgeable) + wired + compressed, as Activity Monitor counts it."""
    total = _sysctl("hw.memsize") or 0
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", out).group(1))
    pages = {k.strip().strip('"'): int(v) for k, v in re.findall(r"^(.+?):\s+(\d+)\.?$", out, re.M)}
    used = (pages.get("Anonymous pages", 0) - pages.get("Pages purgeable", 0)
            + pages.get("Pages wired down", 0) + pages.get("Pages occupied by compressor", 0)) * page
    return {"total_bytes": total, "used_bytes": used, "free_percent": _sysctl("kern.memorystatus_level")}


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _read_pid(name):
    try:
        return int((LOGS / f"{name}.pid").read_text().strip())
    except Exception:
        return None


def load_settings():
    try:
        s = json.loads(SETTINGS.read_text())
    except Exception:
        s = {}
    s.setdefault("chat_model", next(m["id"] for m in CATALOG["chat"] if m.get("default")))
    s.setdefault("image_model", next(m["id"] for m in CATALOG["image"] if m.get("default")))
    s.setdefault("custom", [])
    s["search"] = {**SEARCH_DEFAULTS, **(s.get("search") or {})}
    return s


def save_settings(s):
    SETTINGS.write_text(json.dumps(s, indent=2))


def chat_models():
    return CATALOG["chat"] + [m for m in load_settings()["custom"]]


def chat_model(mid=None):
    mid = mid or load_settings()["chat_model"]
    m = next((m for m in chat_models() if m["id"] == mid), None)
    return m or next(m for m in CATALOG["chat"] if m.get("default"))


def image_model(mid=None):
    mid = mid or load_settings()["image_model"]
    return next((m for m in CATALOG["image"] if m["id"] == mid), CATALOG["image"][0])


def search_model():
    return CATALOG["search"][0]


def all_models():
    """Everything the app can download: chat models (catalog and custom), image models, the search embedder."""
    return chat_models() + CATALOG["image"] + CATALOG["search"]


@app.get("/api/system")
def api_system():
    return {**system_memory(), "edit_enabled": bool(EDIT_MODEL), "version": APP_VERSION}

# ---------------------------------------------------------------- models on disk


def repo_dir(repo: str) -> Path:
    return HF_HUB / ("models--" + repo.replace("/", "--"))


def _repo_files(d: Path) -> set[Path]:
    """Every real file a cached repo uses. Snapshot symlinks resolve into the repo's own
    blobs/ or the hub-wide shared blobs/ store, so resolve them all."""
    files = set()
    for p in d.rglob("*"):
        try:
            r = p.resolve()
            if r.is_file():
                files.add(r)
        except OSError:
            pass
    return files


def _repo_for_dir(name: str) -> str:
    return name[len("models--"):].replace("--", "/", 1)


def snapshot_file(repo, name):
    """Path of `name` inside the newest snapshot of a cached repo, or None."""
    snaps = sorted((repo_dir(repo) / "snapshots").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for s in snaps:
        p = s / name
        if p.exists():
            return p
    return None


def _repo_bytes(repo):
    """Bytes on disk for a repo, counting partial downloads."""
    d = repo_dir(repo)
    if not d.exists():
        return 0
    total = sum(f.stat().st_size for f in _repo_files(d) if not f.name.endswith(".incomplete"))
    total += sum(p.stat().st_size for p in d.rglob("*.incomplete") if p.is_file())
    return total


def _incomplete(repo):
    d = repo_dir(repo)
    return d.exists() and any(d.rglob("*.incomplete"))


def model_parts(m):
    """What a catalog entry needs on disk: [(repo, [files] or None for whole repo)]."""
    if m.get("backend") == "gguf":
        return [(m["repo"], [f for f in (m.get("file"), m.get("mmproj")) if f])]
    parts = [(m["repo"], None)]
    if m.get("drafter"):
        parts.append((m["drafter"], None))
    return parts


def model_installed(m):
    for repo, files in model_parts(m):
        if _incomplete(repo):
            return False
        if files:
            if any(snapshot_file(repo, f) is None for f in files):
                return False
        elif not (repo_dir(repo).exists() and list((repo_dir(repo) / "snapshots").glob("*/*"))):
            return False
    return True


def model_bytes(m):
    return sum(_repo_bytes(repo) for repo, _ in model_parts(m))


downloads: dict[str, dict] = {}


def _download(mid, parts, kind=None):
    job = downloads[mid]
    log = open(LOGS / f"download-{mid}.log", "w")
    for repo, files in parts:
        cmd = [str(HF_CLI), "download", repo] + (files or [])
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        job["pid"] = proc.pid
        rc = proc.wait()
        if job.get("cancelled"):
            job.update(status="cancelled")
            return
        if rc != 0:
            job.update(status="error", returncode=rc)
            return
    job.update(status="done", finished=time.time())
    _activate_first(mid, kind)


def _activate_first(mid, kind):
    """First model of its kind on this Mac: make it the active one automatically."""
    if kind == "search":                              # the embedder is never the chat or image model
        return
    s = load_settings()
    key = "image_model" if kind == "image" else "chat_model"
    current = image_model(s[key]) if kind == "image" else chat_model(s[key])
    if not model_installed(current) or current["id"] == mid:
        s[key] = mid
        save_settings(s)


def start_download(m):
    mid = m["id"]
    if downloads.get(mid, {}).get("status") == "running":
        return downloads[mid]
    downloads[mid] = {"id": mid, "status": "running", "started": time.time()}
    kind = next((k for k in ("image", "search") if any(i["id"] == mid for i in CATALOG[k])), "chat")
    threading.Thread(target=_download, args=(mid, model_parts(m), kind), daemon=True).start()
    return downloads[mid]


def _row(m, kind, active_id, running):
    dl = downloads.get(m["id"], {})
    size = model_bytes(m)
    expected = int((m.get("size_gb") or 0) * 1e9)
    inst = model_installed(m)
    row = {**m, "kind": kind, "installed": inst, "size_bytes": size, "expected_bytes": expected,
           "downloading": dl.get("status") == "running", "download_error": dl.get("status") == "error",
           "progress": min(0.99, size / expected) if dl.get("status") == "running" and expected else None,
           "active": m["id"] == active_id and inst}
    if kind == "chat":
        row["running"] = running
    return row


@app.get("/api/models")
def models():
    s = load_settings()
    eng = engine_state()
    chat = [_row(m, "chat", s["chat_model"], eng.get("model_id") == m["id"] and eng.get("mode") is not None)
            for m in chat_models()]
    image = [_row(m, "image", s["image_model"], False) for m in CATALOG["image"]]
    known = set()
    for m in all_models():
        known |= {repo for repo, _ in model_parts(m)}
    other = []
    if HF_HUB.exists():
        for d in sorted(HF_HUB.glob("models--*")):
            repo = _repo_for_dir(d.name)
            if repo not in known:
                other.append({"repo": repo, "size_bytes": _repo_bytes(repo)})
    return {"chat": chat, "image": image, "search": [_row(m, "search", None, False) for m in CATALOG["search"]],
            "other": other, "llama_cpp": Path(LLAMA_SERVER).exists(), "disk_free_bytes": shutil.disk_usage(HOME).free}


def _find_any(mid):
    m = next((m for m in all_models() if m["id"] == mid), None)
    if not m:
        raise HTTPException(404, "unknown model")
    return m


@app.post("/api/models/download")
def download_model(id: str = Body(..., embed=True)):
    m = _find_any(id)
    if m.get("backend") == "gguf" and not Path(LLAMA_SERVER).exists():
        raise HTTPException(409, "llama.cpp isn't installed. Run: brew install llama.cpp")
    need = int((m.get("size_gb") or 0) * 1e9) - model_bytes(m)
    if need > shutil.disk_usage(HOME).free - 5e9:
        raise HTTPException(507, f"Not enough disk space: needs about {need / 1e9:.0f} GB.")
    return start_download(m)


@app.post("/api/models/cancel")
def cancel_download(id: str = Body(..., embed=True)):
    job = downloads.get(id)
    if not job or job.get("status") != "running":
        raise HTTPException(404, "no running download")
    job["cancelled"] = True
    if job.get("pid"):
        try:
            os.killpg(job["pid"], signal.SIGTERM)
        except OSError:
            pass
    return {"ok": True}


def _delete_repo(repo):
    d = repo_dir(repo)
    if not d.exists():
        return 0
    mine = _repo_files(d) | {p for p in d.rglob("*.incomplete") if p.is_file()}
    others = set()
    for od in HF_HUB.glob("models--*"):
        if od != d:
            others |= _repo_files(od)
    freed = 0
    for f in mine - others:
        if HF_HUB in f.parents:                     # never touch anything outside the cache
            freed += f.stat().st_size
            f.unlink(missing_ok=True)
    shutil.rmtree(d, ignore_errors=True)
    return freed


@app.delete("/api/models")
def delete_model(id: str = "", repo: str = ""):
    """Delete a catalog/custom model by id, or any cached repo by name."""
    if repo:
        if any(repo in {r for r, _ in model_parts(m)} for m in all_models()):
            raise HTTPException(400, "Delete it from the model library instead.")
        return {"ok": True, "freed_bytes": _delete_repo(repo)}
    m = _find_any(id)
    if m in CATALOG["search"] and load_settings()["search"]["enabled"]:
        raise HTTPException(409, "Search is using this model. Turn Search off first.")
    eng = engine_state()
    if eng.get("model_id") == m["id"] and eng.get("mode"):
        raise HTTPException(409, "This model is running. Stop the engine or switch models first.")
    shared = set()
    for other in all_models():
        if other["id"] != m["id"] and model_installed(other):
            shared |= {r for r, _ in model_parts(other)}
    freed = sum(_delete_repo(r) for r, _ in model_parts(m) if r not in shared)
    s = load_settings()
    if m.get("custom"):
        s["custom"] = [c for c in s["custom"] if c["id"] != m["id"]]
        if s["chat_model"] == m["id"]:
            s["chat_model"] = next(c["id"] for c in CATALOG["chat"] if c.get("default"))
        save_settings(s)
    return {"ok": True, "freed_bytes": freed}


@app.post("/api/models/active")
def set_active(id: str = Body(..., embed=True), kind: str = Body("chat", embed=True)):
    m = _find_any(id)
    if not model_installed(m):
        raise HTTPException(409, "Download the model first.")
    s = load_settings()
    if kind == "image":
        s["image_model"] = id
        save_settings(s)
        return {"ok": True}
    s["chat_model"] = id
    save_settings(s)
    eng = engine_state()
    if eng.get("mode") and eng.get("model_id") != id:      # swap the running engine in the background
        mode = "gguf" if m.get("backend") == "gguf" else "text"
        threading.Thread(target=_ensure_quiet, args=(mode, m), daemon=True).start()
    return {"ok": True}


# ---- Hugging Face search (for models outside the catalog)

@app.get("/api/hf/search")
async def hf_search(q: str, kind: str = "mlx"):
    params = {"search": q, "sort": "downloads", "direction": "-1", "limit": "25",
              "filter": "gguf" if kind == "gguf" else "mlx"}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://huggingface.co/api/models", params=params)
    if r.status_code != 200:
        raise HTTPException(502, "Hugging Face search failed")
    return [{"repo": m["id"], "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
             "pipeline": m.get("pipeline_tag")} for m in r.json()]


@app.get("/api/hf/files")
async def hf_files(repo: str):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"https://huggingface.co/api/models/{repo}/tree/main", params={"recursive": "true"})
    if r.status_code != 200:
        raise HTTPException(404, "Repository not found")
    files = [f for f in r.json() if f.get("type") == "file"]
    ggufs = [{"file": f["path"], "size": f.get("size", 0)} for f in files
             if f["path"].endswith(".gguf") and "mmproj" not in f["path"].lower()]
    mmproj = [{"file": f["path"], "size": f.get("size", 0)} for f in files
              if f["path"].endswith(".gguf") and "mmproj" in f["path"].lower()]
    return {"total": sum(f.get("size", 0) for f in files), "ggufs": ggufs, "mmproj": mmproj,
            "has_config": any(f["path"] == "config.json" for f in files)}


@app.post("/api/models/custom")
async def add_custom(repo: str = Body(..., embed=True), backend: str = Body("mlx", embed=True),
                     file: str = Body(None, embed=True), mmproj: str = Body(None, embed=True)):
    info = await hf_files(repo)
    if backend == "gguf":
        if not file:
            raise HTTPException(400, "Pick a GGUF file")
        size = next((g["size"] for g in info["ggufs"] if g["file"] == file), 0) + \
            next((g["size"] for g in info["mmproj"] if g["file"] == mmproj), 0)
    else:
        if not info["has_config"]:
            raise HTTPException(400, "That repository isn't an MLX model (no config.json).")
        size = info["total"]
    slug = re.sub(r"[^a-z0-9]+", "-", (repo + "-" + (file or "")).lower()).strip("-")[:60]
    base = re.sub(r"[-_](GGUF|gguf|MLX|mlx)$", "", repo.split("/")[-1]).replace("-", " ")
    quant = re.search(r"(UD-)?(I?Q\d[\w]*|BF16|F16|F32)(?=\.gguf$)", file or "")
    m = {"id": "custom-" + slug, "name": base + (f" · {quant.group(0)}" if quant else ""),
         "backend": backend, "repo": repo, "accel": "llama.cpp" if backend == "gguf" else "MLX",
         "vision": bool(mmproj), "size_gb": round(size / 1e9, 1), "custom": True,
         "blurb": "Added from Hugging Face."}
    if backend == "gguf":
        m["file"] = file
        if mmproj:
            m["mmproj"] = mmproj
    s = load_settings()
    s["custom"] = [c for c in s["custom"] if c["id"] != m["id"]] + [m]
    save_settings(s)
    start_download(m)
    return m

# ---------------------------------------------------------------- engines
ENGINES = {
    "text": {"pattern": r"^[^ ]*python[^ ]* [^ ]*mlx-dspark serve", "pid": "llm", "url": "http://127.0.0.1:8080", "ready": "/health"},
    "vision": {"pattern": r"^[^ ]*python[^ ]* -m mlx_vlm server", "pid": "vlm", "url": "http://127.0.0.1:8081", "ready": "/v1/models"},
    "gguf": {"pattern": r"^[^ ]*llama-server .*--port 8082", "pid": "gguf", "url": "http://127.0.0.1:8082", "ready": "/health"},
}
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
        cmd = [str(BIN / "mlx-dspark"), "serve", "--model", m["repo"], "--context-window", str(CONTEXT),
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
        cmd = [LLAMA_SERVER, "-m", str(model), "-c", str(CONTEXT), "-ngl", "999", "--jinja",
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


@app.post("/api/engine/start")
def engine_start():
    m = chat_model()
    if not model_installed(m):
        raise HTTPException(409, f"Download {m['name']} first (Model library).")
    mode = "gguf" if m.get("backend") == "gguf" else "text"
    threading.Thread(target=_ensure_quiet, args=(mode, m), daemon=True).start()
    return {"status": "starting", "mode": mode, "model_id": m["id"]}


@app.post("/api/engine/stop")
def engine_stop():
    with engine_lock:
        for mode in ENGINES:
            _stop_engine(mode)
    return {"status": "stopped"}

# ---------------------------------------------------------------- LLM proxy


@app.get("/api/llm/{route:path}")
async def llm_get(route: str):
    st = await asyncio.to_thread(engine_state)
    mode, m = st.get("mode"), chat_model(st.get("model_id"))
    if mode in ("vision", "gguf"):
        ready = await asyncio.to_thread(_engine_ready, mode)
        if route == "health":
            return {"status": "ok" if ready else "loading", "mode": mode, "target": m["repo"],
                    "model_id": m["id"], "model_name": m["name"], "drafter": m.get("drafter") if mode == "vision" else None,
                    "max_draft": None, "kv_bits": None, "context_window": CONTEXT, "warnings": []}
        return JSONResponse({"engine": mode}, status_code=200 if ready else 503)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ENGINES['text']['url']}/{route}")
        j = r.json()
        if route == "health" and isinstance(j, dict):
            j.update(model_id=m["id"], model_name=m["name"])
        return JSONResponse(j, status_code=r.status_code)
    except httpx.HTTPError as e:
        return JSONResponse({"status": "starting" if mode else "down", "model_id": m["id"], "model_name": m["name"],
                             "error": str(e)}, status_code=503)


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    m = chat_model(body.pop("model_id", None))
    messages, needs_vision = build_messages(body.get("messages", []))
    body["messages"] = messages
    # Keep prompt + reply inside the context window: long documents leave less room to answer.
    prompt_chars, images = 0, 0
    for msg in messages:
        c = msg.get("content")
        if isinstance(c, list):
            prompt_chars += sum(len(p.get("text", "")) for p in c if p.get("type") == "text")
            images += sum(1 for p in c if p.get("type") == "image_url")
        else:
            prompt_chars += len(c or "")
    room = CONTEXT - int(prompt_chars / 3.2) - images * 1200 - 512
    body["max_tokens"] = max(512, min(int(body.get("max_tokens") or 16384), room))
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})
    gguf = m.get("backend") == "gguf"
    mode = "gguf" if gguf else ("vision" if needs_vision else "text")

    async def relay():
        if needs_vision and not m.get("vision"):
            yield _sse({"error": f"{m['name']} can't read images. Switch to a model marked Vision "
                                 f"(for example Qwen3.8 27B) from the model menu."})
            return
        if not model_installed(m):
            yield _sse({"error": f"{m['name']} isn't downloaded yet. Get it from the Model library."})
            return
        st = await asyncio.to_thread(engine_state)
        if st.get("mode") != mode or st.get("model_id") != m["id"] or not await asyncio.to_thread(_engine_ready, mode):
            yield _sse({"x_engine": {"status": "switching", "mode": mode, "model": m["name"]}})
            try:
                await asyncio.to_thread(ensure_engine, mode, m)
            except Exception as e:
                yield _sse({"error": str(e)})
                return
            yield _sse({"x_engine": {"status": "ready", "mode": mode, "model": m["name"]}})
        url = ENGINES[mode]["url"]
        if mode == "vision":
            body["model"] = m["repo"]
        if mode == "gguf":
            body["model"] = m["id"]
            body["chat_template_kwargs"] = {"enable_thinking": bool(body.get("enable_thinking", True))}
        t0 = time.time()
        first, n_out, last_timings, usage = None, 0, None, None
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", f"{url}/v1/chat/completions", json=body) as r:
                    if r.status_code != 200:
                        err = (await r.aread()).decode(errors="replace")
                        yield _sse({"error": err, "status": r.status_code})
                        return
                    if mode == "text":
                        async for chunk in r.aiter_raw():
                            yield chunk
                        return
                    # vision / gguf: pass through and synthesize the per-reply stats block
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            continue
                        try:
                            j = json.loads(data)
                        except ValueError:
                            continue
                        d = ((j.get("choices") or [{}])[0] or {}).get("delta") or {}
                        if d.get("content") or d.get("reasoning_content") or d.get("reasoning"):
                            first = first or time.time()
                            n_out += 1
                        last_timings = j.get("timings") or last_timings
                        usage = j.get("usage") or usage
                        yield _sse(j)
        except httpx.HTTPError as e:
            yield _sse({"error": f"Engine unreachable: {e}"})
            return
        end = time.time()
        t = last_timings or {}
        tps = t.get("predicted_per_second")
        out = (usage or {}).get("completion_tokens") or t.get("predicted_n") or n_out
        stats = {"mode": mode, "engine": "llama.cpp" if gguf else "mlx-vlm", "model": m["name"],
                 "decode_tokens_per_sec": round(tps or (out / max(end - (first or end), 1e-6)), 1),
                 "tokens_per_sec": round(out / max(end - t0, 1e-6), 1), "completion_tokens": out,
                 "prompt_tokens": (usage or {}).get("prompt_tokens") or t.get("prompt_n"),
                 "ttft_seconds": round((first or end) - t0, 3), "decode_seconds": round(end - (first or end), 3)}
        if t.get("prompt_per_second"):
            stats["prefill_tokens_per_sec"] = round(t["prompt_per_second"], 1)
        if t.get("cache_n") is not None:
            stats["cached_tokens"] = t["cache_n"]
        if t.get("draft_n"):
            stats["accept_len"] = round(t.get("draft_n_accepted", 0) / max(t["draft_n"], 1), 2)
        yield _sse({"x_mlx_dspark": stats})
        yield "data: [DONE]\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream")

# ---------------------------------------------------------------- attachments
# Files are processed once on upload: images are downscaled, documents become plain text
# (scanned PDFs become page images). Chats store only attachment ids.

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
            ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java",
            ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".zsh", ".sql", ".toml",
            ".ini", ".cfg", ".log", ".r", ".m", ".scala", ".lua", ".pl", ".dart", ".vue", ".svelte"}
MAX_DOC_CHARS = 80_000            # ~20k tokens: leaves room in the 32k context for the reply
MAX_IMAGE_SIDE = 1536
MAX_SCAN_PAGES = 4
_AID = re.compile(r"^[a-f0-9]{16}$")


def _save_image(img, path):
    from PIL import Image
    img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
    img.save(path, "JPEG", quality=90)
    return img.size


def _extract(path: Path, ext: str, aid: str) -> dict:
    """Return {kind, text?, images?, pages?} for an uploaded file."""
    if ext in IMAGE_EXT:
        from PIL import Image
        if ext == ".heic":
            raise HTTPException(415, "HEIC isn't supported yet; export the photo as JPEG or PNG.")
        w, h = _save_image(Image.open(path), UPLOADS / f"{aid}-0.jpg")
        return {"kind": "image", "images": [f"{aid}-0.jpg"], "width": w, "height": h}
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
        text_chars = sum(len(t) for t in pages)
        if text_chars < 80 * max(1, len(pages)) and len(pages) and text_chars < 2000:
            # scanned / image-only PDF: hand the first pages to a vision engine
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(str(path))
            imgs = []
            for i in range(min(MAX_SCAN_PAGES, len(doc))):
                name = f"{aid}-{i}.jpg"
                _save_image(doc[i].render(scale=2).to_pil(), UPLOADS / name)
                imgs.append(name)
            return {"kind": "image", "images": imgs, "pages": len(pages), "scanned": True}
        text = "\n\n".join(f"--- Page {i + 1} ---\n{t}" for i, t in enumerate(pages) if t)
        return {"kind": "document", "text": text, "pages": len(pages)}
    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        parts = []
        for p in d.paragraphs:
            if p.text.strip():
                style = (p.style.name or "").lower() if p.style is not None else ""
                parts.append(("#" * int(style[-1]) + " " if style.startswith("heading") and style[-1].isdigit() else "") + p.text)
        for t in d.tables:
            rows = [" | ".join(c.text.strip() for c in r.cells) for r in t.rows]
            parts.append("\n".join(rows))
        return {"kind": "document", "text": "\n\n".join(parts)}
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                if any(v is not None for v in row):
                    rows.append(" | ".join("" if v is None else str(v) for v in row))
                if len(rows) >= 2000:
                    rows.append("… (more rows not shown)")
                    break
            parts.append(f"## Sheet: {ws.title}\n" + "\n".join(rows))
        return {"kind": "document", "text": "\n\n".join(parts), "sheets": len(wb.worksheets)}
    raw = path.read_bytes()
    if ext in TEXT_EXT or b"\x00" not in raw[:4096]:
        return {"kind": "document", "text": raw.decode("utf-8", errors="replace")}
    raise HTTPException(415, f"Unsupported file type: {ext or 'unknown'}")


@app.post("/api/attachments")
async def upload_attachment(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 60 * 2**20:
        raise HTTPException(413, "File is larger than 60 MB.")
    name = Path(file.filename or "file").name
    ext = Path(name).suffix.lower()
    aid = uuid.uuid4().hex[:16]
    src = UPLOADS / f"{aid}-src{ext}"
    src.write_bytes(data)
    try:
        info = await asyncio.to_thread(_extract, src, ext, aid)
    except HTTPException:
        src.unlink(missing_ok=True)
        raise
    except Exception as e:
        src.unlink(missing_ok=True)
        raise HTTPException(422, f"Couldn't read {name}: {e}")
    meta = {"id": aid, "name": name, "size": len(data), **{k: v for k, v in info.items() if k != "text"}}
    if "text" in info:
        text = info["text"].strip()
        if not text:
            src.unlink(missing_ok=True)
            raise HTTPException(422, f"No readable text found in {name}.")
        meta["chars"] = len(text)
        meta["truncated"] = len(text) > MAX_DOC_CHARS
        (UPLOADS / f"{aid}.txt").write_text(text[:MAX_DOC_CHARS])
        meta["tokens_est"] = min(len(text), MAX_DOC_CHARS) // 4
    if ext not in (".pdf", ".docx", ".xlsx", ".xlsm"):
        src.unlink(missing_ok=True)                       # images/text are fully captured above
    (UPLOADS / f"{aid}.json").write_text(json.dumps(meta))
    return meta


@app.get("/api/attachments/{aid}/{name}")
def attachment_file(aid: str, name: str):
    if not _AID.match(aid) or not re.match(r"^[a-f0-9]{16}-\d+\.jpg$", name) or not name.startswith(aid):
        raise HTTPException(400, "bad path")
    p = UPLOADS / name
    if not p.exists():
        raise HTTPException(404, "missing")
    return FileResponse(p, media_type="image/jpeg")


def _attachment_meta(aid):
    if not isinstance(aid, str) or not _AID.match(aid):
        return None
    p = UPLOADS / f"{aid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def build_messages(messages):
    """Expand attachment ids into model input. Documents become <document> blocks in the
    text; images become image_url parts (which also means a vision engine is needed)."""
    out, needs_vision = [], False
    for m in messages:
        atts = [a for a in (m.get("attachments") or [])]
        msg = {k: v for k, v in m.items() if k != "attachments"}
        if not atts:
            out.append(msg)
            continue
        docs, images = [], []
        for aid in atts:
            meta = _attachment_meta(aid)
            if meta is None:
                docs.append("[An attached file is no longer available.]")
                continue
            if meta["kind"] == "document":
                txt = (UPLOADS / f"{aid}.txt")
                body = txt.read_text() if txt.exists() else "[file content missing]"
                note = " (truncated)" if meta.get("truncated") else ""
                docs.append(f'<document name="{meta["name"]}"{note}>\n{body}\n</document>')
            else:
                for name in meta.get("images", []):
                    p = UPLOADS / name
                    if p.exists():
                        images.append("data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode())
                if meta.get("scanned"):
                    docs.append(f'[Scanned PDF "{meta["name"]}": first {len(meta.get("images", []))} '
                                f'of {meta.get("pages")} pages attached as images.]')
        text = "\n\n".join(docs + [msg.get("content") or ""]).strip()
        if images:
            needs_vision = True
            msg["content"] = [{"type": "image_url", "image_url": {"url": u}} for u in images] + \
                             [{"type": "text", "text": text or "Describe this image."}]
        else:
            msg["content"] = text
        out.append(msg)
    return out, needs_vision

# ---------------------------------------------------------------- saved conversations

_ID = re.compile(r"^[a-f0-9]{8,32}$")


def _chat_path(cid):
    if not _ID.match(cid):
        raise HTTPException(400, "bad id")
    return CHATS / f"{cid}.json"


@app.get("/api/chats")
def list_chats():
    out = []
    for p in CHATS.glob("*.json"):
        try:
            c = json.loads(p.read_text())
            out.append({"id": c["id"], "title": c.get("title", "Untitled"), "updated": c.get("updated", 0)})
        except Exception:
            continue
    return sorted(out, key=lambda c: c["updated"], reverse=True)


@app.get("/api/chats/{cid}")
def get_chat(cid: str):
    p = _chat_path(cid)
    if not p.exists():
        raise HTTPException(404, "not found")
    return json.loads(p.read_text())


@app.put("/api/chats/{cid}")
def put_chat(cid: str, chat: dict = Body(...)):
    chat["id"] = cid
    chat["updated"] = time.time()
    _chat_path(cid).write_text(json.dumps(chat))
    return {"ok": True, "updated": chat["updated"]}


@app.delete("/api/chats/{cid}")
def delete_chat(cid: str):
    _chat_path(cid).unlink(missing_ok=True)
    return {"ok": True}

# ---------------------------------------------------------------- image jobs

jobs: dict[str, dict] = {}
image_lock = threading.Lock()
STEP_RE = re.compile(r"(\d+)/(\d+) \[")
PEAK_RE = re.compile(r"(\d+)\s+peak memory footprint")
TIME_STAT_RE = re.compile(r"^\d+(\.\d+)?\s+[a-z ]+$|\breal\b.*\buser\b.*\bsys\b")   # /usr/bin/time -l noise


def run_image_job(job_id, cmd, meta):
    job = jobs[job_id]
    with image_lock:                      # one image at a time: each load is several GB
        if job.get("cancelled"):
            job.update(status="cancelled")
            return
        job.update(status="running", started=time.time())
        # /usr/bin/time -l reports the process's peak memory footprint (incl. Metal buffers)
        proc = subprocess.Popen(["/usr/bin/time", "-l", *cmd], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        job["pid"] = proc.pid
        buf, tail = "", []
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                line = buf.strip()
                buf = ""
                if not line:
                    continue
                m = STEP_RE.search(line)
                if m:
                    step, total = int(m.group(1)), int(m.group(2))
                    job.update(step=step, total=total)
                    if step == 1:
                        job["first_step_at"] = time.time()
                    if step == total:
                        job["denoise_done_at"] = time.time()
                elif (p := PEAK_RE.search(line)):
                    job["peak_bytes"] = int(p.group(1))
                elif not TIME_STAT_RE.search(line):
                    tail = (tail + [line])[-15:]
                    job["log"] = tail
            else:
                buf += ch
        proc.wait()
        end = time.time()
        job["elapsed"] = round(end - job["started"], 1)
        if job.get("first_step_at") and job.get("denoise_done_at") and job.get("total", 0) > 1:
            job["sec_per_step"] = round((job["denoise_done_at"] - job["first_step_at"])
                                        / (job["total"] - 1), 2)
        out = GALLERY / meta["file"]
        if job.get("cancelled"):
            out.unlink(missing_ok=True)
            job.update(status="cancelled")
        elif proc.returncode == 0 and out.exists():
            meta.update(elapsed=job["elapsed"], sec_per_step=job.get("sec_per_step"),
                        peak_bytes=job.get("peak_bytes"), created=end)
            (GALLERY / (out.stem + ".json")).write_text(json.dumps(meta, indent=2))
            job.update(status="done", image=f"/gallery/{meta['file']}", meta=meta)
        else:
            job.update(status="error", returncode=proc.returncode)


@app.post("/api/image")
async def create_image(prompt: str = Form(...), width: int = Form(1024), height: int = Form(1024),
                       steps: int = Form(20), seed: str = Form(""), guidance: str = Form(""),
                       negative_prompt: str = Form(""), step_cache: str = Form(""),
                       mode: str = Form("generate"), model_id: str = Form(""),
                       images: list[UploadFile] = File(default=[])):
    if mode == "edit" and not EDIT_MODEL:
        raise HTTPException(400, "Editing needs the full Qwen-Image-2.1 checkpoint; the 4-bit "
                                 "weights installed here are text-to-image only.")
    im = image_model(model_id or None)
    if mode != "edit" and not model_installed(im):
        raise HTTPException(409, f"{im['name']} isn't downloaded. Get it from the Model library.")
    seed_val = int(seed) if seed.strip() else secrets.randbelow(2**31)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fname = f"{stamp}-{seed_val}-{uuid.uuid4().hex[:6]}.png"      # unique even for jobs queued together
    meta = {"prompt": prompt, "mode": mode, "width": width, "height": height, "steps": steps,
            "seed": seed_val, "model": im["repo"], "model_name": im["name"], "file": fname, "low_ram": True}

    if mode == "edit":
        if not images:
            raise HTTPException(400, "edit mode needs at least one reference image")
        paths = []
        for up in images[:10]:
            p = UPLOADS / f"{uuid.uuid4().hex}{Path(up.filename or 'x.png').suffix}"
            p.write_bytes(await up.read())
            paths.append(str(p))
        cmd = [str(BIN / "mflux-generate-qwen-2.1-edit"), "--image-paths", *paths, "--quantize", "4"]
        meta["references"] = [Path(p).name for p in paths]
    else:
        cmd = [str(BIN / im["command"])]
        if im.get("base_model"):
            cmd += ["--base-model", im["base_model"]]
    # --low-ram evicts the text encoder/transformer between phases and tiles the VAE:
    # peak ~10 GB instead of ~38 GB for Qwen-Image, ~10% slower — it keeps chat out of swap.
    cmd += ["--low-ram", "--model", EDIT_MODEL if mode == "edit" else im["repo"],
            "--prompt", prompt, "--steps", str(steps), "--seed", str(seed_val),
            "--width", str(width), "--height", str(height), "--output", str(GALLERY / fname)]
    if negative_prompt.strip():
        cmd += ["--negative-prompt", negative_prompt]
        meta["negative_prompt"] = negative_prompt
    if guidance.strip():
        cmd += ["--guidance", guidance]
        meta["guidance"] = float(guidance)
    if step_cache.strip() and mode != "edit" and im["id"] == "qwen-image-2.1":
        cmd += ["--step-cache-ratio", step_cache]
        meta["step_cache_ratio"] = float(step_cache)

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"id": job_id, "status": "queued", "step": 0, "total": steps,
                    "queued": time.time(), "log": []}
    threading.Thread(target=run_image_job, args=(job_id, cmd, meta), daemon=True).start()
    return {"job": job_id, "seed": seed_val}


@app.get("/api/image/{job_id}")
def image_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    view = {k: v for k, v in job.items() if k != "pid"}
    if job.get("started") and job["status"] == "running":
        view["elapsed"] = round(time.time() - job["started"], 1)
    return view


@app.post("/api/image/{job_id}/cancel")
def image_cancel(job_id: str):
    job = jobs.get(job_id)
    if not job or job["status"] not in ("queued", "running"):
        raise HTTPException(404, "no running job")
    job["cancelled"] = True
    if job.get("pid"):
        subprocess.run(["pkill", "-TERM", "-P", str(job["pid"])])   # mflux, child of /usr/bin/time
    return {"ok": True}


@app.get("/api/gallery")
def gallery():
    items = []
    for meta_file in sorted(GALLERY.glob("*.json"), reverse=True):
        try:
            meta = json.loads(meta_file.read_text())
            meta["url"] = f"/gallery/{meta['file']}"
            items.append(meta)
        except Exception:
            continue
    return items


@app.delete("/api/gallery/{name}")
def delete_image(name: str):
    if not re.match(r"^[\w.-]+\.png$", name):
        raise HTTPException(400, "bad name")
    (GALLERY / name).unlink(missing_ok=True)
    (GALLERY / (Path(name).stem + ".json")).unlink(missing_ok=True)
    return {"ok": True}

# ---------------------------------------------------------------- versions & updates

update_job: dict = {"status": "idle"}


def _versions():
    v = {"app": APP_VERSION}
    try:
        out = subprocess.run([UV, "tool", "list"], capture_output=True, text=True, timeout=20).stdout
        for name in ("mlx-dspark", "mflux"):
            m = re.search(rf"^{re.escape(name)} v(\S+)", out, re.M)
            v[name] = m.group(1) if m else None
    except Exception:
        pass
    try:
        out = subprocess.run([LLAMA_SERVER, "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"version: (\S+)", out.stdout + out.stderr)
        v["llama.cpp"] = m.group(1) if m else None
    except Exception:
        v["llama.cpp"] = None
    try:
        out = subprocess.run([str(DSPARK_PY), "-c", "import importlib.metadata as m; print(m.version('mlx-vlm'), m.version('mlx'))"],
                             capture_output=True, text=True, timeout=20).stdout.split()
        v["mlx-vlm"], v["mlx"] = out[0], out[1]
    except Exception:
        pass
    return v


@app.get("/api/versions")
async def versions():
    return {**(await asyncio.to_thread(_versions)), "update": {k: v for k, v in update_job.items() if k != "proc"}}


def _run_update():
    log = open(LOGS / "update.log", "w")
    steps = [[UV, "tool", "upgrade", "mlx-dspark"], [UV, "tool", "upgrade", "mflux"]]
    if Path(BREW).exists() and not LLAMA_SERVER.startswith(str(APP_SUPPORT)):
        steps.append([BREW, "upgrade", "llama.cpp"] if Path(LLAMA_SERVER).exists() else [BREW, "install", "llama.cpp"])
    else:                                             # no Homebrew: refresh the app-managed build
        steps.append(["/bin/zsh", str(ROOT / "setup.sh"), "--llama"])
    before = _versions()
    for cmd in steps:
        update_job["step"] = " ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd))
        log.write(f"$ {update_job['step']}\n")
        log.flush()
        rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        if rc != 0 and "upgrade" in cmd and cmd[0] == BREW:
            continue                                  # brew upgrade exits 0 when already current
    after = _versions()
    changed = {k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k)}
    update_job.update(status="done", finished=time.time(), changed=changed, step=None)


@app.post("/api/versions/update")
def update_engines():
    if update_job.get("status") == "running":
        return update_job
    update_job.clear()
    update_job.update(status="running", started=time.time())
    threading.Thread(target=_run_update, daemon=True).start()
    return update_job

# ---------------------------------------------------------------- storage & uninstall


@app.get("/api/storage")
def storage():
    engine_cache = HOME / ".cache/mlx_dspark"
    return {
        "gallery": {"bytes": dir_size(GALLERY), "count": len(list(GALLERY.glob("*.png")))},
        "uploads": {"bytes": dir_size(UPLOADS), "count": len(list(UPLOADS.glob("*.json")))},
        "chats": {"bytes": dir_size(CHATS), "count": len(list(CHATS.glob("*.json")))},
        "logs": {"bytes": dir_size(LOGS), "count": len(list(LOGS.glob("*.log")))},
        "engine_cache": {"bytes": dir_size(engine_cache) if engine_cache.exists() else 0},
        "disk_free_bytes": shutil.disk_usage(HOME).free,
        "root": str(STATE),
        "bundled": STATE != ROOT,
    }


@app.post("/api/storage/clear")
def clear_storage(what: str = Body(..., embed=True)):
    if what == "gallery":
        for p in GALLERY.iterdir():
            p.unlink(missing_ok=True)
    elif what == "uploads":
        for p in UPLOADS.iterdir():
            p.unlink(missing_ok=True)
    elif what == "chats":
        for p in CHATS.glob("*.json"):
            p.unlink(missing_ok=True)
    elif what == "logs":
        for p in LOGS.glob("*.log"):
            p.write_text("")                    # truncate: running servers still hold them open
    elif what == "engine_cache":
        shutil.rmtree(HOME / ".cache/mlx_dspark", ignore_errors=True)
    else:
        raise HTTPException(400, "unknown target")
    return {"ok": True}


@app.post("/api/uninstall")
def uninstall(remove_app: bool = Body(False, embed=True)):
    log = HOME / "local-studio-uninstall.log"
    args = ["/bin/zsh", str(ROOT / "uninstall.sh"), "--yes"] + (["--remove-app"] if remove_app else [])
    subprocess.Popen(args, stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    return {"ok": True, "log": str(log)}


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")
