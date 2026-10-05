"""Local Studio: a local web app for Qwen3.8 chat (mlx-dspark) and Qwen-Image 2.1 (mflux).

Chat is proxied to the mlx-dspark server; image jobs run the mflux CLI one at a time and
record steps, wall time and peak memory. Also manages the engine process, the downloaded
models, local storage and uninstalling.
"""

import json
import os
import re
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

ROOT = Path(__file__).resolve().parent
GALLERY = ROOT / "gallery"
UPLOADS = ROOT / "uploads"
LOGS = ROOT / "logs"
CHATS = ROOT / "data" / "chats"
for d in (GALLERY, UPLOADS, LOGS, CHATS):
    d.mkdir(parents=True, exist_ok=True)

HOME = Path.home()
BIN = HOME / ".local/bin"
HF_HUB = Path(os.environ.get("HF_HUB_CACHE", HOME / ".cache/huggingface/hub"))
HF_CLI = HOME / ".local/share/uv/tools/mlx-dspark/bin/hf"

LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8080")
LLM_MODEL = os.environ.get("LLM_MODEL", "mlx-community/Qwen3.8-27B-4bit")
LLM_DRAFTER = os.environ.get("LLM_DRAFTER", "incoai/Qwen3.8-27B-DFlash2")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "mlx-community/Qwen-Image-2.1-mflux-q4")
# mflux editing needs a complete checkpoint (VAE + transformer + text encoder configs);
# the mflux-q4 text-only export lacks the visual encoder, so edit stays off unless set.
EDIT_MODEL = os.environ.get("EDIT_MODEL")

MODELS = [
    {"repo": LLM_MODEL, "role": "Chat model", "group": "llm", "approx_gb": 15.2},
    {"repo": LLM_DRAFTER, "role": "DFlash 2 drafter", "group": "llm", "approx_gb": 3.6},
    {"repo": IMAGE_MODEL, "role": "Image model", "group": "image", "approx_gb": 8.9},
]

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


@app.get("/api/system")
def api_system():
    return {**system_memory(), "edit_enabled": bool(EDIT_MODEL)}

# ---------------------------------------------------------------- engine (LLM server)


def engine_running():
    return bool(subprocess.run(["pgrep", "-f", "mlx-dspark serve"], capture_output=True).stdout.strip())


@app.post("/api/engine/start")
def engine_start():
    if engine_running():
        return {"status": "already running"}
    missing = [m["repo"] for m in MODELS if m["group"] == "llm" and not repo_dir(m["repo"]).exists()]
    if missing:
        raise HTTPException(409, f"Download first: {', '.join(missing)}")
    log = open(LOGS / "llm.log", "w")
    proc = subprocess.Popen(
        [str(BIN / "mlx-dspark"), "serve", "--model", LLM_MODEL, "--kv-bits", "8",
         "--context-window", "32768", "--host", "127.0.0.1", "--port", "8080"],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (LOGS / "llm.pid").write_text(str(proc.pid))
    return {"status": "starting", "pid": proc.pid}


@app.post("/api/engine/stop")
def engine_stop():
    pid = _read_pid("llm")
    if pid and _pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
    subprocess.run(["pkill", "-f", "mlx-dspark serve"])
    (LOGS / "llm.pid").unlink(missing_ok=True)
    return {"status": "stopped"}

# ---------------------------------------------------------------- LLM proxy


@app.get("/api/llm/{route:path}")
async def llm_get(route: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{LLM_URL}/{route}")
        return JSONResponse(r.json(), status_code=r.status_code)
    except httpx.HTTPError as e:
        return JSONResponse({"status": "starting" if engine_running() else "down", "error": str(e)},
                            status_code=503)


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})

    async def relay():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", f"{LLM_URL}/v1/chat/completions", json=body) as r:
                    if r.status_code != 200:
                        err = (await r.aread()).decode(errors="replace")
                        yield f"data: {json.dumps({'error': err, 'status': r.status_code})}\n\n"
                        return
                    async for chunk in r.aiter_raw():
                        yield chunk
        except httpx.HTTPError as e:
            yield f"data: {json.dumps({'error': f'Engine unreachable: {e}'})}\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream")

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
                       mode: str = Form("generate"), images: list[UploadFile] = File(default=[])):
    if mode == "edit" and not EDIT_MODEL:
        raise HTTPException(400, "Editing needs the full Qwen-Image-2.1 checkpoint; the 4-bit "
                                 "weights installed here are text-to-image only.")
    if mode != "edit" and not repo_dir(IMAGE_MODEL).exists():
        raise HTTPException(409, "Image model not downloaded. Get it under Models.")
    seed_val = int(seed) if seed.strip() else int(time.time()) % 2**31
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fname = f"{stamp}-{seed_val}.png"
    meta = {"prompt": prompt, "mode": mode, "width": width, "height": height, "steps": steps,
            "seed": seed_val, "model": IMAGE_MODEL, "file": fname, "low_ram": True}

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
        cmd = [str(BIN / "mflux-generate-qwen-2.1")]
    # --low-ram evicts the text encoder/transformer between phases and tiles the VAE:
    # peak ~10 GB instead of ~38 GB, ~10% slower — it keeps the LLM server out of swap.
    cmd += ["--low-ram", "--model", EDIT_MODEL if mode == "edit" else IMAGE_MODEL,
            "--prompt", prompt, "--steps", str(steps), "--seed", str(seed_val),
            "--width", str(width), "--height", str(height), "--output", str(GALLERY / fname)]
    if negative_prompt.strip():
        cmd += ["--negative-prompt", negative_prompt]
        meta["negative_prompt"] = negative_prompt
    if guidance.strip():
        cmd += ["--guidance", guidance]
        meta["guidance"] = float(guidance)
    if step_cache.strip() and mode != "edit":
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


