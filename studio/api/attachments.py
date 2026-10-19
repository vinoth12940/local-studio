"""Routes: attachments.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import asyncio
import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from studio.config import UPLOADS
from studio.security.uploads import mismatch
from studio.security.fences import _harness_path_fence
from studio.storage.atomic import write_text_atomic
from studio.storage.flags import flag
from studio.services.attachments import _AID, _extract, MAX_DOC_CHARS

router = APIRouter()


@router.post("/api/attachments")
async def upload_attachment(file: UploadFile = File(...)):
    data = await file.read(60 * 2**20 + 1)
    if len(data) > 60 * 2**20:
        raise HTTPException(413, "File is larger than 60 MB.")
    name = Path(file.filename or "file").name
    ext = Path(name).suffix.lower()
    if flag("security.upload_sniff") and (why := mismatch(data, ext)):
        raise HTTPException(415, f"{name}: {why}")
    aid = uuid.uuid4().hex[:16]
    src = UPLOADS / f"{aid}-src{ext}"
    src.write_bytes(data)
    try:
        info = await asyncio.to_thread(_extract, src, ext, aid)
    except HTTPException:
        src.unlink(missing_ok=True)
        raise
    except Exception as e:
        src.unlink(missing_ok=True)
        raise HTTPException(422, f"Couldn't read {name}: {e}")
    meta = {"id": aid, "name": name, "size": len(data), **{k: v for k, v in info.items() if k != "text"}}
    if "text" in info:
        text = info["text"].strip()
        if not text:
            src.unlink(missing_ok=True)
            raise HTTPException(422, f"No readable text found in {name}.")
        meta["chars"] = len(text)
        meta["truncated"] = len(text) > MAX_DOC_CHARS
        (UPLOADS / f"{aid}.txt").write_text(text[:MAX_DOC_CHARS])
        meta["tokens_est"] = min(len(text), MAX_DOC_CHARS) // 4
    if ext not in (".pdf", ".docx", ".xlsx", ".xlsm"):
        src.unlink(missing_ok=True)                       # images/text are fully captured above
    write_text_atomic(UPLOADS / f"{aid}.json", json.dumps(meta))
    return meta


@router.get("/api/attachments/{aid}/{name}")
def attachment_file(aid: str, name: str):
    if not _AID.match(aid) or not re.match(r"^[a-f0-9]{16}-\d+\.jpg$", name) or not name.startswith(aid):
        raise HTTPException(400, "bad path")
    try:
        p = _harness_path_fence(str(UPLOADS / name), write=False)
        p.relative_to(UPLOADS.resolve())
    except ValueError:
        raise HTTPException(400, "bad path")
    if not p.exists():
        raise HTTPException(404, "missing")
    return FileResponse(p, media_type="image/jpeg", headers={"Content-Disposition": f'inline; filename="{name}"',
                                                              "Cache-Control": "private, max-age=3600"})
