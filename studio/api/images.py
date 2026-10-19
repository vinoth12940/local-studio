"""Routes: images.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import json
import re
import secrets
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from studio.services import models
from studio.config import AGENT_BUSY_MSG, agent_lock, BIN, EDIT_MODEL, GALLERY, UPLOADS
from studio.services.images import jobs, run_image_job
from studio.services.models import image_model

router = APIRouter()


@router.post("/api/image")
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
    if mode != "edit" and not models.model_installed(im):
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


@router.get("/api/image/{job_id}")
def image_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    view = {k: v for k, v in job.items() if k != "pid"}
    if job.get("started") and job["status"] == "running":
        view["elapsed"] = round(time.time() - job["started"], 1)
    return view


@router.post("/api/image/{job_id}/cancel")
def image_cancel(job_id: str):
    job = jobs.get(job_id)
    if not job or job["status"] not in ("queued", "running"):
        raise HTTPException(404, "no running job")
    job["cancelled"] = True
    if job.get("pid"):
        subprocess.run(["pkill", "-TERM", "-P", str(job["pid"])])   # mflux, child of /usr/bin/time
    return {"ok": True}


@router.get("/api/gallery")
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


@router.delete("/api/gallery/{name}")
def delete_image(name: str):
    if not re.match(r"^[\w.-]+\.png$", name):
        raise HTTPException(400, "bad name")
    (GALLERY / name).unlink(missing_ok=True)
    (GALLERY / (Path(name).stem + ".json")).unlink(missing_ok=True)
    return {"ok": True}
