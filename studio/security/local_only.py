"""LocalOnly: every route and static file answers only this Mac's own page and local scripts.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

from fastapi.responses import JSONResponse


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _local_origin(host, origin):
    """A request from this app's own page or a local script: a loopback Host, and a loopback
    Origin if a browser sent one."""
    name = lambda netloc: (netloc or "").rsplit(":", 1)[0].strip("[]")                    # noqa: E731
    return name(host) in LOOPBACK and (not origin or name(origin.split("://", 1)[-1]) in LOOPBACK)


class LocalOnly:
    """Every route is the app's own: search, chats, the gallery and the agent return the user's data or drive the Mac, and the
    rest can clear storage, delete models or uninstall. So a page on another site, on another local port (Sec-Fetch-Site
    same-site) or behind a public DNS name that resolves to 127.0.0.1 (DNS rebinding, 127.0.0.1.nip.io, localtest.me) gets 403
    on every route and static file. The app's own page is same-origin (or "none" for a navigation) on a loopback Host, and local
    scripts (start.sh, the Mac app) send no Origin. Plain ASGI, so streaming is untouched."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            h = dict(scope["headers"])
            site = h.get(b"sec-fetch-site", b"").decode("latin-1").strip().lower()
            if site in ("cross-site", "same-site") or not _local_origin(h.get(b"host", b"").decode("latin-1"), h.get(b"origin", b"").decode("latin-1")):
                if scope["type"] == "websocket":
                    return await send({"type": "websocket.close", "code": 1008})
                return await JSONResponse({"detail": "Forbidden"}, status_code=403)(scope, receive, send)
        await self.inner(scope, receive, send)
