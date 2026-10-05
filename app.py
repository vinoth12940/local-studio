"""Local Studio: a local web app for Qwen3.8 chat (mlx-dspark) and Qwen-Image 2.1 (mflux).

Chat is proxied to the mlx-dspark server; image jobs run the mflux CLI one at a time and
record steps, wall time and peak memory. Also manages the engine process, the downloaded
models, local storage and uninstalling.
"""

import asyncio
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
VLM_URL = os.environ.get("VLM_URL", "http://127.0.0.1:8081")
DSPARK_PY = HOME / ".local/share/uv/tools/mlx-dspark/bin/python"   # its env also ships mlx-vlm
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

# ---------------------------------------------------------------- engines
# Two engines share the same Qwen3.8 weights; only one runs at a time (~16 GB each).
#   text   — mlx-dspark with DFlash 2 (~40 tok/s), text only: it drops image inputs.
#   vision — mlx-vlm server (images + text, ~24 tok/s), used for chats that contain images.
ENGINES = {
    "text": {"pattern": r"^[^ ]*python[^ ]* [^ ]*mlx-dspark serve", "pid": "llm", "url": LLM_URL, "ready": "/health"},
    "vision": {"pattern": r"^[^ ]*python[^ ]* -m mlx_vlm server", "pid": "vlm", "url": VLM_URL, "ready": "/v1/models"},
}
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


def _engine_ready(mode):
    e = ENGINES[mode]
    try:
        r = httpx.get(e["url"] + e["ready"], timeout=3)
        return r.status_code == 200 and (mode != "text" or r.json().get("status") == "ok")
    except Exception:
        return False


def _start_engine(mode):
    log = open(LOGS / f"{ENGINES[mode]['pid']}.log", "w")
    if mode == "text":
        cmd = [str(BIN / "mlx-dspark"), "serve", "--model", LLM_MODEL, "--kv-bits", "8",
               "--context-window", "32768", "--host", "127.0.0.1", "--port", "8080"]
    else:
        cmd = [str(DSPARK_PY), "-m", "mlx_vlm", "server", "--model", LLM_MODEL,
               "--draft-model", LLM_DRAFTER, "--draft-kind", "dflash",
               "--host", "127.0.0.1", "--port", "8081"]
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (LOGS / f"{ENGINES[mode]['pid']}.pid").write_text(str(proc.pid))
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


def ensure_engine(mode, timeout=240):
    """Make `mode` the running engine (stopping the other); block until it answers."""
    with engine_lock:
        current = running_mode()
        if current == mode and _engine_ready(mode):
            return False
        for other in ENGINES:
            if other != mode and _proc_running(ENGINES[other]["pattern"]):
                _stop_engine(other)
        if not _proc_running(ENGINES[mode]["pattern"]):
            _start_engine(mode)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _engine_ready(mode):
                return True
            if not _proc_running(ENGINES[mode]["pattern"]):
                raise RuntimeError(f"{mode} engine exited; see logs/{ENGINES[mode]['pid']}.log")
            time.sleep(1)
        raise RuntimeError(f"{mode} engine did not become ready in {timeout}s")


@app.post("/api/engine/start")
def engine_start(mode: str = Body("text", embed=True)):
    if mode not in ENGINES:
        raise HTTPException(400, "unknown engine")
    if running_mode() == mode:
        return {"status": "already running", "mode": mode}
    missing = [m["repo"] for m in MODELS if m["group"] == "llm" and not repo_dir(m["repo"]).exists()]
    if missing:
        raise HTTPException(409, f"Download first: {', '.join(missing)}")
    threading.Thread(target=ensure_engine, args=(mode,), daemon=True).start()
    return {"status": "starting", "mode": mode}


@app.post("/api/engine/stop")
def engine_stop():
    with engine_lock:
        for mode in ENGINES:
            _stop_engine(mode)
    return {"status": "stopped"}

# ---------------------------------------------------------------- LLM proxy


@app.get("/api/llm/{route:path}")
async def llm_get(route: str):
    mode = running_mode()
    if mode == "vision":
        # mlx-vlm has no dspark-style health/metrics; report what the UI needs
        ready = await asyncio.to_thread(_engine_ready, "vision")
        if route == "health":
            return {"status": "ok" if ready else "loading", "mode": "vision", "target": LLM_MODEL,
                    "drafter": LLM_DRAFTER, "max_draft": None, "kv_bits": None,
                    "context_window": 32768, "warnings": []}
        return JSONResponse({"engine": "vision"}, status_code=200 if ready else 503)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{LLM_URL}/{route}")
        return JSONResponse(r.json(), status_code=r.status_code)
    except httpx.HTTPError as e:
        return JSONResponse({"status": "starting" if mode else "down", "error": str(e)}, status_code=503)


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages, needs_vision = build_messages(body.get("messages", []))
    body["messages"] = messages
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})
    mode = "vision" if needs_vision else "text"

    async def relay():
        if running_mode() != mode or not await asyncio.to_thread(_engine_ready, mode):
            yield _sse({"x_engine": {"status": "switching", "mode": mode}})
            try:
                await asyncio.to_thread(ensure_engine, mode)
            except Exception as e:
                yield _sse({"error": f"Could not start the {mode} engine: {e}"})
                return
            yield _sse({"x_engine": {"status": "ready", "mode": mode}})
        url = ENGINES[mode]["url"]
        if mode == "vision":
            body["model"] = LLM_MODEL
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
                    # vision: pass through, and synthesize the per-reply stats block the UI shows
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
        tps = (last_timings or {}).get("predicted_per_second")
        out = (usage or {}).get("completion_tokens") or n_out
        yield _sse({"x_mlx_dspark": {
            "mode": "vision", "decode_tokens_per_sec": round(tps or (out / max(end - (first or end), 1e-6)), 1),
            "tokens_per_sec": round(out / max(end - t0, 1e-6), 1), "completion_tokens": out,
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
            "ttft_seconds": round((first or end) - t0, 3), "decode_seconds": round(end - (first or end), 3)}})
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
            # scanned / image-only PDF: hand the first pages to the vision engine
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
    text; images become image_url parts (which also means the vision engine is needed)."""
    import base64
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
