"""HTTP interface of the voice service (loopback only; every route but /health needs the per-launch token).

    GET  /health                 liveness, no token (used by the launcher)
    GET  /status                 what is loaded and whether the microphone is open
    GET  /voices                 Phonon's English voices
    POST /tts                    {"text", "voice", "speed"} -> audio/wav
    POST /stt                    audio/wav -> {"text"}
    POST /mic/open | /mic/close  the microphone is closed until asked for, and closed again on exit
    GET  /events                 SSE: {"t":"mic","rms","pcm"} every 32 ms while open, {"t":"played","id","stopped"}
    POST /play?id=&rate=         raw little-endian float32 mono PCM, played through the echo-cancelling engine
    POST /stop | /pause | /resume

Only the UI backend talks to this process: a browser request (any Origin or Sec-Fetch-Site) is refused, as is a
non-loopback Host (DNS rebinding) and anything without the token.
"""
import argparse
import hmac
import http.server
import json
import os
import queue
import signal
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import numpy as np

from studio.voice import config
from studio.voice.audio import Fanout, from_wav, to_wav


class Service:
    """The components behind the routes; the check suite passes fakes for the models and the microphone."""
    def __init__(self, token, tts=None, stt=None, io_factory=None):
        if not token:
            raise ValueError("the voice service needs a token")
        self.token = token
        if tts is None:
            from studio.voice.tts import Phonon
            tts = Phonon()
        if stt is None:
            from studio.voice.stt import Parakeet
            stt = Parakeet()
        if io_factory is None:
            from studio.voice.bridge import VoiceIO
            io_factory = VoiceIO
        self.tts, self.stt, self.io_factory = tts, stt, io_factory
        self.fanout, self.io, self.io_lock = Fanout(), None, threading.Lock()

    def open_mic(self):
        with self.io_lock:
            if self.io is None:
                self.io = self.io_factory(self.fanout)
            return self.io

    def close_mic(self):
        with self.io_lock:
            io, self.io = self.io, None
        if io is not None:
            io.close()

    def status(self):
        return {"ok": True, "mic_open": self.io is not None, "voice_processing": self.io is not None,
                "tts_loaded": bool(getattr(self.tts, "loaded", False)), "stt_loaded": bool(getattr(self.stt, "loaded", False)),
                "streams": self.fanout.count(), "stt_model": config.STT_MODEL, "tts_model": "gradium/phonon-31m-en"}


def make_handler(service):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):                # never log request lines (they could carry text)
            pass

        def _send(self, code, body=b"", ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self, path):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": "loopback only"}); return False
            if self.headers.get("Origin") is not None or self.headers.get("Sec-Fetch-Site") not in (None, "none"):
                self._send(403, {"error": "browser requests are not accepted; use the app"}); return False
            if path == "/health":
                return True
            auth = self.headers.get("Authorization") or ""
            if not hmac.compare_digest(auth.encode(), ("Bearer " + service.token).encode()):
                self._send(401, {"error": "missing or wrong token"}); return False
            return True

        def _body(self, limit):
            n = int(self.headers.get("content-length") or 0)
            if n > limit:
                raise OverflowError
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self._allowed(path):
                return
            if path == "/health":
                return self._send(200, {"ok": True, "service": "voice"})
            if path == "/status":
                return self._send(200, service.status())
            if path == "/voices":
                try:
                    return self._send(200, {"voices": service.tts.voices()})
                except Exception as e:
                    return self._send(503, {"error": f"voices unavailable: {str(e)[:160]}"})
            if path == "/events":
                return self._events()
            self._send(404, {"error": "not found"})

        def _events(self):
            q = service.fanout.subscribe()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-store")
            self.end_headers()
            try:
                while True:
                    try:
                        self.wfile.write(b"data: " + json.dumps(q.get(timeout=5)).encode() + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                service.fanout.unsubscribe(q)

        def do_POST(self):
            u = urlsplit(self.path)
            if not self._allowed(u.path):
                return
            ctype = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
            qs = parse_qs(u.query)
            try:
                if u.path == "/tts":
                    if ctype != "application/json":
                        return self._send(415, {"error": "send JSON"})
                    req = json.loads(self._body(20_000) or b"{}")
                    pcm, rate = service.tts.synth(req.get("text"), str(req.get("voice") or ""), req.get("speed"))
                    return self._send(200, to_wav(pcm, rate), "audio/wav")
                if u.path == "/stt":
                    if ctype not in ("audio/wav", "audio/x-wav"):
                        return self._send(415, {"error": "send a 16-bit PCM WAV"})
                    pcm, rate = from_wav(self._body(config.MAX_STT_BYTES))
                    return self._send(200, {"text": service.stt.transcribe(pcm, rate)})
                if u.path == "/mic/open":
                    io = service.open_mic()
                    return self._send(200, {"ok": True, "mic_rate_in": getattr(io, "in_rate", None), "mic_rate": config.MIC_RATE})
                if u.path == "/mic/close":
                    service.close_mic()
                    return self._send(200, {"ok": True})
                if u.path == "/play":
                    if ctype != "application/octet-stream":
                        return self._send(415, {"error": "send float32 PCM as application/octet-stream"})
                    rate, item = int(qs.get("rate", [config.OUT_RATE])[0]), int(qs.get("id", [0])[0])
                    data = self._body(config.MAX_PLAY_BYTES)
                    if not 8000 <= rate <= 96000 or len(data) % 4:
                        return self._send(400, {"error": "bad rate or PCM length"})
                    if service.io is None:
                        return self._send(409, {"error": "open the microphone first (speech plays through the same engine)"})
                    service.io.play(np.frombuffer(data, dtype="<f4").astype(np.float32), rate, item)
                    return self._send(200, {"ok": True, "id": item})
                if u.path in ("/stop", "/pause", "/resume"):
                    if service.io is not None:
                        getattr(service.io, u.path[1:])()
                    return self._send(200, {"ok": True})
            except OverflowError:
                return self._send(413, {"error": "request too large"})
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)[:200]})
            except Exception as e:                    # a model or audio failure: report it, keep serving
                return self._send(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})
            self._send(404, {"error": "not found"})

    return Handler


def serve(service, port):
    """A threaded loopback server (returned unstarted, so the check suite can run it on a free port)."""
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(service))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local Studio voice service")
    ap.add_argument("--port", type=int, default=config.PORT)
    args = ap.parse_args(argv)
    token = os.environ.get(config.TOKEN_ENV, "")
    if not token:
        sys.exit(f"{config.TOKEN_ENV} is not set; the app starts this service with a per-launch token")
    service = Service(token)
    server = serve(service, args.port)

    def shutdown(*_):                                # release the microphone on stop.sh / app quit
        service.close_mic()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def warm():                                      # load and warm the models now so the first reply is fast
        for job in (lambda: service.tts.voices(), service.stt.warm):
            try:
                job()
            except Exception as e:
                print(f"voice: warm-up failed: {e}", flush=True)
        print("voice: models ready", flush=True)

    threading.Thread(target=warm, daemon=True).start()
    print(f"voice service on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        service.close_mic()
        server.server_close()
