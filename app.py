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
import queue
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

APP_VERSION = "1.3.0"

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
# Testing: LOCAL_STUDIO_SEARCH_PORT gives a development server its own search service, so it can
# never reach or stop the one belonging to the app the user is running.
SEARCH_PORT = int(os.environ.get("LOCAL_STUDIO_SEARCH_PORT", 8083))
SEARCH_URL = f"http://127.0.0.1:{SEARCH_PORT}"                # search.py, see the search section
SEARCH_DIR = DATA / "search"                                 # its index
SEARCH_REQS = ROOT / "search-requirements.txt"
SEARCH_DEPS_OK = DATA / "search-deps.ok"                     # written by setup.sh --search
SEARCH_DEFAULTS = {"enabled": False, "folders": [], "removed": [], "paused": False, "on_battery": False}
# Local Agent: drives the Mac through CuaDriver (MCP over a unix socket in standard mode, no manifest).
AGENT_DIR = DATA / "agent"
AGENT_RUNS = AGENT_DIR / "runs"
AGENT_SOCK = AGENT_DIR / "cua.sock"                          # absolute; a unix socket path must stay under 104 bytes
CUA_DRIVER = shutil.which("cua-driver") or str(BIN / "cua-driver")
CUA_MIN = (0, 28, 2)
AGENT_DEFAULTS = {"enabled": False, "max_steps": 40, "max_minutes": 15, "tiers": {}}   # tiers: app name -> read|ask|allowed
AGENT_BUSY_MSG = "The agent is running a task. Stop it or wait for it to finish."
agent_lock = threading.Lock()                                # held for the length of an agent run
agent_engine = {"mode": None, "model_id": None}              # the engine the run uses, set by the loop

CATALOG = json.loads((ROOT / "catalog.json").read_text())

app = FastAPI()
app.mount("/gallery", StaticFiles(directory=GALLERY), name="gallery")

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _local_origin(host, origin):
    """A request from this app's own page or a local script: a loopback Host, and a loopback
    Origin if a browser sent one."""
    name = lambda netloc: (netloc or "").rsplit(":", 1)[0].strip("[]")                    # noqa: E731
    return name(host) in LOOPBACK and (not origin or name(origin.split("://", 1)[-1]) in LOOPBACK)


class LocalOnly:
    """Search, "My files" and the agent return the text of the user's files or drive the Mac, so a
    page on another site (or a rebound DNS name) must not be able to call them. Plain ASGI, so streaming is untouched."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (scope["path"].startswith(("/api/search", "/api/agent")) or scope["path"] == "/api/chat"):
            h = dict(scope["headers"])
            if not _local_origin(h.get(b"host", b"").decode("latin-1"), h.get(b"origin", b"").decode("latin-1")):
                return await JSONResponse({"detail": "Forbidden"}, status_code=403)(scope, receive, send)
        await self.inner(scope, receive, send)


app.add_middleware(LocalOnly)

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
    s["agent"] = {**AGENT_DEFAULTS, **(s.get("agent") or {})}
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
    if agent_lock.locked():
        raise HTTPException(409, AGENT_BUSY_MSG)
    m = chat_model()
    if not model_installed(m):
        raise HTTPException(409, f"Download {m['name']} first (Model library).")
    mode = "gguf" if m.get("backend") == "gguf" else "text"
    threading.Thread(target=_ensure_quiet, args=(mode, m), daemon=True).start()
    return {"status": "starting", "mode": mode, "model_id": m["id"]}


@app.post("/api/engine/stop")
def engine_stop():
    if agent_lock.locked():
        raise HTTPException(409, AGENT_BUSY_MSG)
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


FILES_PROMPT = ("Answer using these excerpts from the user's files. Name the files you used. "
                "If they don't contain the answer, say so.")


def _add_file_passages(messages):
    """"My files": put the best-matching passages from the search index in front of the latest
    user message. Returns the sources, or [] (message untouched) when Search is off or nothing matched."""
    last = messages[-1] if messages else None             # only the turn being answered, never an earlier one
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), str) or not last["content"].strip():
        return []
    try:
        r = httpx.post(SEARCH_URL + "/passages", json={"query": last["content"], "limit": 6, "max_chars": 12000}, timeout=30)
        passages = r.json()["passages"] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, KeyError):
        return []
    if not passages:
        return []
    block = "\n\n".join(f"[{i + 1}] {p['name']}" + (f" ({p['locator']})" if p["locator"] else "") + f"\n{p['text']}"
                        for i, p in enumerate(passages))
    last["content"] = f"{FILES_PROMPT}\n\n{block}\n\nQuestion: {last['content']}"
    return [{k: p[k] for k in ("file_id", "name", "path", "locator")} for p in passages]


active_chats = 0


async def _counted(gen):
    """Count open chat streams for /api/busy. The finally also runs when the user stops a reply."""
    global active_chats
    active_chats += 1
    try:
        async for chunk in gen:
            yield chunk
    finally:
        active_chats -= 1


@app.get("/api/agent/status")
def agent_status():
    return {"enabled": load_settings()["agent"]["enabled"]}


@app.get("/api/busy")
def api_busy():
    """Read by the search service between batches: it waits while the Mac is doing user-facing work."""
    return {"busy": active_chats > 0 or agent_lock.locked() or any(j.get("status") in ("queued", "running") for j in jobs.values())}


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    m = chat_model(body.pop("model_id", None))
    # Before build_messages, so the passages count towards the context-size estimate below.
    sources = await asyncio.to_thread(_add_file_passages, body.get("messages", [])) if body.pop("use_files", False) else []
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
        # The agent holds the machine: only its own engine may answer (requests run one at a time).
        if agent_lock.locked() and (mode, m["id"]) != (agent_engine["mode"], agent_engine["model_id"]):
            yield _sse({"error": AGENT_BUSY_MSG})
            return
        if sources:
            yield _sse({"x_sources": sources})
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

    return StreamingResponse(_counted(relay()), media_type="text/event-stream")

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
    if agent_lock.locked():
        raise HTTPException(409, AGENT_BUSY_MSG)
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

# ---------------------------------------------------------------- search
# search.py is a fourth process (port 8083): it indexes the user's files and answers queries. At
# ~2 GB it stays loaded next to a chat engine, so ensure_engine never touches it. It is off until
# the user turns it on; that is also when its dependencies and model are fetched.

search_job: dict = {"status": "idle"}


def search_deps_installed():
    """setup.sh --search records the requirements file it installed; a changed file means reinstall."""
    try:
        return SEARCH_DEPS_OK.read_text().strip() == hashlib.sha256(SEARCH_REQS.read_bytes()).hexdigest()
    except OSError:
        return False


def _search_running():
    try:
        return httpx.get(SEARCH_URL + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _search_process():
    """True while a search.py process exists, including one that is still loading."""
    return subprocess.run(["pgrep", "-f", rf"^[^ ]*python[^ ]* search.py --port {SEARCH_PORT}"], capture_output=True).returncode == 0


def _start_search():
    if _search_running() or _search_process():
        return
    log = open(LOGS / "search.log", "a")
    # Relative file names with cwd=ROOT: the bundled path contains a space, which would defeat
    # stop.sh's pkill patterns.
    proc = subprocess.Popen([UV, "run", "--no-project", "--python", "3.12", "--with-requirements", SEARCH_REQS.name,
                             "python", "search.py", "--port", str(SEARCH_PORT)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "LOCAL_STUDIO_HOME": str(STATE)}, start_new_session=True)
    (LOGS / "search.pid").write_text(str(proc.pid))


def _stop_search():
    pid = _read_pid("search")
    if pid and _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)            # uv and its python child share the session
        except OSError:
            pass
    subprocess.run(["pkill", "-f", rf"^[^ ]*uv run .*python search.py --port {SEARCH_PORT}"])
    subprocess.run(["pkill", "-f", rf"^[^ ]*python[^ ]* search.py --port {SEARCH_PORT}"])
    (LOGS / "search.pid").unlink(missing_ok=True)
    for _ in range(40):                               # wait for the port to free before any restart
        if not _search_running():
            break
        time.sleep(0.25)


def _enable_search():
    """Background job behind the Search switch: install what's missing, then start the service."""
    try:
        if not search_deps_installed():
            search_job.update(step="Installing search components (about 1 GB, one time)")
            with open(LOGS / "search-setup.log", "w") as log:
                rc = subprocess.run(["/bin/zsh", str(ROOT / "setup.sh"), "--search"], stdout=log, stderr=subprocess.STDOUT,
                                    env={**os.environ, "LOCAL_STUDIO_HOME": str(STATE)}).returncode
            if rc != 0 or not search_deps_installed():
                raise RuntimeError("Couldn't install the search components. See logs/search-setup.log.")
        m = search_model()
        if not model_installed(m):
            search_job.update(step=f"Downloading {m['name']} ({m['size_gb']} GB)")
            start_download(m)
            while downloads[m["id"]].get("status") == "running":
                time.sleep(1)
            if not model_installed(m):
                raise RuntimeError("The model download didn't finish. Check your connection and try again.")
        search_job.update(step="Starting Search")
        s = load_settings()
        s["search"]["enabled"] = True
        save_settings(s)
        _start_search()
        for _ in range(180):                          # the first start imports PyTorch and MLX
            if _search_running():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Search didn't start. See logs/search.log.")
        search_job.update(status="done", step=None)
    except Exception as e:
        search_job.update(status="error", error=str(e), step=None)


