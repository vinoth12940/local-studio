"""Routes: chats.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import json
import time

from fastapi import APIRouter, Body, HTTPException

from studio.config import CHATS
from studio.services.agent.loop import _forget_chat_context
from studio.storage.chats import _chat_path

router = APIRouter()


# ---------------------------------------------------------------- saved conversations

@router.get("/api/chats")
def list_chats():
    out = []
    for p in CHATS.glob("*.json"):
        try:
            c = json.loads(p.read_text())
            out.append({"id": c["id"], "title": c.get("title", "Untitled"), "updated": c.get("updated", 0)})
        except Exception:
            continue
    return sorted(out, key=lambda c: c["updated"], reverse=True)


@router.get("/api/chats/{cid}")
def get_chat(cid: str):
    p = _chat_path(cid)
    if not p.exists():
        raise HTTPException(404, "not found")
    return json.loads(p.read_text())


@router.put("/api/chats/{cid}")
def put_chat(cid: str, chat: dict = Body(...)):
    chat["id"] = cid
    chat["updated"] = time.time()
    _chat_path(cid).write_text(json.dumps(chat))
    return {"ok": True, "updated": chat["updated"]}


@router.delete("/api/chats/{cid}")
def delete_chat(cid: str):
    _chat_path(cid).unlink(missing_ok=True)
    _forget_chat_context(cid)                                # the chat's turns also went to its agent runs as context
    return {"ok": True}
