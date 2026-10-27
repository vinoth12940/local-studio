"""Routes: search.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import subprocess
import threading
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, Response

from studio.infra.workers import isolated_io
from studio import config
from studio.services import models
from studio.services import search as services_search
from studio.config import SEARCH_DIR
from studio.services.models import search_model
from studio.services.search import _reset_index, _stop_search, search_deps_installed, search_job
from studio.storage.settings import load_settings, save_settings

router = APIRouter()


@router.get("/api/search/status")
def search_status():
    m = search_model()
    out = {**load_settings()["search"], "installed": search_deps_installed(), "model_installed": models.model_installed(m),
           "model": {"name": m["name"], "size_gb": m["size_gb"]}, "index_on_disk": SEARCH_DIR.exists(), "running": False,
           "setup": {k: search_job.get(k) for k in ("status", "step", "error")}}
    try:
        r = httpx.get(config.SEARCH_URL + "/status", timeout=3, headers=services_search._search_headers())
        if r.status_code == 200:
            out.update(r.json(), running=True)
    except httpx.HTTPError:
        pass
    return out


@router.post("/api/search")
def search_query(body: dict = Body(...)):
    return services_search._search_proxy("POST", "/search", json=body).json()


@router.post("/api/search/enable")
def search_enable():
    if search_job.get("status") != "running":
        search_job.clear()
        search_job.update(status="running", step="Starting")
        threading.Thread(target=services_search._enable_search, daemon=True).start()
    return {k: search_job.get(k) for k in ("status", "step", "error")}


@router.post("/api/search/disable")
def search_disable():
    s = load_settings()
    s["search"]["enabled"] = False
    save_settings(s)
    _stop_search()
    return {"ok": True}


@router.post("/api/search/start")
def search_start():
    """Called by start.sh: bring the service up if the user has Search turned on."""
    if not load_settings()["search"]["enabled"]:
        return {"status": "off"}
    if search_deps_installed() and models.model_installed(search_model()):
        services_search._start_search()
        return {"status": "starting"}
    search_enable()                                   # an update changed the components (or the model is gone): set up again
    return {"status": "setting up"}


@router.put("/api/search/settings")
def search_settings(body: dict = Body(...)):
    s = load_settings()
    for key in ("folders", "removed"):
        if key in body:
            paths = [str(Path(p.strip()).expanduser()) for p in body[key] if isinstance(p, str) and p.strip()]
            if key == "folders":
                bad = next((p for p in paths if not Path(p).is_absolute() or not Path(p).is_dir()), None)
                if bad:
                    raise HTTPException(400, f"Not a folder on this Mac: {bad}")
            s["search"][key] = list(dict.fromkeys(paths))
    for key in ("paused", "on_battery"):
        if key in body:
            s["search"][key] = bool(body[key])
    save_settings(s)
    try:
        httpx.post(config.SEARCH_URL + "/reload", timeout=3, headers=services_search._search_headers())
    except httpx.HTTPError:
        pass
    return s["search"]


@router.post("/api/search/rebuild")
def search_rebuild():
    if not load_settings()["search"]["enabled"]:
        raise HTTPException(409, "Turn Search on first.")
    return _reset_index(restart=True)


@router.delete("/api/search/index")
def search_delete_index():
    return _reset_index(restart=load_settings()["search"]["enabled"])


@router.get("/api/search/errors")
def search_errors():
    return services_search._search_proxy("GET", "/errors").json()


@router.get("/api/search/thumb/{file_id}")
@isolated_io
def search_thumb(file_id: int, t: int = 0):
    r = services_search._search_proxy("GET", f"/thumb/{file_id}", params={"t": t})
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@router.get("/api/search/preview/{file_id}")
@isolated_io
def search_preview(file_id: int, chunk_id: int | None = None):
    return services_search._search_proxy("GET", f"/preview/{file_id}", params={"chunk_id": chunk_id} if chunk_id else None).json()


@router.get("/api/search/page/{file_id}/{page}")
@isolated_io
def search_page(file_id: int, page: int, chunk_id: int | None = None):
    r = services_search._search_proxy("GET", f"/page/{file_id}/{page}", params={"chunk_id": chunk_id} if chunk_id else None)
    return Response(r.content, media_type="image/jpeg", headers={"X-Pages": r.headers.get("x-pages", ""), "Cache-Control": "no-store"})


@router.get("/api/search/image/{file_id}")
@isolated_io
def search_image(file_id: int):
    return Response(services_search._search_proxy("GET", f"/image/{file_id}").content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/api/search/media/{file_id}")
@isolated_io
def search_media(file_id: int):
    """The audio or video file itself, for the preview player (FileResponse handles the player's
    range requests). The path comes from the index, never from the request, and nothing else is served."""
    f = services_search._search_proxy("GET", f"/file/{file_id}").json()
    if f.get("kind") not in ("audio", "video") or not Path(f["path"]).is_file():
        raise HTTPException(404, "No player preview for this file.")
    return FileResponse(f["path"])


@router.post("/api/search/open")
def search_open(file_id: int = Body(...), path: str = Body(...), reveal: bool = Body(False)):
    """Open or reveal an indexed file. The path comes from the index, never from the request: the
    request's path only has to match, so an id reused after a rebuild can't open a different file."""
    try:
        f = services_search._search_proxy("GET", f"/file/{file_id}").json()
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "This file is no longer in the search index.")
        raise
    if f["path"] != path or not Path(f["path"]).exists():
        raise HTTPException(404, "This file has moved or is no longer in the search index.")
    subprocess.run(["open", "-R", f["path"]] if reveal else ["open", f["path"]])
    return {"ok": True}


@router.post("/api/search/choose-folder")
def search_choose_folder():
    """The standard macOS folder chooser. `tell me to activate` brings it in front of the app window."""
    try:
        r = subprocess.run(["osascript", "-e", "tell me to activate", "-e",
                            'POSIX path of (choose folder with prompt "Choose a folder to index")'],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"path": None}
    return {"path": r.stdout.strip().rstrip("/") or None if r.returncode == 0 else None}