def _search_proxy(method, path, **kw):
    try:
        r = httpx.request(method, SEARCH_URL + path, timeout=60, **kw)
    except httpx.HTTPError:
        raise HTTPException(503, "Search isn't running. Turn it on in Settings.")
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        raise HTTPException(r.status_code, detail or "Search error")
    return r


@app.get("/api/search/status")
def search_status():
    m = search_model()
    out = {**load_settings()["search"], "installed": search_deps_installed(), "model_installed": model_installed(m),
           "model": {"name": m["name"], "size_gb": m["size_gb"]}, "index_on_disk": SEARCH_DIR.exists(), "running": False,
           "setup": {k: search_job.get(k) for k in ("status", "step", "error")}}
    try:
        r = httpx.get(SEARCH_URL + "/status", timeout=3)
        if r.status_code == 200:
            out.update(r.json(), running=True)
    except httpx.HTTPError:
        pass
    return out


@app.post("/api/search")
def search_query(body: dict = Body(...)):
    return _search_proxy("POST", "/search", json=body).json()


@app.post("/api/search/enable")
def search_enable():
    if search_job.get("status") != "running":
        search_job.clear()
        search_job.update(status="running", step="Starting")
        threading.Thread(target=_enable_search, daemon=True).start()
    return {k: search_job.get(k) for k in ("status", "step", "error")}


@app.post("/api/search/disable")
def search_disable():
    s = load_settings()
    s["search"]["enabled"] = False
    save_settings(s)
    _stop_search()
    return {"ok": True}


@app.post("/api/search/start")
def search_start():
    """Called by start.sh: bring the service up if the user has Search turned on."""
    if not load_settings()["search"]["enabled"]:
        return {"status": "off"}
    if search_deps_installed() and model_installed(search_model()):
        _start_search()
        return {"status": "starting"}
    search_enable()                                   # an update changed the components (or the model is gone): set up again
    return {"status": "setting up"}


@app.put("/api/search/settings")
def search_settings(body: dict = Body(...)):
    s = load_settings()
    for key in ("folders", "removed"):
        if key in body:
            paths = [str(Path(p.strip()).expanduser()) for p in body[key] if isinstance(p, str) and p.strip()]
            if key == "folders":
                bad = next((p for p in paths if not Path(p).is_absolute() or not Path(p).is_dir()), None)
                if bad:
                    raise HTTPException(400, f"Not a folder on this Mac: {bad}")
            s["search"][key] = list(dict.fromkeys(paths))
    for key in ("paused", "on_battery"):
        if key in body:
            s["search"][key] = bool(body[key])
    save_settings(s)
    try:
        httpx.post(SEARCH_URL + "/reload", timeout=3)
    except httpx.HTTPError:
        pass
    return s["search"]


def _reset_index(restart):
    _stop_search()
    shutil.rmtree(SEARCH_DIR, ignore_errors=True)
    if restart:
        _start_search()
    return {"ok": True}


@app.post("/api/search/rebuild")
def search_rebuild():
    if not load_settings()["search"]["enabled"]:
        raise HTTPException(409, "Turn Search on first.")
    return _reset_index(restart=True)


@app.delete("/api/search/index")
def search_delete_index():
    return _reset_index(restart=load_settings()["search"]["enabled"])


@app.get("/api/search/errors")
def search_errors():
    return _search_proxy("GET", "/errors").json()


@app.get("/api/search/thumb/{file_id}")
def search_thumb(file_id: int, t: int = 0):
    r = _search_proxy("GET", f"/thumb/{file_id}", params={"t": t})
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/search/preview/{file_id}")
def search_preview(file_id: int, chunk_id: int | None = None):
    return _search_proxy("GET", f"/preview/{file_id}", params={"chunk_id": chunk_id} if chunk_id else None).json()


@app.get("/api/search/page/{file_id}/{page}")
def search_page(file_id: int, page: int, chunk_id: int | None = None):
    r = _search_proxy("GET", f"/page/{file_id}/{page}", params={"chunk_id": chunk_id} if chunk_id else None)
    return Response(r.content, media_type="image/jpeg", headers={"X-Pages": r.headers.get("x-pages", ""), "Cache-Control": "no-store"})


@app.get("/api/search/image/{file_id}")
def search_image(file_id: int):
    return Response(_search_proxy("GET", f"/image/{file_id}").content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/search/media/{file_id}")
def search_media(file_id: int):
    """The audio or video file itself, for the preview player (FileResponse handles the player's
    range requests). The path comes from the index, never from the request, and nothing else is served."""
    f = _search_proxy("GET", f"/file/{file_id}").json()
    if f.get("kind") not in ("audio", "video") or not Path(f["path"]).is_file():
        raise HTTPException(404, "No player preview for this file.")
    return FileResponse(f["path"])


@app.post("/api/search/open")
def search_open(file_id: int = Body(...), path: str = Body(...), reveal: bool = Body(False)):
    """Open or reveal an indexed file. The path comes from the index, never from the request: the
    request's path only has to match, so an id reused after a rebuild can't open a different file."""
    try:
        f = _search_proxy("GET", f"/file/{file_id}").json()
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "This file is no longer in the search index.")
        raise
    if f["path"] != path or not Path(f["path"]).exists():
        raise HTTPException(404, "This file has moved or is no longer in the search index.")
    subprocess.run(["open", "-R", f["path"]] if reveal else ["open", f["path"]])
    return {"ok": True}


