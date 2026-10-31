"""Saved conversation paths.

Moved from app.py in the refactor's P1 split.
"""

import re

from fastapi import HTTPException

from studio.config import CHATS


_ID = re.compile(r"^[a-f0-9]{8,32}$")


def _chat_path(cid):
    if not _ID.fullmatch(cid):
        raise HTTPException(400, "bad id")
    return CHATS / f"{cid}.json"


# Chat, Code and Voice keep separate conversations. A workspace belongs to Code only: a conversation that has one (older
# ones were never marked) is Code, one marked voice is Voice, anything else is Chat.
def chat_kind(chat):
    kind = chat.get("kind")
    if kind in ("chat", "voice"):
        return kind
    return "code" if kind == "code" or chat.get("workspace") else "chat"


def chat_summary(chat):
    """What the sidebar lists: id, title, updated, plus the kind (and folder) when it is not a Chat conversation."""
    out = {"id": chat["id"], "title": chat.get("title", "Untitled"), "updated": chat.get("updated", 0)}
    kind = chat_kind(chat)
    if kind != "chat":
        out["kind"] = kind
    if kind == "code" and chat.get("workspace"):
        out["workspace"] = chat["workspace"]
    return out
