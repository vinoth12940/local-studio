"""Saved conversation paths.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import re

from fastapi import HTTPException

from studio.config import CHATS


_ID = re.compile(r"^[a-f0-9]{8,32}$")


def _chat_path(cid):
    if not _ID.fullmatch(cid):
        raise HTTPException(400, "bad id")
    return CHATS / f"{cid}.json"