@app.post("/api/search/choose-folder")
def search_choose_folder():
    """The standard macOS folder chooser. `tell me to activate` brings it in front of the app window."""
    try:
        r = subprocess.run(["osascript", "-e", "tell me to activate", "-e",
                            'POSIX path of (choose folder with prompt "Choose a folder to index")'],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"path": None}
    return {"path": r.stdout.strip().rstrip("/") or None if r.returncode == 0 else None}


# ---------------------------------------------------------------- agent

AGENT_CUA_TOOLS = frozenset((
    "start_session end_session get_session check_permissions health_report "
    "browser_prepare get_browser_state browser_navigate browser_click browser_type browser_pointer browser_dialog browser_download "
    "list_apps list_windows launch_app get_window_state click double_click type_text set_value press_key hotkey scroll invoke_menu zoom bring_to_front"
).split())                                                   # everything else CuaDriver offers (kill_app, clipboard_*, set_config, ...) is never reachable
AGENT_IDLE_S = 600                                           # stop the daemon this long after the last call when no run is active
AGENT_SOCK_WAIT_S = 10                                       # how long to wait for a fresh daemon's socket


class McpError(Exception):
    pass


class McpClient:
    """MCP over stdio to a child process: newline-delimited JSON-RPC 2.0. Calls are serialized; a reader
    thread routes each response to the queue of the request that is waiting for it. If the process has
    exited, the next call starts it again (once). A call that dies mid-flight is reported, never replayed:
    a click that may have happened must not happen twice."""

    def __init__(self, argv, allowed=AGENT_CUA_TOOLS, prepare=None, touch=None):
        self.argv, self.allowed, self.prepare, self.touch = argv, allowed, prepare, touch
        self.proc = None
        self.pending: dict = {}
        self.next_id = 0
        self.eof = True
        self.lock = threading.RLock()

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None and not self.eof

    def start(self):
        with self.lock:
            self._start()

    def _start(self):
        self._kill()
        if self.prepare:
            self.prepare()
        self.pending, self.eof = {}, False
        try:
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.eof = True
            raise McpError(f"Couldn't start {Path(self.argv[0]).name}: {e}")
        threading.Thread(target=self._read, args=(self.proc, self.pending), daemon=True).start()
        try:
            self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "local-studio", "version": APP_VERSION}}, 15)
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except McpError:
            self._kill()
            raise

    def _read(self, proc, pending):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            q = pending.get(msg.get("id")) if isinstance(msg, dict) and "method" not in msg else None
            if q is not None:
                q.put(msg)
        if proc is self.proc:
            self.eof = True
        for q in list(pending.values()):                      # wake every waiter: the process is gone
            q.put(None)

    def _write(self, msg):
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            raise McpError("The computer-control helper stopped responding.")

    def _request(self, method, params, timeout):
        self.next_id += 1
        rid, q = self.next_id, queue.Queue()
        self.pending[rid] = q
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            try:
                msg = q.get(timeout=timeout)
            except queue.Empty:
                raise McpError(f"{method} timed out after {timeout:g}s")
            if msg is None:
                raise McpError("The computer-control helper stopped responding.")
            if "error" in msg:
                raise McpError(str(msg["error"].get("message") or msg["error"]))
            return msg.get("result") or {}
        finally:
            self.pending.pop(rid, None)

    def call(self, name, args, timeout=30):
        if name not in self.allowed:
            raise McpError(f"{name} is not allowed")
        with self.lock:
            if self.touch:
                self.touch()
            if not self.alive:
                self._start()
            res = self._request("tools/call", {"name": name, "arguments": args or {}}, timeout)
            if self.touch:
                self.touch()
        parts = res.get("content") or []
        text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
        data = res.get("structuredContent")
        if data is None:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        image = next((base64.b64decode(p["data"]) for p in parts if p.get("type") == "image" and p.get("data")), None)
        return {"ok": not res.get("isError"), "text": text, "data": data if isinstance(data, dict) else None, "image": image}

    def _kill(self):
        proc, self.proc, self.eof = self.proc, None, True
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
            try:
                proc.stdout.close()
            except OSError:
                pass

    def close(self):
        with self.lock:
            self._kill()


