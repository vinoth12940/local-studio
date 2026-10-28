"""Routes: undo.

Moved from app.py in the refactor's P1 split.
"""

import json
import re

from fastapi import APIRouter, Body, HTTPException

from studio.config import AGENT_BACKUPS_DIR
from studio.storage.backups import _restore_backup_snap

router = APIRouter()


@router.post("/api/agent/undo")
def agent_undo(backup_id: str = Body(..., embed=True)):
    """Put a file back the way it was before one chat edit (delete it if the edit created it)."""
    if not re.fullmatch(r"[a-f0-9]{16}", backup_id or ""):
        raise HTTPException(400, "bad backup id")
    p = AGENT_BACKUPS_DIR / f"{backup_id}.json"
    try:
        return _restore_backup_snap(p)
    except (OSError, ValueError) as exc:
        if "symbolic link" in str(exc):
            raise HTTPException(400, str(exc))
        raise HTTPException(404, "This change can no longer be undone.")


@router.post("/api/agent/undo-turn")
def agent_undo_turn(body: dict = Body(...)):
    """Revert all file edits made during a turn in reverse chronological order (1-click Turn Checkpoint)."""
    turn_id = str(body.get("turn_id") or "").strip()
    backup_ids = [str(b).strip() for b in (body.get("backup_ids") or []) if re.fullmatch(r"[a-f0-9]{16}", str(b).strip())]
    if not turn_id and not backup_ids:
        raise HTTPException(400, "Provide turn_id or backup_ids to revert.")
    candidates = []
    if backup_ids:
        for bid in backup_ids:
            p = AGENT_BACKUPS_DIR / f"{bid}.json"
            if p.is_file():
                try:
                    snap = json.loads(p.read_text())
                    candidates.append((snap.get("created", 0), p))
                except Exception:
                    continue
    elif turn_id and AGENT_BACKUPS_DIR.is_dir():
        for p in AGENT_BACKUPS_DIR.glob("*.json"):
            try:
                snap = json.loads(p.read_text())
                if snap.get("turn_id") == turn_id:
                    candidates.append((snap.get("created", 0), p))
            except Exception:
                continue
    if not candidates:
        raise HTTPException(404, "No reversible backups found for this turn.")
    candidates.sort(key=lambda item: item[0], reverse=True)
    restored = []
    for _, p in candidates:
        try:
            res = _restore_backup_snap(p)
            restored.append(res)
        except Exception:
            continue
    if not restored:
        raise HTTPException(404, "Could not restore turn changes.")
    files = list(dict.fromkeys(r["name"] for r in restored))
    return {"ok": True, "turn_id": turn_id or None, "restored_count": len(restored), "files": files, "items": restored}
