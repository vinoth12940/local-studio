"""Search service: api. Moved verbatim from search.py (P6)."""
import subprocess
from pathlib import Path

from studio.search.extractors import _load_image, _video_frames
from studio.search.indexer import shutdown
from studio.search.preview import _needles, large_image, preview_info, render_page
from studio.search.ranking import RESULT_LIMIT, read_file_evidence

# ---------------------------------------------------------------- HTTP API

def _local_request(host, origin, fetch_site=None):
    """Only this Mac's own app calls the service: a loopback Host (no DNS rebinding), no browser
    Origin and no cross-site Sec-Fetch-Site (app.py calls it server-side, a web page never should:
    not by fetch, and not by embedding /preview/N in an iframe, which sends no Origin)."""
    name = (host or "").rsplit(":", 1)[0].strip("[]")
    if (fetch_site or "").strip().lower() in ("cross-site", "same-site"):
        return False
    return name in ("127.0.0.1", "localhost", "::1") and not origin


def make_app(store, embedder, indexer, searcher, token):
    from studio.search.auth import authorized
    if not token:
        raise ValueError("the search service needs a token")
    import io
    from contextlib import asynccontextmanager
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import PlainTextResponse, Response

    @asynccontextmanager
    async def lifespan(_):
        yield
        shutdown(indexer)                                 # runs on SIGTERM, before uvicorn lets the process die

    api = FastAPI(lifespan=lifespan)

    async def app(scope, receive, send):                  # plain ASGI wrapper: these routes return the user's file contents
        if scope["type"] == "http":
            h = dict(scope["headers"])
            if not _local_request(h.get(b"host", b"").decode("latin-1"), h.get(b"origin", b"").decode("latin-1"),
                                  h.get(b"sec-fetch-site", b"").decode("latin-1")):
                return await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)
            if scope["path"] != "/health" and not authorized(h, token):
                return await PlainTextResponse("Search service token required", status_code=401, headers={"Cache-Control":"no-store"})(scope, receive, send)
        await api(scope, receive, send)

    @api.get("/health")
    def health():
        return {"ok": True}

    @api.get("/status")
    def status():
        return indexer.status()

    @api.post("/search")
    def search(body: dict = Body(...)):
        if indexer.config().get("incremental") and indexer.full_indexing:
            raise HTTPException(503, "Search index is being rebuilt", headers={"Retry-After": "1"})
        if indexer.error:
            raise HTTPException(503, indexer.error)
        limit = max(1, min(int(body.get("limit") or RESULT_LIMIT), 100))
        sort = body.get("sort", "relevance")
        if sort not in ("relevance", "newest"): raise HTTPException(400, "sort must be relevance or newest")
        return {"results": searcher.search(body.get("query"), body.get("kind") or None, body.get("folder") or None, limit, sort)}

    @api.post("/passages")
    def passages(body: dict = Body(...)):
        if indexer.config().get("incremental") and indexer.full_indexing:
            raise HTTPException(503, "Search index is being rebuilt", headers={"Retry-After": "1"})
        if indexer.error:
            raise HTTPException(503, indexer.error)
        return {"passages": searcher.passages(body.get("query"), int(body.get("limit") or 6), int(body.get("max_chars") or 12000))}

    @api.post("/reload")
    def reload():
        indexer.wake.set()
        return {"ok": True}

    @api.get("/errors")
    def errors():
        return {"errors": store.errors()}

    @api.get("/file/{file_id}")
    def file(file_id: int):
        f = store.file_info([file_id]).get(file_id)
        if not f:
            raise HTTPException(404, "not in the index")
        return f

    @api.get("/text/{file_id}")
    def text(file_id: int, start_page: int = 1, max_pages: int = 2):
        if start_page < 1 or not 1 <= max_pages <= 4: raise HTTPException(400, "Use start_page>=1 and max_pages between 1 and 4")
        try: evidence = read_file_evidence(store, file_id, start_page, max_pages)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error: raise HTTPException(422, str(error))
        if evidence is None: raise HTTPException(404, "not in the index")
        return evidence

    @api.get("/preview/{file_id}")
    def preview(file_id: int, chunk_id: int | None = None):
        info = preview_info(store, file_id, chunk_id)
        if info is None:
            raise HTTPException(404, "not in the index")
        return info

    @api.get("/page/{file_id}/{page}")
    def page(file_id: int, page: int, chunk_id: int | None = None):
        info = preview_info(store, file_id, chunk_id)
        if info is None or info["type"] != "page":
            raise HTTPException(404, "no page preview for this file")
        marks = _needles(info["match"]) if chunk_id and page == info["page"] else ()      # only the matching page is marked
        try:
            data, total, _ = render_page(info["path"], page, marks)
        except Exception as e:
            raise HTTPException(404, f"This page couldn't be shown: {type(e).__name__}")
        return Response(data, media_type="image/jpeg", headers={"X-Pages": str(total), "Cache-Control": "no-store"})

    @api.get("/image/{file_id}")
    def image(file_id: int):
        f = store.file_info([file_id]).get(file_id)
        try:
            data = large_image(f["path"]) if f and f["kind"] == "image" else None
        except Exception:
            data = None
        if data is None:
            raise HTTPException(404, "image unavailable")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @api.get("/thumb/{file_id}")
    def thumb(file_id: int, t: int = 0):
        f = store.file_info([file_id]).get(file_id)
        if not f or f["kind"] not in ("image", "video"):
            raise HTTPException(404, "not an indexed image or video")
        try:
            if f["kind"] == "video":
                frames = _video_frames(Path(f["path"]), times=[t], ahead=90)
                im = frames[0][1] if frames else None
            else:
                im = _load_image(Path(f["path"]))
        except Exception:
            im = None
        if im is None:
            raise HTTPException(404, "image unavailable")
        im.thumbnail((320, 320))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    return app