def cua_status():
    """What the Settings screen shows about the CuaDriver install."""
    out = {"installed": False, "version": None, "ok_version": False, "telemetry": None}
    if not Path(CUA_DRIVER).exists():
        return out
    out["installed"] = True
    try:
        head = subprocess.run([CUA_DRIVER, "--help"], capture_output=True, text=True, timeout=5).stdout
        m = re.match(r"\s*cua-driver (\d+)\.(\d+)\.(\d+)", head)
        if m:
            out["version"] = ".".join(m.groups())
            out["ok_version"] = tuple(int(x) for x in m.groups()) >= CUA_MIN
        t = subprocess.run([CUA_DRIVER, "telemetry", "status"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"Telemetry:\s*(enabled|disabled)", t)
        out["telemetry"] = m.group(1) == "enabled" if m else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


def agent_daemon_pattern():
    """Matches our CuaDriver daemon (its own socket) and not another app's, such as one on the default socket.
    No `^` anchor: the Mac app's data folder contains spaces."""
    return "cua-driver serve --socket " + re.escape(str(AGENT_SOCK))


def _agent_daemon_running():
    return subprocess.run(["pgrep", "-f", agent_daemon_pattern()], capture_output=True).returncode == 0


def start_agent_daemon():
    """Start CuaDriver's daemon through LaunchServices, so it runs with CuaDriver's own macOS permissions
    (not those of this app), on a socket of our own."""
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    if not _agent_daemon_running():                          # one that is still starting just needs its socket awaited
        AGENT_SOCK.unlink(missing_ok=True)                   # a stale one from a daemon that died
        if subprocess.run(["open", "-n", "-g", "-a", "CuaDriver", "--args", "serve", "--socket", str(AGENT_SOCK),
                           "--permission-mode", "standard"], capture_output=True).returncode != 0:
            raise McpError("Couldn't start CuaDriver. Is it installed in Applications?")
    deadline = time.time() + AGENT_SOCK_WAIT_S
    while time.time() < deadline:
        if AGENT_SOCK.exists():
            return
        time.sleep(0.1)
    raise McpError("CuaDriver started but didn't open its socket.")


def stop_agent_daemon():
    """Stop our daemon (and the isolated browser it owns, which exits with it)."""
    global _agent_cli
    with _agent_cli_lock:
        if _agent_cli is not None:
            _agent_cli.close()
            _agent_cli = None
    subprocess.run(["pkill", "-f", agent_daemon_pattern()])
    for _ in range(40):
        if not _agent_daemon_running():
            break
        time.sleep(0.1)
    AGENT_SOCK.unlink(missing_ok=True)


def _ensure_agent_daemon():
    if not (_agent_daemon_running() and AGENT_SOCK.exists()):
        start_agent_daemon()


_agent_cli = None
_agent_cli_lock = threading.Lock()
_agent_last_use = None                                       # time of the last call, None while the daemon is not in use
_agent_reaper = None


def agent_touch():
    global _agent_last_use
    _agent_last_use = time.time()


def agent_client():
    """The one MCP client for our daemon. The daemon is started if needed, and again before any restart
    of the client, so a daemon the idle reaper stopped comes back on the next use."""
    global _agent_cli
    with _agent_cli_lock:
        _ensure_agent_daemon()
        if _agent_cli is None:
            _agent_cli = McpClient([CUA_DRIVER, "mcp", "--socket", str(AGENT_SOCK)], prepare=_ensure_agent_daemon, touch=agent_touch)
        agent_touch()
        _agent_reaper_start()
        return _agent_cli


def agent_reap(now=None):
    """Stop the daemon once it has sat unused for AGENT_IDLE_S and no agent run is active. True if it stopped."""
    global _agent_last_use
    if _agent_last_use is None or agent_lock.locked():
        return False
    if (time.time() if now is None else now) - _agent_last_use < AGENT_IDLE_S:
        return False
    _agent_last_use = None
    stop_agent_daemon()
    return True


def _agent_reaper_start():
    global _agent_reaper
    if _agent_reaper is not None:
        return

    def loop():
        while True:
            time.sleep(60)
            try:
                agent_reap()
            except Exception:
                pass
    _agent_reaper = threading.Thread(target=loop, daemon=True)
    _agent_reaper.start()


# -- views: CuaDriver page/app snapshots -> compact text with short refs (e1..eN) for a 32k-context model

def est_tokens(s):
    return int(len(s) / 3.2)                                 # the same heuristic /api/chat uses


def _clip(s, n):
    s = " ".join(str(s or "").split())                       # one line, no stray control whitespace
    return s if len(s) <= n else s[:n - 1] + "…"


_OUTLINE_LINE = re.compile(r'^(\s*)- ([\w-]+)(?: "(.*?)"(?= \[|\s*$))?')
_VIEW_TEXT_ROLES = frozenset(("statictext", "text", "heading"))
_VIEW_PLAIN_ROLES = frozenset((                              # page structure that is neither a control nor text worth a line
    "generic image figure paragraph none rootwebarea group list listitem labeltext legend form main banner navigation "
    "contentinfo search region article section table row cell rowgroup columnheader presentation separator").split())
_VIEW_LANDMARKS = frozenset(("main", "banner", "navigation", "contentinfo", "search", "region"))
_VIEW_FLAGS = ("required", "focused", "selected", "expanded", "pressed", "disabled", "invalid")
# A button counts as a submit when the DOM says type=submit, or (no DOM info) its label starts like one.
# (and a button inside a <form> with no DOM type is one too: HTML's default button type is submit)
_SUBMIT_RE = re.compile(r"^(submit|send|place order|sign in|log in|continue|next|pay|buy|purchase|sign up|register|create account)\b", re.I)
# Name heuristics for a textbox the DOM view did not describe: a field so named is treated as sensitive.
_PASSWORD_RE = re.compile(r"pass(word|code|wort|wd)|kennwort|mot de passe|contrase[ñn]a|senha|wachtwoord|パスワード|密码|密碼|비밀번호|\bpin\b", re.I)
_OTP_RE = re.compile(r"\botp\b|one[- ]?time|verification code|authenticat\w* code|auth code|sms code|2fa|two[- ]factor", re.I)
_CARD_RE = re.compile(r"card number|credit card|debit card|card holder|cardholder|\bcvv\b|\bcvc\b|security code|expir(y|ation)", re.I)
_SENSITIVE_AC = re.compile(r"^(current-password|new-password|one-time-code|cc-.*)$", re.I)
_DOM_KEY = re.compile(r"(?:^|\s)(\w[\w-]*)=")
_AX_INTERACTIVE = frozenset((
    "Button TextField TextArea SearchField SecureTextField CheckBox RadioButton PopUpButton MenuButton MenuItem MenuBarItem Link "
    "Row Cell Slider ComboBox Tab DisclosureTriangle Switch Stepper").split())
_AX_ACTIONS = frozenset(("AXPress", "AXConfirm", "AXPick"))


def _sensitive_ac(v):
    """True when any whitespace-separated autocomplete token is a password, one-time-code or card one
    (`billing cc-number`, `section-x cc-number`, `current-password webauthn`)."""
    return any(_SENSITIVE_AC.match(t) for t in str(v or "").split())


def _dom_attrs(label):
    """`name=password id=password type=password value=x y` -> dict. `value=` is the last attribute and takes the rest of the
    label (it may hold spaces and fake `key=`s); of every other key the first occurrence counts. A page controls
    these strings, so a sensitive type/autocomplete anywhere in the label wins over a later or earlier decoy."""
    label = label or ""
    v = re.search(r"(?:^|\s)value=", label)
    head = label[:v.start()] if v else label
    out, ms = {}, list(_DOM_KEY.finditer(head))
    for i, m in enumerate(ms):
        out.setdefault(m.group(1).lower(), head[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(head)].strip())
    if v:
        out["value"] = label[v.end():].strip()
    if re.search(r"(?:^|\s)type=password\b", label, re.I):
        out["type"] = "password"
    sens = [m.strip() for m in re.findall(r"(?:^|\s)autocomplete=(.*?)(?=\s\w[\w-]*=|$)", label, re.I | re.S) if _sensitive_ac(m)]
    if sens:                                                 # the value is a token list: `billing cc-number`
        out["autocomplete"] = sens[0].lower()
    return out


def _dom_class(node, attrs):
    t = attrs.get("type")
    if node == "button" or t in ("submit", "button", "reset", "image"):
        return "button"
    return t if t in ("checkbox", "radio") else "text"


def _sem_class(role):
    if role in ("textbox", "searchbox", "spinbutton", "combobox") or role.startswith("input"):
        return "text"
    return role if role in ("checkbox", "radio", "button") else None


def _dom_join(sem, dom):
    """Pair each interactive semantic ref with the DOM element it came from. By name first (case-insensitive, against
    aria-label/name/id/value/placeholder); then by order within a role class, but only when the leftovers on both
    sides are equally many (honeypot inputs and icon buttons would otherwise shift every pairing). {index: attrs}"""
    cands = []
    for r in (dom or {}).get("refs") or []:
        if r.get("node") in ("input", "button", "textarea"):
            a = _dom_attrs(r.get("label"))
            if a.get("type") != "hidden":
                cands.append((_dom_class(r["node"], a), a))
    used, got = set(), {}
    for i, s in enumerate(sem):
        c, nm = _sem_class(s["role"]), (s.get("name") or "").strip().lower()
        if c and nm:
            for j, (k, a) in enumerate(cands):
                if j not in used and k == c and nm in (a.get(f, "").strip().lower() for f in ("aria-label", "name", "id", "value", "placeholder")):
                    used.add(j); got[i] = a
                    break
    for c in ("text", "button", "checkbox", "radio"):
        left_s = [i for i, s in enumerate(sem) if i not in got and _sem_class(s["role"]) == c]
        left_d = [j for j, (k, _) in enumerate(cands) if j not in used and k == c]
        if left_s and len(left_s) == len(left_d):
            for i, j in zip(left_s, left_d):
                got[i] = cands[j][1]
    return got


def _on(v):
    return v not in (None, False, 0, "", "false", "0", "no")


def _dom_sensitive(dom):
    """True when the DOM view lists a visible password, one-time-code or card input."""
    for r in (dom or {}).get("refs") or []:
        if r.get("node") == "input":
            a = _dom_attrs(r.get("label"))
            if a.get("type") != "hidden" and (a.get("type") == "password" or _sensitive_ac(a.get("autocomplete"))):
                return True
    return False


def _guess_autocomplete(name):
    if _OTP_RE.search(name):
        return "one-time-code"
    m = _CARD_RE.search(name)
    return None if not m else "cc-csc" if re.search(r"cvv|cvc|security", m.group(0), re.I) else "cc-exp" if m.group(0).lower().startswith("expir") else "cc-number"


def _facts(role, name, input_type=None, autocomplete=None, in_form=False, is_submit=False, href=None, box=None, sensitive_unknown=False):
    return {"role": role, "name": name, "input_type": input_type, "autocomplete": autocomplete,
            "in_form": in_form, "is_submit": is_submit, "href": href, "box": box,
            "sensitive_unknown": sensitive_unknown}              # editable, not matched to the DOM, on a page whose DOM holds a sensitive input


def _assemble(header, items, budget):
    """items (document order): {kind: "i"|"t", vis, line: fn(id) -> str, ref, facts}. Keeps interactive-and-visible, then
    visible text, then off-screen interactive, then off-screen text, until the budget is spent; ids e1..eN are then handed
    out in document order to the interactive items that made it."""
    total = sum(1 for it in items if it["kind"] == "i")
    idw = len(str(max(total, 1)))
    room = int(budget * 3.2) - len("\n".join(header)) - 1 - 32           # 32 = the "(+N more not shown)" footer
    chosen = set()
    for tier in ((True, "i"), (True, "t"), (False, "i"), (False, "t")):
        for n, it in enumerate(items):
            if (it["vis"], it["kind"]) != tier:
                continue
            cost = len(it["line"]("e" + "9" * idw)) + 1
            if cost > room:
                break
            room -= cost
            chosen.add(n)
        else:
            continue
        break
    lines, refs = list(header), {}
    for n, it in enumerate(items):
        if n in chosen:
            eid = None
            if it["kind"] == "i":
                eid = f"e{len(refs) + 1}"
                refs[eid] = {"ref": it["ref"], "facts": it["facts"]}
            lines.append(it["line"](eid))
    if len(chosen) < len(items):
        lines.append(f"(+{len(items) - len(chosen)} more not shown)")
    body = sum(len(it["line"]("e1")) for it in items)
    return {"text": "\n".join(lines), "refs": refs, "interactive": len(refs),
            "sparse": total < 5 or body < 200}                             # judged on the whole snapshot, not on what fit


def _control_line(role, name, extras=""):
    return lambda eid: f"[{eid}] {role}" + (f' "{name}"' if name else "") + extras


def browser_view(state, dom=None, budget=6000):
    refs = state.get("refs") or []
    sem = [r for r in refs if r.get("role") not in _VIEW_TEXT_ROLES and r.get("role") not in _VIEW_PLAIN_ROLES]
    joined = _dom_join(sem, dom)
    dom_of = {id(s): joined[i] for i, s in enumerate(sem) if i in joined}
    dom_sens = _dom_sensitive(dom)
    sem_ids = {id(r) for r in sem}
    # walk the outline for document order, text and <form> scope; refs are matched to outline lines in order
    seq, ptr, forms, vis, form_at = [], 0, [], "in_viewport", None
    names = {" ".join((r.get("name") or "").split()) for r in sem}
    for raw in (state.get("outline") or "").splitlines():
        m = _OUTLINE_LINE.match(raw)
        if not m:
            continue
        ind, role, name = len(m.group(1)), m.group(2), " ".join((m.group(3) or "").split())
        # in_form is lenient: the outline's indentation drifts, so a form runs until a shallower line, a landmark or the next form
        if forms and ind < forms[-1]:
            forms = []
        if role == "form":
            forms = [ind]                                    # forms do not nest; a new one ends the last
        elif role in _VIEW_LANDMARKS:
            forms = []
        if role in _VIEW_TEXT_ROLES or role not in _VIEW_PLAIN_ROLES:
            hit = next((j for j in range(ptr, min(ptr + 60, len(refs)))
                        if refs[j].get("role") == role and " ".join((refs[j].get("name") or "").split()) == name), None)
            if hit is not None:
                for r in refs[ptr:hit]:
                    if id(r) in sem_ids:
                        seq.append(("i", r, bool(forms)))
                ptr = hit + 1
            if role in _VIEW_TEXT_ROLES:
                if name and name not in names and (not seq or seq[-1][:2] != ("t", name)):
                    seq.append(("t", name, role, vis))
            elif hit is not None:
                seq.append(("i", refs[hit], bool(forms)))
                vis = refs[hit].get("visibility", vis)
    for r in refs[ptr:]:
        if id(r) in sem_ids:
            seq.append(("i", r, False))
    items = []
    for e in seq:
        if e[0] == "t":
            items.append({"kind": "t", "vis": e[3] == "in_viewport", "line": (lambda t, n: lambda eid: f'{t} "{_clip(n, 160)}"')("heading" if e[2] == "heading" else "text", e[1])})
            continue
        r, in_form = e[1], e[2]
        a = dom_of.get(id(r)) or {}
        role, name = r["role"], _clip(r.get("name"), 100)
        editable = _sem_class(role) == "text" or "type" in (r.get("actions") or ()) and role not in ("button", "link", "checkbox", "radio", "menuitem", "tab", "switch")
        itype = a.get("type")
        if editable and itype != "password" and _PASSWORD_RE.search(name):      # a "show password" toggle turns type=password into text
            itype = "password"
        ac = a.get("autocomplete")
        if editable and not _sensitive_ac(ac):               # the label is the fallback unless the DOM already says it is sensitive
            ac = _guess_autocomplete(name) or ac
        unknown = bool(editable and dom_sens and id(r) not in dom_of)
        submit = a.get("type") == "submit" or (role == "button" and a.get("type") is None and (in_form or bool(_SUBMIT_RE.match(name))))
        st = r.get("states") or {}
        flags = [f for f in _VIEW_FLAGS if _on(st.get(f))] + (["checked" if _on(st["checked"]) else "unchecked"] if "checked" in st else [])
        extra = (f" type={itype}" if itype and itype not in ("text", "submit", "button") else "") + (f" [{' '.join(flags)}]" if flags else "")
        if r.get("value") not in (None, "") and itype != "password" and not unknown and not _sensitive_ac(ac):
            extra += f' = "{_clip(r["value"], 80)}"'
        items.append({"kind": "i", "vis": r.get("visibility") == "in_viewport", "ref": r["ref"], "line": _control_line(role, name, extra),
                      "facts": _facts(role, r.get("name"), itype, ac, in_form, submit, sensitive_unknown=unknown)})
    page = state.get("page") or {}
    title = page.get("title")
    if not title or title == "about:blank":                  # the root web area carries the real title
        title = next((c.get("name") for c in state.get("content_refs") or [] if c.get("role") == "rootwebarea" and c.get("name")), title)
    header = [f"Page: {_clip(title, 120)}", f"URL: {_clip(page.get('url'), 200)}"]
    if (state.get("snapshot") or {}).get("complete") is False:
        header.append("(the page continues below the part listed here)")
    return _assemble(header, items, budget)


def app_view(state, budget=6000):
    els = state.get("elements") or []
    wb = state.get("window_bounds") or {}
    sw, sh = state.get("screenshot_width"), state.get("screenshot_height")
    sx = sw / wb["width"] if sw and wb.get("width") else state.get("screenshot_scale") or 1.0
    sy = sh / wb["height"] if sh and wb.get("height") else state.get("screenshot_scale") or 1.0
    kids = {}
    for e in els:
        kids.setdefault(e.get("parent_index"), []).append(e)

    def inner(e, depth=3):                                   # the first text under a row or cell, which is what names it
        for c in kids.get(e["element_index"], ()):
            t = c.get("label") or c.get("value") if c.get("role") in ("AXStaticText", "AXTextField", "AXTextArea") else None
            if t or depth > 1 and (t := inner(c, depth - 1)):
                return t
        return None
    items = []
    for e in els:
        role = e.get("role") or ""
        short = role[2:] if role.startswith("AX") else role
        label, value, f = e.get("label"), e.get("value"), e.get("frame")
        if short in ("Row", "Cell") and not (label or value):
            label = inner(e)
        if short == "StaticText":
            t = value or label
            if t:
                items.append({"kind": "t", "vis": True, "line": (lambda n: lambda eid: f'text "{_clip(n, 160)}"')(t)})
            continue
        if not (short in _AX_INTERACTIVE or _AX_ACTIONS & set(e.get("actions") or ())):
            continue
        if not (label or value or f) or short in ("Row", "Cell") and not (label or value):
            continue                                         # nothing to call it by (and, for controls, nowhere to see it)
        box = [round((f["x"] - wb.get("x", 0)) * sx), round((f["y"] - wb.get("y", 0)) * sy), round(f["w"] * sx), round(f["h"] * sy)] if f else None
        vis = bool(box and sw and sh and box[0] < sw and box[1] < sh and box[0] + box[2] > 0 and box[1] + box[3] > 0)
        name = _clip(label or (value if short not in ("TextField", "TextArea", "SearchField", "SecureTextField", "ComboBox") else ""), 100)
        flags = [k for k in ("selected",) if e.get(k)] + (["disabled"] if e.get("enabled") is False else [])
        extra = (f" [{' '.join(flags)}]" if flags else "") + (f' = "{_clip(value, 80)}"' if value and label and short != "SecureTextField" else "")
        kind = short.lower()
        secure = short == "SecureTextField"
        if secure:
            kind = "textfield"
        items.append({"kind": "i", "vis": vis, "line": _control_line(kind, name, extra),
                      "ref": {"element_index": e["element_index"], "window_id": state.get("window_id"), "pid": state.get("pid")},
                      "facts": _facts(kind, label or value, "password" if secure else None, None, False,
                                      kind == "button" and bool(_SUBMIT_RE.match(label or "")), None, box)})
    header = [f"App: {_clip(state.get('app_name'), 80)}" + (f' - window "{_clip(state.get("window_title"), 100)}"' if state.get("window_title") else "")]
    return _assemble(header, items, budget)


def fold_history(lines, budget=3000):
    """Newest steps stay whole (each capped); the oldest become one `Earlier: …` line. Fits est_tokens(...) <= budget."""
    cap = int(budget * 3.2)
    if est_tokens("\n".join(lines)) <= budget:
        return list(lines)
    head = max(40, cap // 5)
    room = cap - head - 1
    keep, used = [], 0
    for ln in reversed(lines):
        ln = _clip(ln, min(400, room))
        if keep and used + len(ln) + 1 > room:
            break
        keep.append(ln); used += len(ln) + 1
    older = lines[:len(lines) - len(keep)]
    out = list(reversed(keep))
    if older:
        out.insert(0, _clip("Earlier: " + "; ".join(_clip(l, 50) for l in older), head))
    return out


# ---- approval gate: decides from facts about the target (never the model's own words) whether an action is safe, risky or blocked

# Words on a control (or in a menu path) that make acting on it irreversible or public. Matched after _norm_label, with
# the inflections people see on buttons ("Sends", "Deleting...", "Payment"); \s* / \s+ inside the multi-word ones.
RISKY_WORDS = re.compile(
    r"\b(?:send(?:s|ing)?|sent|submit(?:s|ted|ting)?|buy(?:s|ing)?|pay(?:s|ing|ment|ments|ed)?|order(?:s|ed|ing)?|"
    r"check[\s-]*out|confirm(?:s|ed|ing|ation)?|delet(?:e|es|ed|ing|ion)|remov(?:e|es|ed|ing|al)|post(?:s|ed|ing)?|"
    r"publish(?:es|ed|ing)?|sign[\s-]*up|(?:un)?subscri(?:be|bes|bed|bing|ption|ptions)|book(?:s|ed|ing)?|transfer(?:s|red|ring)?|"
    r"place[\s-]*order|purchas(?:e|es|ed|ing)|donat(?:e|es|ed|ing|ion|ions)|trash(?:ed|ing)?|repl(?:y|ies|ied|ying)|"
    r"shar(?:e|es|ed|ing)|authori[sz](?:e|es|ed|ing|ation)|approv(?:e|es|ed|ing|al)|accept(?:s|ed|ing)?|grant(?:s|ed|ing)?)\b", re.I)
APP_TIERS_DEFAULT = {
    **{n: "read" for n in (
        "Keychain Access", "System Settings", "Terminal", "iTerm", "Warp", "1Password", "Bitwarden", "Passwords",
        "Chase", "PayPal", "Venmo", "Coinbase", "Robinhood", "Wise", "Revolut", "Schwab", "Fidelity", "Vanguard",
        "E*TRADE", "Binance", "Kraken", "Quicken", "YNAB",
        "LastPass", "Dashlane", "Keeper", "Enpass", "KeePassXC", "Proton Pass",
        "Ghostty", "kitty", "Alacritty", "WezTerm", "Hyper", "Tabby",
        "Shortcuts", "Script Editor", "Automator")},
    **{n: "ask" for n in (
        "Mail", "Messages", "WhatsApp", "Slack", "Calendar", "Finder",
        "Telegram", "Discord", "Signal", "Microsoft Teams", "Skype", "LINE", "WeChat", "Zoom", "zoom.us", "Outlook", "Microsoft Outlook")},
}
READ_ONLY_NAME = re.compile(r"bank|banking|finance|wallet|password|crypto|invest|broker|trading|pay\b", re.I)
SEARCH_DOMAINS = frozenset(("duckduckgo.com", "google.com", "bing.com"))     # open_url may go to a search on exactly these hosts (_search_ok)
_TIER_NAMES = ("read", "ask", "allowed")
_SEARCH_FIELD_RE = re.compile(r"^\s*(?:search|find)\b|\bsearch\b", re.I)
_SEARCH_ROLES = frozenset(("searchbox", "searchfield"))
_CTRL_RE = re.compile("[\\x00-\\x08\\x0a-\\x1f\\x7f\\u2028\\u2029\\x85]")      # C0 controls but tab (Enter, NSEnter \\x03, Backspace, ...) and line separators
_ENTER_KEYS = frozenset(("↩", "⏎", "⌅", "↵", "space", "spacebar", "␣", "kpenter", "newline", "linefeed", "lf", "cr"))
_DELETE_KEYS = frozenset(("del", "⌫", "⌦", "kpdelete"))
_APP_ACTING = frozenset(("click_app", "type_app", "menu", "hotkey"))
_TYPING = frozenset(("type", "type_app"))
_SAFE_TOOLS = frozenset(("read_page", "scroll", "back", "note", "ask_user", "done", "list_apps", "open_app", "read_app"))
_MD_SPECIAL = re.compile(r"([\\*_\[\]()!`#<>|&~])|:(?=//)")


def _norm_label(s):
    """What a person would read: NFKC, no zero-width/bidi (Cf) or control characters, whitespace collapsed."""
    s = unicodedata.normalize("NFKC", str(s if s is not None else ""))
    s = "".join(" " if unicodedata.category(c) == "Cc" else "" if unicodedata.category(c) == "Cf" else c for c in s)
    return " ".join(s.split())


def _md_escape(s):
    return _MD_SPECIAL.sub(lambda m: "\\" + m.group(0), s)


def _tier_key(name):
    k = (name or "").strip().casefold()
    return k[:-4] if k.endswith(".app") else k


def _default_tier(k):
    """Longest default whose name equals k or is a whole-word prefix of it ("Microsoft Teams (work or school)", "Telegram Desktop")."""
    best = None
    for n, t in APP_TIERS_DEFAULT.items():
        d = _tier_key(n)
        if (k == d or k.startswith(d) and not k[len(d)].isalnum()) and (best is None or len(d) > best[0]):
            best = (len(d), t)
    return best[1] if best else None


def app_tier(app_name, overrides):
    """"read" (look only), "ask" (every action needs approval) or "allowed": the user's overrides (exact name) first, then the
    defaults (name or whole-word prefix), then a name that sounds like money or secrets (read), else allowed."""
    k = _tier_key(app_name)
    for n, t in (overrides or {}).items():
        if _tier_key(n) == k and t in _TIER_NAMES:
            return t
    t = _default_tier(k) or (_default_tier(k.rstrip("0123456789 ")) if k.rstrip("0123456789 ") != k else None)   # iTerm2 -> iterm
    return t or ("read" if READ_ONLY_NAME.search(_norm_label(app_name)) else "allowed")


def _norm_url(u):
    """The WHATWG way: leading/trailing C0 controls and spaces go, tab/CR/LF vanish everywhere."""
    return re.sub(r"[\t\r\n]", "", str(u if u is not None else "")).strip("".join(map(chr, range(0x21))))


def _url_scheme(u):
    """Any `name:` prefix of a normalised URL is a scheme (so `javascript:1/x` and `data:1/x` are); http/https are the only ones
    let through, so a schemeless `host:port` fails safe."""
    m = re.match(r"^([a-z][a-z0-9+.-]*):", u, re.I)
    return m.group(1).lower() if m else ""


def _split_url(s):
    return urlsplit(s if re.match(r"^(?:[a-z][a-z0-9+.-]*:)?//", s, re.I) else "//" + s)


def _host(s):
    """Lowercase ASCII (IDNA) host of a URL or bare host, without www., port or trailing dot; "" if there is none.
    A host with ß, ς, ZWNJ or ZWJ is returned as typed: Python's IDNA2003 would map ß to ss where browsers (UTS 46) keep it, so
    it must not be compared as its ASCII look-alike."""
    s = _norm_url(s)
    if not s or "\\" in s:                                         # browsers read \ as /, urlsplit does not: https://evil.net\@example.com
        return ""
    try:
        h = (_split_url(s).hostname or "").lower().rstrip(".")
        if not any(c in h for c in "ßς\u200c\u200d"):
            h = h.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return ""
    return h[4:] if h.startswith("www.") else h


def _search_ok(url, host):
    """A plain web search on exactly google.com, bing.com or duckduckgo.com (www. allowed); not their subdomains, redirectors
    (/url, /l/, /ck/a) or form/script hosts, which can carry data out."""
    if host not in SEARCH_DOMAINS:
        return False
    try:
        parts = _split_url(url)
    except ValueError:
        return False
    if host == "duckduckgo.com":
        return (parts.path in ("", "/") and bool(parse_qs(parts.query).get("q"))) or parts.path == "/html" or parts.path.startswith("/html/")
    return parts.path == "/search"


def _domain_in(host, domains):
    for d in domains or ():
        d = _host(d)
        if "." in d and (host == d or host.endswith("." + d)):          # a bare label ("com", "localhost") never vouches for anything
            return True
    return False


def _sensitive_field(facts):
    return (facts.get("input_type") or "").strip().lower() == "password" or _sensitive_ac(facts.get("autocomplete"))


def _key_class(keys):
    """"plain", "enter" (return/enter/space, any spelling), "delete" or "invalid" for a hotkey's `keys`: a list of strings
    (a lone string counts as one element), each element possibly a combo like `cmd+return` or `cmd-return`."""
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, (list, tuple)) or not keys or not all(isinstance(k, str) for k in keys):
        return "invalid"
    toks, cls = [], "plain"
    for k in keys:
        if _CTRL_RE.search(k):
            return "enter"
        if any(part and not part.strip(" ") for part in unicodedata.normalize("NFKC", k).split("+")):
            return "enter"                                          # a literal space key, `shift+ `
        toks += [t for t in re.split(r"[+\s]+|(?<=\w)-(?=\w)", k) if t]
    if not toks:
        return "invalid"
    for t in toks:
        t = unicodedata.normalize("NFKC", t).lower().replace("_", "").replace("-", "")
        if t in _ENTER_KEYS or "enter" in t or "return" in t:
            return "enter"
        if t in _DELETE_KEYS or "delete" in t or "backspace" in t:
            cls = "delete"
    return cls


def _menu_path(path):
    """A menu path as a list of item names: a list of strings, or one string like `File > Delete Note`. None if it is neither."""
    if isinstance(path, str):
        path = [path]
    if not isinstance(path, (list, tuple)) or not path or not all(isinstance(p, str) for p in path):
        return None
    out = [_norm_label(x) for p in path for x in re.split(r"[>›]", p)]
    return [x for x in out if x] or None


def gate(tool, args, facts, ctx):
    """Decide `("safe" | "risky" | "blocked", reason)` for one action. Pure; reads only its arguments.

    tool   model-facing tool name (see _SAFE_TOOLS, _TYPING, _APP_ACTING, click, open_url), plus the adapter's own
           `download` (a download about to happen) and `dialog` (answering a page confirm/beforeunload dialog).
    args   the tool's arguments (`url` for open_url, `text`/`submit` for type, `keys` for hotkey, `app`/`path` for menu).
    facts  Facts of the target ref (None when there is none); a click/type without facts is risky.
    ctx    surface         "browser" | "app"; every acting tool is judged by the app's tier when it is "app"
           app, tier       the app being acted on and its tier ("read" | "ask" | "allowed"); a missing tier counts as "ask"
           domain          host of the current page (open_url to it is safe)
           task_domains    set of hosts the task is about
           link_domains    set of hosts of links seen on the page (from the DOM; facts["href"] is always None for the browser)
           url_from_page   not used: the model writes every URL, so where it came from cannot be known
    open_url is safe only within task_domains | link_domains | {domain} (a subdomain counts as its parent; a domain with no dot
    never counts) or for a plain search on SEARCH_DOMAINS (_search_ok); anything else is risky, any scheme but http(s) is blocked. Typing that presses Enter (submit=True, or a newline in
    the text) is risky unless the target is a search field."""
    args = args if isinstance(args, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    facts = facts if isinstance(facts, dict) else None
    if tool in _SAFE_TOOLS:
        return "safe", "reading, scrolling and notes do not change anything"
    if tool == "download":
        return "risky", "a file is about to be downloaded"
    if tool == "dialog":
        return "risky", "the page is asking a question in a dialog"
    if tool == "open_url":
        url = _norm_url(args.get("url"))
        sch = _url_scheme(url)
        if sch and sch not in ("http", "https"):
            return "blocked", f"only http and https addresses can be opened, not {sch}:"
        host = _host(url)                                           # no scheme counts as https://<host>
        if not host:
            return "risky", "the address has no usable host"
        if (_domain_in(host, ctx.get("task_domains")) or _domain_in(host, ctx.get("link_domains"))
                or _domain_in(host, [ctx.get("domain")]) or _search_ok(url, host)):
            return "safe", f"{host} is part of the task, the page or a search engine"
        return "risky", f"{host} is not part of the task"
    if tool in _TYPING or tool == "click" or tool in _APP_ACTING:
        return _gate_acting(tool, args, facts, ctx)
    return "blocked", f"unknown tool {tool!r}"


def _gate_acting(tool, args, facts, ctx):
    in_app = ctx.get("surface") == "app" or tool in _APP_ACTING
    tier = (ctx.get("tier") if ctx.get("tier") in _TIER_NAMES else "ask") if in_app else None
    app = ctx.get("app")
    if tool in _TYPING and facts and _sensitive_field(facts):
        return "blocked", "passwords, one-time codes and card details are never typed by the agent"
    if tier == "read":
        return "blocked", f"{app or 'this app'} is read-only for the agent"
    if tier == "ask":
        if ctx.get("surface") != "app" and not ctx.get("tier"):
            return "risky", "keyboard shortcuts and app actions need approval when no app is chosen"
        return "risky", f"every action in {app or 'this app'} needs approval"
    if tool == "menu":
        if args.get("app") is not None and _tier_key(_norm_label(args.get("app"))) != _tier_key(_norm_label(app)):
            return "risky", f"the menu belongs to {_norm_label(args.get('app'))!r}, not {app or 'the chosen app'}"
        path = _menu_path(args.get("path"))
        if not path:
            return "risky", "the menu path is not understood"
        hit = next((p for p in path if RISKY_WORDS.search(p)), None)
        return ("risky", f"the menu item {hit!r} sounds irreversible") if hit else ("safe", "an ordinary menu item")
    if tool == "hotkey":
        k = _key_class(args.get("keys"))
        return {"invalid": ("risky", "the keys are not understood"), "enter": ("risky", "the keys include return, enter or space"),
                "delete": ("risky", "the keys include delete"), "plain": ("safe", "an ordinary shortcut")}[k]
    if not facts:
        return "risky", "the target could not be identified"
    name = _norm_label(facts.get("name"))
    if tool in _TYPING:
        if facts.get("sensitive_unknown"):
            return "risky", "this field may hold a password or code"
        if in_app and RISKY_WORDS.search(name):
            return "risky", f"the field {name!r} sounds irreversible"
        text = args.get("text")
        if args.get("submit") or (isinstance(text, str) and _CTRL_RE.search(text)):
            if (facts.get("role") or "").lower() in _SEARCH_ROLES or _SEARCH_FIELD_RE.search(name):
                return "safe", "a search"
            return "risky", "typing would also press Enter"
        return "safe", "ordinary typing"
    if facts.get("is_submit"):
        return "risky", "this control submits a form"
    if RISKY_WORDS.search(name):
        return "risky", f"{name!r} sounds irreversible"
    return "safe", "an ordinary control"


_KEY_NAMES = {"cmd": "⌘", "command": "⌘", "shift": "⇧", "option": "⌥", "alt": "⌥", "opt": "⌥", "ctrl": "⌃", "control": "⌃"}


def approval_title(tool, facts, ctx, args=None):
    """One line for the approval card, in the user's terms. Labels come from the facts, not from the model; everything a
    page or app controls is cleaned (_norm_label) and markdown-escaped so it cannot rewrite the card."""
    args, ctx = args or {}, ctx or {}
    esc = lambda v, n=80: _md_escape(_clip(_norm_label(v), n))
    where_web = f"on {esc(ctx.get('domain') or 'this page')}"
    where_app = f"in {esc(ctx.get('app') or 'the app')}"
    f = facts or {}
    label = lambda: esc(f.get("name") or f.get("role") or "control")
    if tool == "click":
        return f"Click **{label()}** {where_web}"
    if tool == "click_app":
        return f"Click **{label()}** {where_app}"
    if tool == "type":
        return f"Type into **{label()}** {where_web}"
    if tool == "type_app":
        return f"Type into **{label()}** {where_app}"
    if tool == "download":
        return f"Download **{esc(f.get('name') or args.get('name') or args.get('filename') or 'a file')}** {where_web}"
    if tool == "dialog":
        return f"Answer a page dialog {where_web}"
    if tool == "open_url":
        url = _norm_url(args.get("url"))
        return f"Open **{_md_escape(_host(url)) or 'an address with no host'}** ({esc(url)})"
    if tool == "menu":
        return f"Use menu **{esc(' › '.join(_menu_path(args.get('path')) or ['?']))}** {where_app}"
    if tool == "hotkey":
        ks = args.get("keys")
        parts = [t for k in ([ks] if isinstance(ks, str) else ks if isinstance(ks, (list, tuple)) else ()) if isinstance(k, str)
                 for t in re.split(r"[+\s]+|(?<=\w)-(?=\w)", _norm_label(k)) if t]
        keys = [_KEY_NAMES.get(t.lower(), t.capitalize() if len(t) > 1 else t.upper()) for t in parts]
        return f"Press **{esc('+'.join(keys) or '?')}** {where_app}"
    return f"Run **{esc(tool)}**"


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
    # no-cache: the app window must revalidate, or it keeps showing the previous version's page after an update
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-cache"})
