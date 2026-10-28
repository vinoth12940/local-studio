"""Routes: models.

Moved from app.py in the refactor's P1 split.
"""

import os
import re
import shutil
import signal
import threading
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, HTTPException

from studio.services import engines
from studio.services import models as services_models
from studio.config import CATALOG, HF_HUB, HOME, LLAMA_SERVER
from studio.services.engines import _ensure_quiet
from studio.services.models import (
    _delete_repo, _find_any, _repo_bytes, _repo_for_dir, _row, all_models, chat_models, downloads, model_bytes,
    model_parts, start_download,
)
from studio.storage.settings import load_settings, save_settings

router = APIRouter()


@router.get("/api/models")
def models():
    s = load_settings()
    eng = engines.engine_state()
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


@router.post("/api/models/download")
def download_model(id: str = Body(..., embed=True)):
    m = _find_any(id)
    if m.get("backend") == "gguf" and not Path(LLAMA_SERVER).exists():
        raise HTTPException(409, "llama.cpp isn't installed. Run: brew install llama.cpp")
    need = int((m.get("size_gb") or 0) * 1e9) - model_bytes(m)
    if need > shutil.disk_usage(HOME).free - 5e9:
        raise HTTPException(507, f"Not enough disk space: needs about {need / 1e9:.0f} GB.")
    return start_download(m)


@router.post("/api/models/cancel")
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


@router.delete("/api/models")
def delete_model(id: str = "", repo: str = ""):
    """Delete a catalog/custom model by id, or any cached repo by name."""
    if repo:
        if any(repo in {r for r, _ in model_parts(m)} for m in all_models()):
            raise HTTPException(400, "Delete it from the model library instead.")
        return {"ok": True, "freed_bytes": _delete_repo(repo)}
    m = _find_any(id)
    if m in CATALOG["search"] and load_settings()["search"]["enabled"]:
        raise HTTPException(409, "Search is using this model. Turn Search off first.")
    eng = engines.engine_state()
    if eng.get("model_id") == m["id"] and eng.get("mode"):
        raise HTTPException(409, "This model is running. Stop the engine or switch models first.")
    shared = set()
    for other in all_models():
        if other["id"] != m["id"] and services_models.model_installed(other):
            shared |= {r for r, _ in model_parts(other)}
    freed = sum(_delete_repo(r) for r, _ in model_parts(m) if r not in shared)
    s = load_settings()
    if m.get("custom"):
        s["custom"] = [c for c in s["custom"] if c["id"] != m["id"]]
        if s["chat_model"] == m["id"]:
            s["chat_model"] = next(c["id"] for c in CATALOG["chat"] if c.get("default"))
        save_settings(s)
    return {"ok": True, "freed_bytes": freed}


@router.post("/api/models/active")
def set_active(id: str = Body(..., embed=True), kind: str = Body("chat", embed=True)):
    m = _find_any(id)
    if not services_models.model_installed(m):
        raise HTTPException(409, "Download the model first.")
    s = load_settings()
    if kind == "image":
        s["image_model"] = id
        save_settings(s)
        return {"ok": True}
    s["chat_model"] = id
    save_settings(s)
    eng = engines.engine_state()
    if eng.get("mode") and eng.get("model_id") != id:      # swap the running engine in the background
        mode = "gguf" if m.get("backend") == "gguf" else "text"
        threading.Thread(target=_ensure_quiet, args=(mode, m), daemon=True).start()
    return {"ok": True}


# ---- Hugging Face search (for models outside the catalog)

@router.get("/api/hf/search")
async def hf_search(q: str, kind: str = "mlx"):
    params = {"search": q, "sort": "downloads", "direction": "-1", "limit": "25",
              "filter": "gguf" if kind == "gguf" else "mlx"}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://huggingface.co/api/models", params=params)
    if r.status_code != 200:
        raise HTTPException(502, "Hugging Face search failed")
    return [{"repo": m["id"], "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
             "pipeline": m.get("pipeline_tag")} for m in r.json()]


@router.get("/api/hf/files")
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


@router.post("/api/models/custom")
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
