"""Pre-edit backups behind Undo and Revert turn.

Moved from app.py in the refactor's P1 split.
"""

import json
import os
import time
import uuid
from pathlib import Path

from studio.config import AGENT_BACKUPS_DIR
from studio.infra.workspace import _chat_turn_id
from studio.security.fences import _harness_path_fence


def _backup_file(target, turn_id=None):
    """Snapshot a file (or its absence) before the model changes it; returns the backup id for Undo."""
    AGENT_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    content = target.read_text(encoding="utf-8", errors="replace") if existed else None
    if existed and len(content) > 2_000_000:
        return None                                          # too large to keep a copy of; the edit still runs
    bid = uuid.uuid4().hex[:16]
    tid = turn_id or _chat_turn_id.get()
    (AGENT_BACKUPS_DIR / f"{bid}.json").write_text(json.dumps({
        "id": bid, "turn_id": tid, "path": str(target), "existed": existed,
        "content": content, "created": time.time()
    }))
    for old in sorted(AGENT_BACKUPS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-100]:
        old.unlink(missing_ok=True)                          # keep the newest 100
    return bid


def _restore_backup_snap(p: Path) -> dict:
    snap = json.loads(p.read_text())
    target = _harness_path_fence(snap.get("path"), write=True)
    if target.is_symlink():
        raise ValueError("Cannot undo over a symbolic link.")
    if snap.get("existed"):
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(snap.get("content") or "", encoding="utf-8")
        os.replace(tmp, target)
    else:
        target.unlink(missing_ok=True)
    p.unlink(missing_ok=True)
    return {"ok": True, "backup_id": snap.get("id"), "path": str(target), "name": target.name, "restored": bool(snap.get("existed"))}
