"""Routes: system.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from studio.services import engines, harness, search
from studio.config import agent_lock, APP_VERSION, EDIT_MODEL, ROOT
from studio.infra.system import system_memory
from studio.services.images import jobs

router = APIRouter()


@router.get("/api/system")
def api_system():
    return {**system_memory(), "edit_enabled": bool(EDIT_MODEL), "version": APP_VERSION}


@router.get("/api/health")
def api_health():
    """Readiness for scripts and the Mac app (added in the refactor's P1): no locks, and no user data beyond which model the
    engine runs. Reads engine_state and _search_running through their modules, where the suites patch them."""
    st = engines.engine_state()
    return {"ok": True, "version": APP_VERSION, "engine": {"mode": st.get("mode"), "model_id": st.get("model_id")},
            "search": "up" if search._search_running() else "down"}


@router.get("/api/busy")
def api_busy():
    """Read by the search service between batches: it waits while the Mac is doing user-facing work."""
    return {"busy": harness.active_chats > 0 or agent_lock.locked() or any(j.get("status") in ("queued", "running") for j in jobs.values())}


@router.get("/")
def index():
    # no-cache: the app window must revalidate, or it keeps showing the previous version's page after an update
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-cache"})
