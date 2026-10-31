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
os.environ["LOCAL_STUDIO_DECIDE_PORT"] = "9"               # nothing listens: never the running app's d1
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
        assert _req(port, "POST", "/speak", json.dumps({"text": "Hi.", "voice": "Edwin"}), {"content-type": "application/json"})[0] == 409
        assert _req(port, "GET", "/status")[2] and json.loads(_req(port, "GET", "/status")[2])["mic_open"] is False
        assert _req(port, "POST", "/mic/open")[0] == 200 and len(FakeIO.instances) == 1
        assert _req(port, "POST", "/mic/open")[0] == 200 and len(FakeIO.instances) == 1, "opening twice reuses the engine"
        assert _req(port, "POST", "/play?id=5&rate=24000", pcm, {"content-type": "application/octet-stream"})[0] == 200
        assert _req(port, "POST", "/play?id=6&rate=24000", b"abc", {"content-type": "application/octet-stream"})[0] == 400
        assert _req(port, "POST", "/play?id=7&rate=1", pcm, {"content-type": "application/octet-stream"})[0] == 400
        s, _, body = _req(port, "POST", "/speak", json.dumps({"text": "Hi.", "voice": "Edwin", "id": 8}), {"content-type": "application/json"})
        assert s == 200 and json.loads(body) == {"ok": True, "id": 8, "seconds": 0.1}, body
        assert _req(port, "POST", "/speak", json.dumps({"text": "Hi.", "voice": "nobody", "id": 9}), {"content-type": "application/json"})[0] == 400
        for p in ("/pause", "/resume", "/stop"):
            assert _req(port, "POST", p)[0] == 200
        io = FakeIO.instances[0]
        assert [c[0] for c in io.calls] == ["play", "play", "pause", "resume", "stop"], io.calls
        assert io.calls[1][3] == 8, "/speak plays under the id the page waits for"
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


