"""Build guard for the voice service (voice.py, studio/voice) and its wiring into the app. Offline: fake models and a fake
microphone, so it needs neither the voice dependencies nor a Mac microphone. Run by macos/build.sh, or by hand:

    uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt --with numpy python macos/check_voice.py [check_name ...]
"""
import http.client
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lsvoice"))
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")      # never touch real user data
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from studio.voice import audio, config as vconfig  # noqa: E402
from studio.voice.server import Service, serve  # noqa: E402

CHECKS = []
TOKEN = "t" * 40


def check(fn):
    CHECKS.append(fn)
    return fn


class FakeTTS:
    loaded = True
    def voices(self):
        return [{"name": "Edwin", "description": "calm"}]
    def synth(self, text, voice, speed=0.0):
        if voice != "Edwin":
            raise ValueError("unknown voice")
        if not str(text or "").strip() or len(text) > vconfig.MAX_TEXT:
            raise ValueError("text must be 1-2000 characters")
        return np.zeros(2400, dtype=np.float32), 24000


class FakeSTT:
    loaded = True
    def __init__(self):
        self.got = None
    def transcribe(self, pcm, rate=16000):
        self.got = (len(pcm), rate)
        return "hello there"
    def warm(self):
        pass


class FakeIO:
    instances = []
    def __init__(self, fanout):
        self.fanout, self.in_rate, self.calls = fanout, 48000, []
        FakeIO.instances.append(self)
    def play(self, pcm, rate, item_id):
        self.calls.append(("play", len(pcm), rate, item_id))
        self.fanout.publish({"t": "played", "id": item_id, "stopped": False})
    def stop(self): self.calls.append(("stop",))
    def pause(self): self.calls.append(("pause",))
    def resume(self): self.calls.append(("resume",))
    def close(self): self.calls.append(("close",))


