"""Optional JSON-line request log (flag logging.json_requests, off by default).

One line per request that was slow (>= 250 ms), failed (5xx) or changed something (not GET/HEAD), in logs/requests.jsonl next
to the existing logs (ui.log and the rest are unchanged). It records the path without its query string, the status, the
duration and the engine the app had recorded, never bodies or headers. The file rotates at 5 MB (one .1 copy is kept).
Streams are timed until their last byte.
"""
import json
import os
import threading
import time
import uuid

from studio.config import LOGS
from studio.security.redact import redact_obj
from studio.storage.flags import flag

LOG = LOGS / "requests.jsonl"
MAX_BYTES = 5_000_000
SLOW_S = 0.25
_write_lock = threading.Lock()


def _engine():
    try:
        st = json.loads((LOGS / "engine.json").read_text())
        return {"mode": st.get("mode"), "model_id": st.get("model_id")}
    except (OSError, ValueError):
        return None


def _write(entry):
    if flag("security.redact"):
        entry = redact_obj(entry)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    with _write_lock:
        try:
            if LOG.exists() and LOG.stat().st_size > MAX_BYTES:
                os.replace(LOG, LOG.with_name(LOG.name + ".1"))
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass                                             # logging never breaks a request


class RequestLog:
    """Plain ASGI, so streaming responses pass through untouched."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not flag("logging.json_requests"):
            return await self.inner(scope, receive, send)
        t0, status = time.monotonic(), {"code": None}

        async def send_wrapper(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)
        try:
            await self.inner(scope, receive, send_wrapper)
        finally:
            dt = time.monotonic() - t0
            code = status["code"]
            if dt >= SLOW_S or scope.get("method") not in ("GET", "HEAD") or code is None or code >= 500:
                _write({"ts": round(time.time(), 3), "request_id": uuid.uuid4().hex[:12], "method": scope.get("method"),
                        "path": scope.get("path"), "status": code, "ms": round(dt * 1000, 1), "engine": _engine()})
