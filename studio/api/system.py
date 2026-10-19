"""Routes: system.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

from fastapi.responses import FileResponse
from fastapi import APIRouter

from studio.services import harness
from studio.config import agent_lock, APP_VERSION, EDIT_MODEL, ROOT
from studio.infra.system import system_memory
from studio.services.images import jobs

router = APIRouter()


@router.get("/api/system")
def api_system():
    return {**system_memory(), "edit_enabled": bool(EDIT_MODEL), "version": APP_VERSION}


@router.get("/api/busy")
def api_busy():
    """Read by the search service between batches: it waits while the Mac is doing user-facing work."""
    return {"busy": harness.active_chats > 0 or agent_lock.locked() or any(j.get("status") in ("queued", "running") for j in jobs.values())}


@router.get("/")
def index():
    # no-cache: the app window must revalidate, or it keeps showing the previous version's page after an update
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-cache"})
