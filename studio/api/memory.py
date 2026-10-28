"""Routes: memory.

Moved from app.py in the refactor's P1 split.
"""

from datetime import datetime

from fastapi import APIRouter, Body, HTTPException

from studio.storage.memory import _load_agent_memory, _save_agent_memory

router = APIRouter()


@router.get("/api/agent/memory")
def api_get_memory():
    return {"memories": _load_agent_memory()}


@router.post("/api/agent/memory")
def api_set_memory(body: dict = Body(...)):
    key = str(body.get("key") or "").strip()[:120]
    value = str(body.get("value") or "").strip()[:2000]
    if not key:
        raise HTTPException(400, "Memory key is required.")
    mem = _load_agent_memory()
    if not value:
        mem.pop(key, None)
    else:
        mem[key] = {"value": value, "updated": datetime.now().astimezone().isoformat(timespec="seconds")}
    _save_agent_memory(mem)
    return {"ok": True, "memories": mem}


@router.delete("/api/agent/memory/{key}")
def api_delete_memory(key: str):
    mem = _load_agent_memory()
    mem.pop(key, None)
    _save_agent_memory(mem)
    return {"ok": True, "memories": mem}
