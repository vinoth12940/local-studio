"""The chat workspace: a ContextVar, never os.chdir.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import contextvars
from pathlib import Path

from studio.config import HOME, ROOT


# The chat's workspace folder. Set only inside the worker thread of one tool call (asyncio.to_thread copies the
# context), so it never leaks between requests; _chat_read_tool keeps its 3-argument signature.
_chat_workspace: contextvars.ContextVar = contextvars.ContextVar("_chat_workspace", default=None)
_chat_turn_id: contextvars.ContextVar = contextvars.ContextVar("_chat_turn_id", default=None)


def _harness_root() -> Path:
    """Where relative paths and commands start: the chat's workspace, else the checkout (dev) or home (inside the .app)."""
    ws = _chat_workspace.get()
    if ws:
        return Path(ws)
    return HOME if any(part.endswith(".app") for part in ROOT.parts) else ROOT


def _in_workspace(workspace, fn, *args, turn_id=None):
    """Run fn(*args) with the chat's workspace set; meant for asyncio.to_thread so the setting stays in that thread."""
    workspace_token = _chat_workspace.set(workspace)
    turn_token = _chat_turn_id.set(turn_id) if turn_id is not None else None
    try:
        return fn(*args)
    finally:
        if turn_token is not None:
            _chat_turn_id.reset(turn_token)
        _chat_workspace.reset(workspace_token)
