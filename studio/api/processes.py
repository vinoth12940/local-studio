"""Routes: processes.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

from fastapi import APIRouter, HTTPException

from studio.infra.processes import _manage_bg_process

router = APIRouter()


@router.get("/api/processes")
def api_list_processes():
    return _manage_bg_process("list")


@router.post("/api/processes/{proc_id}/stop")
def api_stop_process(proc_id: str):
    try:
        return _manage_bg_process("stop", proc_id)
    except ValueError as err:
        raise HTTPException(404, str(err))
