"""Routes: terminal.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import asyncio
import base64
import json

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from studio.infra.workspace import _harness_root
from studio.security.fences import _harness_path_fence
from studio.services.terminal import _TERM_MAX_SESSIONS, _Terminal, _terminal, _terminals, _terminals_lock

router = APIRouter()


@router.get("/api/terminals")
def api_terminals():
    return {"terminals": [t.info() for t in _terminals.values()]}


@router.post("/api/terminals")
def api_terminal_open(body: dict = Body(default={})):
    workspace = body.get("workspace")
    try:
        cwd = _harness_path_fence(workspace, write=False) if workspace else _harness_root()
    except ValueError as err:
        raise HTTPException(400, str(err))
    if not cwd.is_dir():
        raise HTTPException(400, f"Workspace directory not found: {cwd}")
    with _terminals_lock:
        for tid in [t for t, term in _terminals.items() if term.exit_code is not None]:
            _terminals.pop(tid).close()
        if len(_terminals) >= _TERM_MAX_SESSIONS:
            raise HTTPException(429, f"At most {_TERM_MAX_SESSIONS} terminals can be open. Close one first.")
        term = _Terminal(cwd, int(body.get("rows") or 24), int(body.get("cols") or 80))
        _terminals[term.id] = term
    return term.info()


@router.get("/api/terminals/{tid}/stream")
async def api_terminal_stream(tid: str, request: Request, offset: int = 0):
    term = _terminal(tid)

    async def events():
        pos = max(0, offset)
        while True:
            if await request.is_disconnected():
                return
            data, end, rc = await asyncio.to_thread(term.read, pos, 3)
            if data:
                pos = end
                yield f"data: {json.dumps({'d': base64.b64encode(data).decode(), 'o': end})}\n\n"
            elif rc is not None:
                yield f"data: {json.dumps({'exit': rc})}\n\n"
                return
            else:
                return                          # idle: end the response (a stopping server waits for open ones); the page reconnects from its offset

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@router.post("/api/terminals/{tid}/input")
def api_terminal_input(tid: str, body: dict = Body(...)):
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, str) or len(data) > 1_000_000:
        raise HTTPException(400, "data must be a string")
    try:
        _terminal(tid).write(data.encode("utf-8", errors="surrogateescape"))
    except (ValueError, OSError) as err:
        raise HTTPException(409, str(err))
    return {"ok": True}


@router.post("/api/terminals/{tid}/resize")
def api_terminal_resize(tid: str, body: dict = Body(...)):
    try:
        _terminal(tid).resize(int(body.get("rows") or 24), int(body.get("cols") or 80))
    except (ValueError, OSError, TypeError) as err:
        raise HTTPException(400, str(err))
    return {"ok": True}


@router.delete("/api/terminals/{tid}")
def api_terminal_close(tid: str):
    with _terminals_lock:
        term = _terminals.pop(tid, None)
    if term is None:
        raise HTTPException(404, "No such terminal.")
    term.close()
    return {"ok": True}