@app.get("/api/models")
def models():
    known = {m["repo"] for m in MODELS}
    rows = []
    for m in MODELS:
        d = repo_dir(m["repo"])
        files = _repo_files(d) if d.exists() else set()
        incomplete = d.exists() and any(p.name.endswith(".incomplete") for p in d.rglob("*"))
        dl = downloads.get(m["repo"], {})
        rows.append({**m, "installed": bool(files) and not incomplete, "incomplete": incomplete,
                     "size_bytes": sum(f.stat().st_size for f in files),
                     "downloading": dl.get("status") == "running", "download_error": dl.get("status") == "error"})
    if HF_HUB.exists():
        for d in sorted(HF_HUB.glob("models--*")):
            repo = _repo_for_dir(d.name)
            if repo in known:
                continue
            files = _repo_files(d)
            rows.append({"repo": repo, "role": "Other cached model", "group": "other", "installed": True,
                         "incomplete": False, "downloading": False,
                         "size_bytes": sum(f.stat().st_size for f in files)})
    return rows


@app.delete("/api/models")
def delete_model(repo: str):
    d = repo_dir(repo)
    if not d.exists():
        raise HTTPException(404, "not downloaded")
    group = next((m["group"] for m in MODELS if m["repo"] == repo), "other")
    if group == "llm" and engine_running():
        raise HTTPException(409, "Stop the engine before deleting its model.")
    mine = _repo_files(d)
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
    return {"ok": True, "freed_bytes": freed}


downloads: dict[str, dict] = {}


def _download(repo):
    log = LOGS / f"download-{repo.replace('/', '--')}.log"
    with open(log, "w") as fh:
        rc = subprocess.run([str(HF_CLI), "download", repo], stdout=fh, stderr=subprocess.STDOUT).returncode
    downloads[repo].update(status="done" if rc == 0 else "error", returncode=rc)


@app.post("/api/models/download")
def download_model(repo: str = Body(..., embed=True)):
    if repo not in {m["repo"] for m in MODELS}:
        raise HTTPException(400, "unknown model")
    if downloads.get(repo, {}).get("status") == "running":
        return downloads[repo]
    downloads[repo] = {"repo": repo, "status": "running", "started": time.time()}
    threading.Thread(target=_download, args=(repo,), daemon=True).start()
    return downloads[repo]

# ---------------------------------------------------------------- storage & uninstall


@app.get("/api/storage")
def storage():
    engine_cache = HOME / ".cache/mlx_dspark"
    return {
        "gallery": {"bytes": dir_size(GALLERY), "count": len(list(GALLERY.glob("*.png")))},
        "uploads": {"bytes": dir_size(UPLOADS), "count": sum(1 for p in UPLOADS.iterdir() if p.is_file())},
        "chats": {"bytes": dir_size(CHATS), "count": len(list(CHATS.glob("*.json")))},
        "logs": {"bytes": dir_size(LOGS), "count": len(list(LOGS.glob("*.log")))},
        "engine_cache": {"bytes": dir_size(engine_cache) if engine_cache.exists() else 0},
        "disk_free_bytes": shutil.disk_usage(HOME).free,
        "root": str(ROOT),
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
    args = [str(ROOT / "uninstall.sh"), "--yes"] + (["--remove-app"] if remove_app else [])
    subprocess.Popen(args, stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    return {"ok": True, "log": str(log)}


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")