def _server():
    service = Service(TOKEN, tts=FakeTTS(), stt=FakeSTT(), io_factory=FakeIO)
    srv = serve(service, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return service, srv, srv.server_address[1]


def _req(port, method, path, body=None, headers=None, token=True):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": f"127.0.0.1:{port}", **({"Authorization": "Bearer " + TOKEN} if token else {}), **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, r.getheader("content-type") or "", data


@check
def audio_helpers_round_trip_and_reject_bad_input():
    x = np.sin(np.linspace(0, 200, 48000)).astype(np.float32) * 0.5
    y = audio.resample(x, 48000, 16000)
    assert len(y) == 16000 and y.dtype == np.float32
    pcm, rate = audio.from_wav(audio.to_wav(y, 16000))
    assert rate == 16000 and len(pcm) == 16000 and float(np.max(np.abs(pcm - y))) < 1e-3
    for bad in (b"", b"RIFF1234WAVEjunk", b"not audio at all"):
        try:
            audio.from_wav(bad)
            raise AssertionError("garbage WAV accepted")
        except ValueError:
            pass
    import io as _io, wave
    b = _io.BytesIO()
    with wave.open(b, "wb") as w:       # 8-bit audio is refused, not misread
        w.setnchannels(1); w.setsampwidth(1); w.setframerate(16000); w.writeframes(b"\x80" * 1600)
    try:
        audio.from_wav(b.getvalue()); raise AssertionError("8-bit WAV accepted")
    except ValueError:
        pass


@check
def chunks_carry_over_and_slow_streams_drop_audio():
    ch = audio.Chunker(512)
    assert [len(c) for c in ch(np.zeros(700, np.float32))] == [512] and len(ch.carry) == 188
    assert [len(c) for c in ch(np.zeros(400, np.float32))] == [512] and len(ch.carry) == 76
    ev = audio.mic_event(np.full(512, 0.5, np.float32))
    assert ev["t"] == "mic" and abs(ev["rms"] - 0.5) < 1e-3 and len(ev["pcm"]) > 1000
    f = audio.Fanout(size=3)
    q = f.subscribe()
    for i in range(10):
        f.publish({"i": i})
    assert q.qsize() == 3, "a stalled stream must stay bounded"
    f.unsubscribe(q)
    assert f.count() == 0


@check
def service_needs_its_token():
    try:
        Service("", tts=FakeTTS(), stt=FakeSTT(), io_factory=FakeIO)
        raise AssertionError("a service without a token started")
    except ValueError:
        pass
    from studio.voice import server as vserver
    os.environ.pop(vconfig.TOKEN_ENV, None)
    try:
        vserver.main(["--port", "0"])
        raise AssertionError("voice.py started without LOCAL_STUDIO_VOICE_TOKEN")
    except SystemExit:
        pass


@check
def service_refuses_browsers_rebinding_and_missing_tokens():
    service, srv, port = _server()
    try:
        assert _req(port, "GET", "/health", token=False)[0] == 200, "/health is the launcher's liveness probe"
        assert _req(port, "GET", "/status", token=False)[0] == 401
        assert _req(port, "GET", "/status", headers={"Authorization": "Bearer wrong"}, token=False)[0] == 401
        assert _req(port, "GET", "/status")[0] == 200
        assert _req(port, "GET", "/status", headers={"Origin": "http://127.0.0.1:7860"})[0] == 403, "browsers never talk to voice.py"
        assert _req(port, "GET", "/status", headers={"Origin": "https://evil.example"})[0] == 403
        assert _req(port, "GET", "/status", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert _req(port, "GET", "/status", headers={"Host": "evil.example"})[0] == 403, "DNS rebinding"
        assert _req(port, "POST", "/mic/open", headers={"Origin": "https://evil.example"})[0] == 403
        assert not FakeIO.instances, "a refused request must not open the microphone"
    finally:
        srv.shutdown(); srv.server_close()


@check
def service_routes_speak_listen_and_play():
    FakeIO.instances.clear()
    service, srv, port = _server()
    try:
        s, ct, wav = _req(port, "POST", "/tts", json.dumps({"text": "Hi.", "voice": "Edwin"}), {"content-type": "application/json"})
        assert s == 200 and ct == "audio/wav" and wav[:4] == b"RIFF"
        assert _req(port, "POST", "/tts", json.dumps({"text": "Hi.", "voice": "../../x"}), {"content-type": "application/json"})[0] == 400
        assert _req(port, "POST", "/tts", "x=1", {"content-type": "text/plain"})[0] == 415, "no-CORS simple posts are refused"
        assert _req(port, "POST", "/tts", "x" * 20_001, {"content-type": "application/json"})[0] == 413
        clip = audio.to_wav(np.zeros(8000, np.float32), 16000)
        s, _, body = _req(port, "POST", "/stt", clip, {"content-type": "audio/wav"})
        assert s == 200 and json.loads(body)["text"] == "hello there" and service.stt.got == (8000, 16000)
        assert _req(port, "POST", "/stt", b"junk", {"content-type": "audio/wav"})[0] == 400
        assert _req(port, "POST", "/stt", clip, {"content-type": "application/octet-stream"})[0] == 415
        pcm = np.zeros(2400, np.float32).astype("<f4").tobytes()
        assert _req(port, "POST", "/play?id=1&rate=24000", pcm, {"content-type": "application/octet-stream"})[0] == 409, \
            "speech must play through the echo-cancelling engine, which opens with the microphone"
        assert _req(port, "GET", "/status")[2] and json.loads(_req(port, "GET", "/status")[2])["mic_open"] is False
        assert _req(port, "POST", "/mic/open")[0] == 200 and len(FakeIO.instances) == 1
        assert _req(port, "POST", "/mic/open")[0] == 200 and len(FakeIO.instances) == 1, "opening twice reuses the engine"
        assert _req(port, "POST", "/play?id=5&rate=24000", pcm, {"content-type": "application/octet-stream"})[0] == 200
        assert _req(port, "POST", "/play?id=6&rate=24000", b"abc", {"content-type": "application/octet-stream"})[0] == 400
        assert _req(port, "POST", "/play?id=7&rate=1", pcm, {"content-type": "application/octet-stream"})[0] == 400
        for p in ("/pause", "/resume", "/stop"):
            assert _req(port, "POST", p)[0] == 200
        io = FakeIO.instances[0]
        assert [c[0] for c in io.calls] == ["play", "pause", "resume", "stop"]
        # the event stream carries microphone chunks and playback completions
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/events", headers={"Host": f"127.0.0.1:{port}", "Authorization": "Bearer " + TOKEN})
        r = c.getresponse()
        assert r.status == 200 and r.getheader("content-type") == "text/event-stream"
        for _ in range(50):
            if service.fanout.count():
                break
            time.sleep(0.02)
        service.fanout.publish(audio.mic_event(np.zeros(512, np.float32)))
        service.fanout.publish({"t": "played", "id": 9, "stopped": False})
        got = []
        while len(got) < 2:
            line = r.fp.readline().decode().strip()
            if line.startswith("data: "):
                got.append(json.loads(line[6:]))
        assert got[0]["t"] == "mic" and got[1] == {"t": "played", "id": 9, "stopped": False}
        c.close()
        assert _req(port, "POST", "/mic/close")[0] == 200 and io.calls[-1] == ("close",)
        assert json.loads(_req(port, "GET", "/status")[2])["mic_open"] is False
    finally:
        srv.shutdown(); srv.server_close()


# ---------------------------------------------------------------- the app side
import app  # noqa: E402  (after LOCAL_STUDIO_HOME is set)
from studio import config  # noqa: E402
from studio.services import voice as services_voice  # noqa: E402


@check
def backend_never_imports_the_voice_package():
    import studio.main as main
    assert not any(m.startswith("studio.voice") for m in main.MODULES)
    heavy = [m for m in sys.modules if m in ("ptts", "parakeet_mlx", "AVFoundation", "mlx", "mlx.core")]
    assert not heavy, f"the UI backend must not load voice dependencies: {heavy}"


@check
def token_file_is_private_and_stable():
    config.VOICE_TOKEN.unlink(missing_ok=True)
    t1 = services_voice._voice_token()
    mode = stat.S_IMODE(config.VOICE_TOKEN.stat().st_mode)
    assert len(t1) >= 32 and mode == 0o600, oct(mode)
    assert services_voice._voice_token() == t1


@check
def launcher_and_stop_patterns_agree_with_stop_sh():
    cmd = services_voice._voice_command()
    assert cmd[-4:] == ["python", "voice.py", "--port", str(config.VOICE_PORT)], cmd
    assert "voice-requirements.txt" in cmd and not any("/" in a and a.endswith(".py") for a in cmd), "relative names: the bundle path has a space"
    started, ran = [], []
    real_popen, real_run = services_voice.subprocess.Popen, services_voice.subprocess.run
    real_running, real_process = services_voice._voice_running, services_voice._voice_process

    class P:
        pid = 4242
    try:
        services_voice._voice_running = lambda: False
        services_voice._voice_process = lambda: False
        services_voice.subprocess.Popen = lambda *a, **k: (started.append((a, k)), P())[1]
        services_voice.subprocess.run = lambda *a, **k: (ran.append(a[0]), type("R", (), {"returncode": 1})())[1]
        services_voice._start_voice()
        services_voice._stop_voice()
    finally:
        services_voice.subprocess.Popen, services_voice.subprocess.run = real_popen, real_run
        services_voice._voice_running, services_voice._voice_process = real_running, real_process
    (args, kw), = started
    assert kw["cwd"] == config.ROOT and kw["start_new_session"] is True
    assert kw["env"]["LOCAL_STUDIO_VOICE_TOKEN"] == services_voice._voice_token()
    assert kw["env"]["PYTHONDONTWRITEBYTECODE"] == "1", "no bytecode may be written into the signed bundle"
    stop_sh = (REPO / "stop.sh").read_text()
    for argv in ran:
        assert argv[0] == "pkill"
        assert f'pkill -f "{argv[2]}"' in stop_sh.replace("8084", str(config.VOICE_PORT)), f"stop.sh lacks {argv[2]}"
    assert re.search(r"for name in [^;]*\bvoice\b[^;]*; do", stop_sh), "stop.sh must also stop voice by its pid file"


@check
def setup_build_and_requirements_wire_voice():
    setup = (REPO / "setup.sh").read_text()
    assert "--voice) install_voice" in setup and "voice-deps.ok" in setup and "voice-requirements.txt" in setup
    build = (REPO / "macos/build.sh").read_text()
    copy = re.search(r"for f in ([^;]+); do", build).group(1).split()
    assert {"voice.py", "voice-requirements.txt"} <= set(copy), "voice files must be copied into the bundle"
    assert "NSMicrophoneUsageDescription" in build, "the app cannot open the microphone without a usage string"
    assert '--entitlements "$HERE/LocalStudio.entitlements"' in build
    ent = (REPO / "macos/LocalStudio.entitlements").read_text()
    assert "com.apple.security.device.audio-input" in ent
    assert ent.count("<key>") == 1, "only the microphone entitlement is granted"
    assert "check_voice.py" in build
    reqs = [l.strip() for l in (REPO / "voice-requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert reqs and all(re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9.+-]+", r) for r in reqs), "exact pins only"
    names = {r.split("==")[0].lower() for r in reqs}
    assert {"ptts", "parakeet-mlx", "pyobjc-framework-avfoundation", "numpy"} <= names
    assert "phonon" not in names, "PyPI 'phonon' is an unrelated package; Gradium's runtime is ptts"


@check
def voice_routes_are_off_by_default():
    from fastapi.testclient import TestClient
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    real_stop = services_voice._stop_voice
    services_voice._stop_voice = lambda: None
    try:
        st = c.get("/api/voice/status").json()
        assert st["available"] is False and st["running"] is False
        for method, path in (("post", "/api/voice/enable"), ("get", "/api/voice/voices"), ("post", "/api/voice/say")):
            r = getattr(c, method)(path, json={"text": "hi", "voice": "Edwin"}) if method == "post" else c.get(path)
            assert r.status_code == 404, (path, r.status_code)
        assert c.post("/api/voice/transcribe", content=b"x", headers={"content-type": "audio/wav"}).status_code == 404
        assert c.post("/api/voice/disable").status_code == 200, "the user can always stop voice"
    finally:
        services_voice._stop_voice = real_stop


@check
def voice_routes_proxy_with_the_flag_on():
    from fastapi.testclient import TestClient
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    calls = []

    class R:
        def __init__(self, content=b"", js=None):
            self.content, self._js = content, js
        def json(self):
            return self._js

    def fake_request(method, path, timeout=60, **kw):
        calls.append((method, path, kw))
        if path == "/tts":
            return R(audio.to_wav(np.zeros(100, np.float32), 24000))
        if path == "/stt":
            return R(js={"text": "hello there"})
        if path == "/voices":
            return R(js={"voices": [{"name": "Edwin"}]})
        return R(js={})

    real = services_voice.voice_available, services_voice.voice_request
    services_voice.voice_available = lambda: True
    services_voice.voice_request = fake_request
    try:
        r = c.post("/api/voice/say", json={"text": "x" * 5000, "voice": "Edwin"})
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
        assert len(calls[-1][2]["json"]["text"]) == 2000, "text is capped before it reaches the service"
        assert c.get("/api/voice/voices").json()["voices"][0]["name"] == "Edwin"
        assert c.post("/api/voice/transcribe", content=b"x", headers={"content-type": "text/plain"}).status_code == 415
        wav = audio.to_wav(np.zeros(1600, np.float32), 16000)
        assert c.post("/api/voice/transcribe", content=wav, headers={"content-type": "audio/wav"}).json() == {"text": "hello there"}
        assert c.post("/api/voice/say", json={"text": "hi"}, headers={"Origin": "https://evil.example"}).status_code == 403, \
            "LocalOnly guards the voice routes like every other route"
    finally:
        services_voice.voice_available, services_voice.voice_request = real


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   voice checks OK")
