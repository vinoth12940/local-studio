"""Routes: updates.

Moved from app.py in the refactor's P1 split.
"""

import asyncio
import threading
import time

from fastapi import APIRouter

from studio.services.updates import _run_update, _versions, update_job

router = APIRouter()


@router.get("/api/versions")
async def versions():
    return {**(await asyncio.to_thread(_versions)), "update": {k: v for k, v in update_job.items() if k != "proc"}}


@router.post("/api/versions/update")
def update_engines():
    if update_job.get("status") == "running":
        return update_job
    update_job.clear()
    update_job.update(status="running", started=time.time())
    threading.Thread(target=_run_update, daemon=True).start()
    return update_job
