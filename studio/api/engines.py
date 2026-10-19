"""Routes: engines.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import asyncio
import threading

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from studio import config
from studio.services import engines
from studio.services import models
from studio.config import AGENT_BUSY_MSG, agent_lock, ENGINES
from studio.services.engines import _ensure_quiet, _stop_engine, engine_lock
from studio.services.models import chat_model

router = APIRouter()


@router.post("/api/engine/start")
def engine_start():
    if agent_lock.locked():
        raise HTTPException(409, AGENT_BUSY_MSG)
    m = chat_model()
    if not models.model_installed(m):
        raise HTTPException(409, f"Download {m['name']} first (Model library).")
    mode = "gguf" if m.get("backend") == "gguf" else "text"
    threading.Thread(target=_ensure_quiet, args=(mode, m), daemon=True).start()
    return {"status": "starting", "mode": mode, "model_id": m["id"]}


@router.post("/api/engine/stop")
def engine_stop():
    if agent_lock.locked():
        raise HTTPException(409, AGENT_BUSY_MSG)
    with engine_lock:
        for mode in ENGINES:
            _stop_engine(mode)
    return {"status": "stopped"}


@router.get("/api/llm/{route:path}")
async def llm_get(route: str):
    st = await asyncio.to_thread(engines.engine_state)
    mode, m = st.get("mode"), chat_model(st.get("model_id"))
    if mode in ("vision", "gguf"):
        ready = await asyncio.to_thread(engines._engine_ready, mode)
        if route == "health":
            return {"status": "ok" if ready else "loading", "mode": mode, "target": m["repo"],
                    "model_id": m["id"], "model_name": m["name"], "drafter": m.get("drafter") if mode == "vision" else None,
                    "max_draft": None, "kv_bits": None, "context_window": config.CONTEXT, "warnings": []}
        return JSONResponse({"engine": mode}, status_code=200 if ready else 503)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ENGINES['text']['url']}/{route}")
        j = r.json()
        if route == "health" and isinstance(j, dict):
            j.update(model_id=m["id"], model_name=m["name"])
        return JSONResponse(j, status_code=r.status_code)
    except (httpx.HTTPError, ValueError) as e:                 # ValueError: an answer that isn't JSON (an engine still starting, a proxy page)
        return JSONResponse({"status": "starting" if mode else "down", "model_id": m["id"], "model_name": m["name"],
                             "error": str(e)}, status_code=503)
