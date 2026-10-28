"""Routes: system.

Moved from app.py in the refactor's P1 split.
"""

import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from studio.services import engines, harness, search
from studio.security.redact import redact
from studio.storage.flags import flag
from studio.config import agent_lock, APP_VERSION, EDIT_MODEL, LOGS, ROOT
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
    if flag("security.api_token"):
        return {"ok": True, "version": APP_VERSION, "engine": {"mode": None, "model_id": None}, "search": "up" if search._search_running() else "down"}
    st = engines.engine_state()
    return {"ok": True, "version": APP_VERSION, "engine": {"mode": st.get("mode"), "model_id": st.get("model_id")},
            "search": "up" if search._search_running() else "down"}


@router.post("/api/csp-report")
async def api_csp_report(request: Request):
    """Content-Security-Policy violation reports (flag security.csp), one JSON line each in logs/csp.log (capped at 1 MB)."""
    if flag("security.csp") not in ("report-only", "enforce"):
        return Response(status_code=204)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 16384:
            return Response(status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks).decode("utf-8", "replace")
    if flag("security.redact"):
        body = redact(body)
    log = LOGS / "csp.log"
    try:
        if not log.exists() or log.stat().st_size < 1_000_000:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": round(time.time(), 3), "report": body}) + "\n")
    except OSError:
        pass
    return Response(status_code=204)


@router.get("/api/busy")
def api_busy():
    """Read by the search service between batches: it waits while the Mac is doing user-facing work."""
    return {"busy": harness.active_chats > 0 or agent_lock.locked() or any(j.get("status") in ("queued", "running") for j in jobs.values())}


@router.get("/")
def index():
    # no-cache: the app window must revalidate, or it keeps showing the previous version's page after an update
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-cache"})