@check
def speech_recognition_runs_on_one_thread():
    # MLX streams belong to the thread that made them: the real model, loaded on the warm-up thread and called from a
    # request thread, failed every transcription with "There is no Stream(cpu, 1) in current thread".
    import threading
    from studio.voice.stt import Parakeet
    stt, seen = Parakeet(), set()

    def fake(pcm, rate):
        seen.add(threading.get_ident())
        return "ok"
    stt._transcribe = fake
    workers = [threading.Thread(target=stt.transcribe, args=(np.zeros(1600, dtype=np.float32), 16000)) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    stt.warm()
    assert len(seen) == 1 and threading.get_ident() not in seen, seen


@check
def a_silent_microphone_is_restarted():
    made = []

    class StalledIO(FakeIO):
        def __init__(self, fanout):
            super().__init__(fanout)
            self.last_tap = time.monotonic() - (60 if not made else 0)   # the first engine never delivers audio
            made.append(self)
    service = Service(TOKEN, tts=FakeTTS(), stt=FakeSTT(), io_factory=StalledIO)
    q = service.fanout.subscribe()
    service.open_mic()
    for _ in range(60):
        if service.restarts:
            break
        time.sleep(0.1)
    assert service.restarts == 1 and len(made) == 2 and service.io is made[1], "a stalled engine must be replaced"
    assert made[0].calls[-1] == ("close",), "the stalled engine is closed first"
    assert {"t": "mic_restart", "restarts": 1} in [q.get_nowait() for _ in range(q.qsize())], "the page is told"
    service.close_mic()


@check
def a_stopped_engine_is_restarted_in_place():
    class StoppingIO(FakeIO):
        def __init__(self, fanout):
            super().__init__(fanout)
            self.is_running, self.last_tap, self.kicks = False, time.monotonic(), 0   # stops itself right after start
        def running(self):
            return self.is_running
        def kick(self):
            self.kicks += 1; self.is_running = True; self.last_tap = time.monotonic()
            return True
    FakeIO.instances.clear()
    service = Service(TOKEN, tts=FakeTTS(), stt=FakeSTT(), io_factory=StoppingIO)
    io = service.open_mic()
    for _ in range(40):
        if io.kicks:
            break
        time.sleep(0.1)
    assert io.kicks == 1 and service.io is io and len(FakeIO.instances) == 1, "restart the same engine; do not rebuild it"
    assert service.restarts == 1
    service.close_mic()


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
        assert f'pkill -f "{argv[2]}"' in stop_sh.replace("8084", str(config.VOICE_PORT)).replace("8085", str(config.DECIDE_PORT)), \
            f"stop.sh lacks {argv[2]}"                 # _stop_voice also stops the d1 check that runs beside it
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
        for method, path in (("post", "/api/voice/enable"), ("get", "/api/voice/voices"), ("post", "/api/voice/say"),
                             ("post", "/api/voice/mic/open"), ("post", "/api/voice/speak"), ("post", "/api/voice/stop"),
                             ("post", "/api/voice/pause"), ("post", "/api/voice/resume"), ("get", "/api/voice/events")):
            r = getattr(c, method)(path, json={"text": "hi", "voice": "Edwin"}) if method == "post" else c.get(path)
            assert r.status_code == 404, (path, r.status_code)
        assert c.post("/api/voice/transcribe", content=b"x", headers={"content-type": "audio/wav"}).status_code == 404
        assert c.post("/api/voice/disable").status_code == 200, "the user can always stop voice"
        real_running = services_voice._voice_running
        services_voice._voice_running = lambda: False
        try:
            assert c.post("/api/voice/mic/close").json() == {"ok": True}, "the microphone can always be released"
        finally:
            services_voice._voice_running = real_running
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
        assert c.post("/api/voice/speak", json={"text": "y" * 3000, "voice": "Edwin", "id": "12"}).status_code == 200
        method, path, kw = calls[-1]
        assert path == "/speak" and len(kw["json"]["text"]) == 2000 and kw["json"]["id"] == 12
        for p in ("/api/voice/mic/open", "/api/voice/stop", "/api/voice/pause", "/api/voice/resume"):
            assert c.post(p).status_code == 200 and calls[-1][1] == p.replace("/api/voice", "")
        real_running = services_voice._voice_running
        services_voice._voice_running = lambda: False
        try:
            assert c.get("/api/voice/events").status_code == 503, "no event stream while the service is down"
        finally:
            services_voice._voice_running = real_running
        assert c.post("/api/voice/say", json={"text": "hi"}, headers={"Origin": "https://evil.example"}).status_code == 403, \
            "LocalOnly guards the voice routes like every other route"
    finally:
        services_voice.voice_available, services_voice.voice_request = real


@check
def chat_voice_mode_never_reaches_the_engine():
    from studio.services import harness
    body = {"messages": [], "voice": True, "voice_style": "  a calm   butler\n" + "x" * 900, "enable_thinking": True}
    note = harness.apply_voice_mode(body)
    assert "voice" not in body and "voice_style" not in body, "the engine must never see the voice fields"
    assert body["enable_thinking"] is False, "spoken turns do not think aloud"
    assert note.startswith(harness.VOICE_NOTE) and "a calm butler" in note and len(note) < len(harness.VOICE_NOTE) + 700
    assert "never refuse" in note, "a character never limits what it helps with"
    typed = {"messages": [], "enable_thinking": True}
    assert harness.apply_voice_mode(typed) is None and typed == {"messages": [], "enable_thinking": True}
    src = (REPO / "studio/api/chat.py").read_text()
    assert src.index("harness.apply_voice_mode(body)") < src.index("build_messages(body.get(\"messages\""), \
        "voice fields are removed before the request is built"
    # Qwen's chat template (and others) reject a system message anywhere but first: the note joins the leading one.
    assert 'harness.with_voice_note(body["messages"], voice_note)' in src
    assert '{"role": "system", "content": voice_note}' not in src
    chat = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Hi"}]
    out = harness.with_voice_note(chat, note)
    assert [m["role"] for m in out] == ["system", "user"] and out[0]["content"].startswith("You are helpful.")
    assert note in out[0]["content"] and chat[0]["content"] == "You are helpful.", "merged, and the input is not mutated"
    parts = harness.with_voice_note([{"role": "system", "content": [{"type": "text", "text": "a"}]}, chat[1]], note)
    assert [m["role"] for m in parts] == ["system", "user"] and parts[0]["content"][-1]["text"] == note
    bare = harness.with_voice_note([chat[1]], note)
    assert [m["role"] for m in bare] == ["system", "user"] and bare[0]["content"] == note
    # The short reminder follows the latest user message only (local models ignore a rule far up a long system message).
    convo = [chat[0], {"role": "user", "content": "first"}, {"role": "assistant", "content": "a"}, {"role": "user", "content": "LFM3?"}]
    out = harness.with_voice_note(convo, note)
    assert out[3]["content"].startswith("LFM3?") and out[3]["content"].endswith(harness.VOICE_TURN_REMINDER)
    assert out[1]["content"] == "first" and convo[3]["content"] == "LFM3?", "only the latest turn, and the input is not mutated"
    multi = harness.with_voice_note([chat[0], {"role": "user", "content": [{"type": "text", "text": "q"}]}], note)
    assert multi[1]["content"][-1]["text"] == harness.VOICE_TURN_REMINDER
    assert "example prompts" in harness.VOICE_NOTE


RULES_CAUGHT, RULES_FALSE_ALARM = 30, 0                # the rules' measured counts on the fixture (28 Oct); keep them as floors


@check
def decide_rules_route_the_labeled_questions():
    from studio.services import decide
    q = json.loads((Path(__file__).resolve().parent / "fixtures/route_questions.json").read_text())
    live = lambda t: decide.prefetch_call(decide.rules(t), t, web_fast=True) is not None
    caught = sum(live(t) for t in q["search"]); false_alarm = sum(live(t) for t in q["direct"])
    assert caught >= RULES_CAUGHT and false_alarm <= RULES_FALSE_ALARM, (caught, false_alarm)
    assert not any(live(t) for t in q["personal"] + q["follow_up"])
    assert [t for t in q["small_talk_and_commands"] if live(t)] == [], "small talk and commands never go to a public search"
    assert all(decide.prefetch_call(decide.rules(t), t, web_fast=True, files=True)[0] == "search_files" for t in q["personal"])
    assert decide.clean_query("um, what's the, uh, bitcoin price right now") == "what's the bitcoin price right now"
    assert decide.clean_query("Do you know who won the match?") == "Do you know who won the match?", "only a filler 'you know' goes"
    assert decide.clean_query("It's, like, really cold, you know, outside") == "It's really cold outside"
    assert decide.clean_query("I'd like the latest news") == "I'd like the latest news"
    live = dict(live_facts=0.99, my_files=0.0, small_talk=0.0, standalone=0.9, news=0.0)
    for said in ("What time is it right now?", "What's the date today?", "What day is it?"):
        assert decide.prefetch_call(live, said, web_fast=True) == ("get_current_datetime", {}), said   # the clock, never the web
    assert decide.prefetch_call(live, "What time is it in Tokyo?", web_fast=True) is None, "another city's time: the model asks the clock for that zone"
    assert decide.prefetch_call(decide.rules("What's the latest cricket news?"), "What's the latest cricket news?", web_fast=True)[0] == "news_search"
    assert decide.prefetch_call(decide.rules("What's the latest cricket news?"), "What's the latest cricket news?", web_fast=False)[0] == "web_search"


@check
def decide_service_warms_before_it_says_loaded_and_rejects_bad_sizes():
    import decide as service
    release, calls = threading.Event(), []
    class SlowD1:
        def system_one(self, state, questions):
            calls.append(state)
            if len(calls) == 1:
                release.wait(5)                       # the warm-up pass
            return {"answers": {k: {"type": "noul", "noul": 0.5} for k in questions}}
    decider = service.Decider(TOKEN, load=lambda repo: SlowD1())
    warming = threading.Thread(target=decider.warm, daemon=True)
    warming.start()
    time.sleep(0.2)
    assert decider.model is None, "not loaded (and not taking requests) until the warm-up pass is done"
    release.set(); warming.join(5)
    assert decider.model is not None and len(calls) == 1
    srv = service.serve(decider, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest("POST", "/decide", skip_host=True)
        for k, v in (("Host", f"127.0.0.1:{port}"), ("Authorization", "Bearer " + TOKEN), ("Content-Length", "-1")):
            conn.putheader(k, v)
        conn.endheaders()
        assert conn.getresponse().status == 400, "a negative size is a bad request, never a read to the end"
        conn.close()
    finally:
        srv.shutdown(); srv.server_close()


@check
def decide_personal_questions_never_reach_the_web():
    from studio.services import decide
    missed = dict(live_facts=0.9, my_files=0.1, small_talk=0.0, standalone=0.9, news=0.0)      # d1 missed the personal cue
    for said in ("When does my passport expire?", "What does my green card say?", "Is my car insurance still valid?",
                 "What's the date on my passport?", "What's my name?"):
        assert decide.prefetch_call(missed, said, web_fast=True, files=False) is None, said
        pick = decide.prefetch_call(missed, said, web_fast=True, files=True)
        assert pick is None or pick[0] == "search_files", (said, pick)
    veto = dict(live_facts=0.1, my_files=0.0, small_talk=0.9, standalone=0.2, news=0.0, source="d1")   # d1 read the greeting before it as context
    latest = "Would you be able to get me some details from Latest match New Zealand and England."
    assert decide.prefetch_call(veto, latest, web_fast=True)[0] == "web_search", "the rules are a floor under d1 for live facts"
    assert decide.prefetch_call(veto, "Hello! How is it going today?", web_fast=True) is None, "a greeting still never searches"
    # A greeting in front of a question is not small talk (found on 1.7.8: "Hey, um what is the Frisco weather looks like today?"
    # never searched because "Hey" matched the greeting rule), but a greeting alone, or in front of small talk, still is.
    frisco = "Hey, um what is the Frisco weather looks like today?"
    assert decide.prefetch_call(decide.rules(frisco), frisco, web_fast=True) == ("web_search", {"query": "what is the Frisco weather looks like today?"})
    assert decide.prefetch_call(veto, "Hey, what's the weather in Frisco?", web_fast=True)[0] == "web_search", "d1 reading the greeting as small talk"
    assert decide.strip_lead("Hey Jarvis, what is the weather?") == "what is the weather?" and decide.strip_lead("Hey, Frisco, what's the weather?") == "Frisco, what's the weather?"
    assert decide.strip_lead("Hey!") == "Hey!" and decide.strip_lead("Hello, hi") == "Hello, hi" and decide.strip_lead("And what about tomorrow?") == "And what about tomorrow?"
    for chat in ("Hey, how are you?", "Hello, how is it going today?", "Hey Jarvis, how are you doing today?", "Hi, can you open Safari?", "Good morning!"):
        assert decide.prefetch_call(decide.rules(chat), chat, web_fast=True) is None, chat
    # "Use web search for that" after a live question searches the question, not the request; after small talk it searches nothing.
    ask, before = "Why don't you use a web search to get that information?", "Hey, um what is the Frisco weather looks like today?"
    assert decide.prefetch_call(decide.rules(ask), ask, web_fast=True) is None, "alone it is not a question about the world"
    assert decide.prefetch_call(decide.rules(ask), ask, web_fast=True, previous=before) == ("web_search", {"query": "what is the Frisco weather looks like today?"})
    assert decide.prefetch_call(decide.rules(ask), ask, web_fast=True, previous="Hello there, how are you?") is None
    # Found on 1.7.9 in a real Voice session: "Sorry, my bad" vetoed the search as if it were about the user, and "Yes, please, go
    # ahead." searched for those very words (pages about the phrase) instead of the question it confirmed.
    cricket = "Um can you check who won between India and South Africa? Sorry, my bad, I mean South Africa versus Australia."
    assert decide.prefetch_call(decide.rules(cricket), cricket, web_fast=True)[0] == "web_search", "an idiom with 'my' is not about the user"
    for statement in ("Oh my god, I'm so tired today.", "Sorry, my bad, I was thinking about something else today."):
        assert decide.prefetch_call(decide.rules(statement), statement, web_fast=True) is None, statement
    assert decide.prefetch_call(decide.rules("What is my name?"), "What is my name?", web_fast=True) is None, "a real 'my' still never goes to the web"
    asked = "Yeah, so it happened today morning, I think South Africa, day five. So can you check it? It's a test game."
    for yes in ("Yes, please, go ahead.", "Sure, check it.", "Okay, go on."):
        pick = decide.prefetch_call(decide.rules(yes), yes, web_fast=True, previous=asked)
        assert pick and pick[0] == "web_search" and "South Africa" in pick[1]["query"] and "go ahead" not in pick[1]["query"], (yes, pick)
        assert decide.prefetch_call(decide.rules(yes), yes, web_fast=True, previous="Hello there, how are you?") is None, "a yes to small talk searches nothing"
        assert decide.prefetch_call(decide.rules(yes), yes, web_fast=True) is None, "a yes with nothing before it searches nothing"
    chatty = dict(live_facts=0.9, my_files=0.0, small_talk=0.9, standalone=0.9, news=0.0)
    assert decide.prefetch_call(chatty, "Hello! How is it going today?", web_fast=True) is None, "d1's small talk never searches"
    assert "LFM Open License v1.0" in __import__("studio.storage.flags", fromlist=["FLAGS"]).FLAGS["voice.decide"][1]
    assert "LFM Open License v1.0" in (REPO / "frontend/voice/voice.mjs").read_text(), "the Voice view names d1's license"


@check
def d1_follows_the_voice_session():
    from fastapi.testclient import TestClient
    from studio.services import decide
    c = TestClient(app.app, base_url="http://127.0.0.1:7860")
    class R:
        def json(self): return {"ok": True}
    events = []
    saved = (services_voice.voice_available, services_voice.voice_request, services_voice._voice_running, decide.start_decide,
             decide.stop_decide, decide.flag)
    services_voice.voice_available = lambda: True
    services_voice.voice_request = lambda *a, **k: R()
    services_voice._voice_running = lambda: True
    decide.start_decide = lambda: events.append("start")
    decide.stop_decide = lambda: events.append("stop")
    decide.flag = lambda name: name == "voice.decide"
    try:
        assert c.post("/api/voice/mic/open").status_code == 200
        for _ in range(50):
            if events: break
            time.sleep(0.02)
        assert events == ["start"], "Start brings d1 up (again, if it died or the flag was turned on later)"
        assert c.post("/api/voice/mic/close").status_code == 200 and events == ["start", "stop"], "End unloads d1"
    finally:
        (services_voice.voice_available, services_voice.voice_request, services_voice._voice_running, decide.start_decide,
         decide.stop_decide, decide.flag) = saved


@check
def decide_reads_d1_answers_and_falls_back_to_rules():
    import httpx
    from studio.services import decide
    answers = {"answers": {k: {"type": "noul", "noul": v} for k, v in
               {"live_facts": 0.93, "my_files": 0.02, "small_talk": 0.01, "standalone": 0.97, "news": 0.1}.items()}}
    seen = []
    def post(url, json=None, headers=None, timeout=None):
        seen.append((url, json, headers, timeout)); return httpx.Response(200, json=answers, request=httpx.Request("POST", url))
    real = decide.httpx.post
    decide.httpx.post = post
    try:
        plan = decide.preflight("Bitcoin price now?", "Hi")
        assert plan["source"] == "d1" and plan["live_facts"] == 0.93 and seen[0][0].endswith("/decide")
        assert seen[0][1]["state"] == "Earlier: Hi\nLast message: Bitcoin price now?" and seen[0][3] == 0.25
        assert seen[0][2]["Authorization"].startswith("Bearer ") and set(seen[0][1]["questions"]) == set(decide.QUESTIONS)
        def slow(*a, **k): raise httpx.ReadTimeout("warming up")
        decide.httpx.post = slow
        assert decide.preflight("Bitcoin price now?", None)["source"] == "rules"
        real_token = decide._decide_token
        def unreadable(): raise PermissionError("data/decide.token")
        decide._decide_token = unreadable
        try:
            assert decide.preflight("Bitcoin price now?", None)["source"] == "rules", "a token problem is a fallback, not a dead turn"
        finally:
            decide._decide_token = real_token
    finally:
        decide.httpx.post = real


@check
def voice_starts_even_when_d1_cannot():
    from studio.services import decide
    saved = (services_voice.voice_deps_installed, services_voice._start_voice, services_voice._voice_running, decide.start_decide, services_voice.flag)
    def broken(): raise FileNotFoundError("hf CLI is missing")
    services_voice.voice_deps_installed = lambda: True
    services_voice._start_voice = lambda: None
    services_voice._voice_running = lambda: True
    services_voice.flag = lambda name: name in ("voice.enabled", "voice.decide")
    decide.start_decide = broken
    try:
        services_voice._enable_voice()
        assert services_voice.voice_job["status"] == "done", services_voice.voice_job
    finally:
        (services_voice.voice_deps_installed, services_voice._start_voice, services_voice._voice_running, decide.start_decide,
         services_voice.flag) = saved
        services_voice.voice_job.clear(); services_voice.voice_job.update(status="idle")


@check
def d1_starts_once_and_only_after_its_whole_download():
    """Found in 1.7.1: two starts raced; the second saw config.json (the download's first small file), launched d1 before
    model.safetensors arrived, and d1 sat failed ("IncompleteSnapshotError") while the status said "loading"."""
    import tempfile
    from studio.services import decide, models
    hub, real_hub = Path(tempfile.mkdtemp()), models.HF_HUB
    snap = hub / ("models--" + studio_config_decide_model().replace("/", "--")) / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    models.HF_HUB = hub
    saved = (decide.flag, decide.system_memory, decide._fetch_model, decide._spawn, decide._process, decide._health, decide.stop_decide)
    spawned, stopped, seen_while_fetching = [], [], []
    try:
        assert decide._model_present() is False, "config.json alone is not a downloaded model"
        (snap / "model.safetensors").write_text("w")
        (snap.parent.parent / "blobs").mkdir()
        (snap.parent.parent / "blobs" / "x.incomplete").write_text("")
        assert decide._model_present() is False, "a file still arriving means the download is not done"
        (snap.parent.parent / "blobs" / "x.incomplete").unlink()
        (snap / "model.safetensors").unlink()
        decide.flag = lambda name: name == "voice.decide"
        decide.system_memory = lambda: {"free_percent": 40}
        def fetch():
            seen_while_fetching.append(decide.mode())
            time.sleep(0.3)
            (snap / "model.safetensors").write_text("w")
            return True
        decide._fetch_model = fetch
        decide._spawn = lambda: spawned.append(1)
        decide._process = lambda: bool(spawned)
        decide._health = lambda: None
        decide.stop_decide = lambda: stopped.append(1)
        starts = [threading.Thread(target=decide.start_decide) for _ in range(2)]
        for t in starts: t.start()
        for t in starts: t.join(5)
        assert spawned == [1], "two starts at once launch d1 once, after the download"
        assert seen_while_fetching[0] == {"mode": "rules", "reason": "downloading d1-2B (1.35 GB)"}, seen_while_fetching
        decide._health = lambda: {"ok": True, "loaded": False, "error": "IncompleteSnapshotError: 1 file(s) are missing"}
        assert decide.mode() == {"mode": "rules", "reason": "d1 failed to load; it retries at the next Start"}
        decide.start_decide()
        assert stopped == [1] and spawned == [1, 1], "a d1 that failed to load is restarted at the next Start"
    finally:
        (decide.flag, decide.system_memory, decide._fetch_model, decide._spawn, decide._process, decide._health, decide.stop_decide) = saved
        models.HF_HUB = real_hub


def studio_config_decide_model():
    import studio.config as c
    return c.DECIDE_MODEL


@check
def decide_launcher_and_stop_patterns_agree_and_never_look_like_the_vision_engine():
    from studio.services import decide
    import studio.config as c
    cmd = " ".join(decide.decide_command())
    assert re.search(decide.DECIDE_PATTERN, cmd) and not re.search(c.ENGINES["vision"]["pattern"], cmd)
    assert decide.decide_command()[1] == "decide.py", "a relative name with cwd=ROOT: the bundled path contains a space"
    stop = (REPO / "stop.sh").read_text()
    assert f'pkill -f "{decide.DECIDE_PATTERN}"' in stop.replace("8085", str(c.DECIDE_PORT))
    assert re.search(r"for name in [^;]*\bdecide\b[^;]*; do", stop), "stop.sh must also stop d1 by its pid file"
    build = (REPO / "macos/build.sh").read_text()
    assert re.search(r"^for f in .* decide\.py .*; do", build, re.M), "decide.py must be copied into the app bundle"


@check
def decide_waits_for_memory_and_reports_rules_when_down():
    from studio.services import decide
    saved = (decide.system_memory, decide._spawn, decide.flag, decide._model_present)
    spawned = []
    decide.flag = lambda name: name == "voice.decide"
    decide._spawn = lambda: spawned.append(1)
    try:
        decide.system_memory = lambda: {"total_bytes": 48 << 30, "used_bytes": 44 << 30, "free_percent": 12}
        out = decide.start_decide()
        assert out == {"mode": "rules", "reason": "memory: 12% free"} and not spawned
        decide.system_memory = lambda: {"total_bytes": 48 << 30, "used_bytes": 20 << 30, "free_percent": 40}
        decide._model_present = lambda: True
        decide.start_decide()
        assert spawned == [1]
        assert decide.mode()["mode"] == "rules" and "not running" in decide.mode()["reason"]   # nothing answers on the test port
        real_process = decide._process
        decide._process = lambda: True
        try:
            assert decide.mode() == {"mode": "rules", "reason": "d1 is loading"}, "spawned but not answering yet"
        finally:
            decide._process = real_process
        decide.flag = lambda name: False
        assert decide.mode() == {"mode": "off", "reason": ""}
    finally:
        (decide.system_memory, decide._spawn, decide.flag, decide._model_present) = saved


@check
def decide_service_answers_only_the_backend():
    import decide as service
    class FakeD1:
        def system_one(self, state, questions):
            return {"answers": {k: {"type": "noul", "noul": 0.9 if "latest" in state else 0.1} for k in questions}, "usage": {}}
    try:
        service.Decider("")
        raise AssertionError("a decision service without a token started")
    except ValueError:
        pass
    decider = service.Decider(TOKEN, load=lambda repo: FakeD1())
    srv = service.serve(decider, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    body = json.dumps({"state": "What is the latest news?", "questions": {"live_facts": {"type": "noul", "instructions": "Live?"}}})
    try:
        status, _, data = _req(port, "GET", "/health", token=False)
        assert status == 200 and json.loads(data)["loaded"] is False, "not loaded until warm() has run"
        assert _req(port, "POST", "/decide", body=body)[0] == 503
        decider.warm()
        assert json.loads(_req(port, "GET", "/health", token=False)[2])["loaded"] is True
        status, _, data = _req(port, "POST", "/decide", body=body)
        assert status == 200 and json.loads(data)["answers"]["live_facts"]["noul"] == 0.9
        assert _req(port, "POST", "/decide", body=body, token=False)[0] == 401
        assert _req(port, "POST", "/decide", body=body, headers={"Origin": "https://evil.example"})[0] == 403
        assert _req(port, "POST", "/decide", body=body, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert _req(port, "POST", "/decide", body=body, headers={"Host": "evil.example"})[0] == 403, "DNS rebinding"
        assert _req(port, "POST", "/decide", body=json.dumps({"state": "x" * 5000, "questions": {}}))[0] == 400
        assert _req(port, "POST", "/decide", body="not json")[0] == 400
    finally:
        srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   voice checks OK")
