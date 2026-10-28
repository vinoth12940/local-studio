"""The decision service: `python decide.py --port 8085` runs d1-2B, Liquid's decision model, for the live-facts check.

The backend (studio/services/decide.py) starts it with the mlx-dspark environment's python, which has mlx-vlm, when Voice is
on and the voice.decide flag is set. mlx-vlm's own server cannot serve d1-2B yet (its decision route needs a model class
that declares `decision_types`), so this small service calls the model's `system_one()`, the API its model card documents.

Only the backend talks to it: a browser request (any Origin or Sec-Fetch-Site), a non-loopback Host and anything without the
per-install token are refused, as on voice.py. GET /health says whether the model is loaded; POST /decide {"state",
"questions"} returns d1's answers. Request text is never logged. Standard library plus mlx-vlm only: no studio imports.
"""
import argparse
import hmac
import http.server
import json
import os
import signal
import sys
import threading
from urllib.parse import urlsplit

TOKEN_ENV = "LOCAL_STUDIO_DECIDE_TOKEN"
MODEL = os.environ.get("LOCAL_STUDIO_DECIDE_MODEL", "LiquidAI/d1-2B-MLX-4bit")
MAX_STATE, MAX_QUESTIONS, MAX_INSTRUCTIONS = 2000, 8, 400


def _load_d1(repo):
    from mlx_vlm import load
    model, _ = load(repo)
    if not hasattr(model, "system_one"):
        raise ValueError(f"{repo} is not a d1 decision model")
    return model


class Decider:
    """The loaded model and one lock: MLX runs one forward pass at a time."""

    def __init__(self, token, load=_load_d1):
        if not token:
            raise ValueError("the decision service needs a token")
        self.token, self.model, self.error, self._load = token, None, None, load
        self.lock = threading.Lock()

    def warm(self):
        try:
            self.model = self._load(MODEL)
            self.model.system_one("Hello.", {"warm": {"type": "noul", "instructions": "Is this a greeting?"}})
        except Exception as e:                       # reported on /health; the backend falls back to its rules
            self.model, self.error = None, f"{type(e).__name__}: {str(e)[:200]}"

    def decide(self, state, questions):
        with self.lock:
            return self.model.system_one(state, questions)


def _valid(body):
    """(state, questions) from a request body, or raise ValueError."""
    state, questions = body.get("state"), body.get("questions")
    if not isinstance(state, str) or not state.strip() or len(state) > MAX_STATE:
        raise ValueError(f"state must be text of 1-{MAX_STATE} characters")
    if not isinstance(questions, dict) or not 0 < len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"questions must name 1-{MAX_QUESTIONS} questions")
    for name, q in questions.items():
        if not isinstance(q, dict) or q.get("type") != "noul" or not isinstance(q.get("instructions"), str) \
                or len(q["instructions"]) > MAX_INSTRUCTIONS:
            raise ValueError(f"question {str(name)[:40]!r} must be a yes/no (noul) question with short instructions")
    return state, questions


def make_handler(decider):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):                # never log request lines
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self, path):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": "loopback only"}); return False
            if self.headers.get("Origin") is not None or self.headers.get("Sec-Fetch-Site") not in (None, "none"):
                self._send(403, {"error": "browser requests are not accepted; use the app"}); return False
            if path == "/health":
                return True
            auth = self.headers.get("Authorization") or ""
            if not hmac.compare_digest(auth.encode(), ("Bearer " + decider.token).encode()):
                self._send(401, {"error": "missing or wrong token"}); return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self._allowed(path):
                return
            if path == "/health":
                return self._send(200, {"ok": True, "service": "decide", "model": MODEL, "loaded": decider.model is not None,
                                        "error": decider.error})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if not self._allowed(path):
                return
            if path != "/decide":
                return self._send(404, {"error": "not found"})
            size = int(self.headers.get("content-length") or 0)
            if size > 32_000:
                return self._send(413, {"error": "request too large"})
            try:
                state, questions = _valid(json.loads(self.rfile.read(size) or b"{}"))
            except (ValueError, AttributeError) as e:
                return self._send(400, {"error": str(e)[:200]})
            if decider.model is None:
                return self._send(503, {"error": decider.error or "the model is still loading"})
            try:
                return self._send(200, decider.decide(state, questions))
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})

    return Handler


def serve(decider, port):
    """A threaded loopback server (returned unstarted, so the check suite can run it on a free port)."""
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(decider))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local Studio decision service (d1-2B)")
    ap.add_argument("--port", type=int, default=8085)
    args = ap.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        sys.exit(f"{TOKEN_ENV} is not set; the app starts this service with a per-install token")
    decider = Decider(token)
    server = serve(decider, args.port)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    threading.Thread(target=decider.warm, daemon=True).start()
    print(f"decide service on 127.0.0.1:{args.port} ({MODEL})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
