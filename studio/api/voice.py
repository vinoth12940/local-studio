"""Routes: voice (behind the voice.enabled flag; every route except status answers 404 while it is off)."""

import asyncio
import threading

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from studio.services import voice as services_voice
from studio.services import decide as services_decide

router = APIRouter()
MAX_UPLOAD = 16_000_000                               # same limit as voice.py's /stt


def _need_voice():
    if not services_voice.voice_available():
        raise HTTPException(404, "Voice is off (flag voice.enabled).")


@router.get("/api/voice/status")
def voice_status():
    out = {"available": services_voice.voice_available(), "installed": services_voice.voice_deps_installed(),
           "running": False, "setup": {k: services_voice.voice_job.get(k) for k in ("status", "step", "error")}}
    out["decide"] = services_decide.mode()
    if out["available"] and services_voice._voice_running():
        try:
            out.update(services_voice.voice_request("GET", "/status", timeout=3).json(), running=True)
        except HTTPException:
            pass
    return out


@router.post("/api/voice/enable")
def voice_enable():
    _need_voice()
    if services_voice.voice_job.get("status") != "running":
        services_voice.voice_job.clear()
        services_voice.voice_job.update(status="running", step="Starting")
        threading.Thread(target=services_voice._enable_voice, daemon=True).start()
    return {k: services_voice.voice_job.get(k) for k in ("status", "step", "error")}


@router.post("/api/voice/disable")
def voice_disable():
    services_voice._stop_voice()                       # allowed even with the flag off: always lets the user stop it
    return {"ok": True}


@router.get("/api/voice/voices")
def voice_voices():
    _need_voice()
    return services_voice.voice_request("GET", "/voices").json()


@router.post("/api/voice/say")
def voice_say(body: dict = Body(...)):
    """Speak text with a Phonon voice: {"text", "voice", "speed"} -> WAV (for previews; the conversation loop plays
    through the echo-cancelling engine instead)."""
    _need_voice()
    req = {"text": str(body.get("text") or "")[:2000], "voice": str(body.get("voice") or ""), "speed": body.get("speed")}
    r = services_voice.voice_request("POST", "/tts", json=req)
    return Response(r.content, media_type="audio/wav", headers={"cache-control": "no-store"})


@router.post("/api/voice/transcribe")
async def voice_transcribe(request: Request):
    """A 16-bit PCM WAV -> {"text"}, on this Mac (Parakeet)."""
    _need_voice()
    if request.headers.get("content-type", "").split(";")[0].strip().lower() not in ("audio/wav", "audio/x-wav"):
        raise HTTPException(415, "Send a 16-bit PCM WAV.")
    data = await request.body()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Recording too long.")
    r = await asyncio.to_thread(services_voice.voice_request, "POST", "/stt", content=data, headers={"content-type": "audio/wav"})
    return r.json()


# ---- the live conversation: the UI drives the echo-cancelling engine in voice.py through these (same token, same gate)

@router.post("/api/voice/mic/open")
def voice_mic_open():
    _need_voice()
    opened = services_voice.voice_request("POST", "/mic/open", timeout=20).json()
    if services_decide.flag("voice.decide"):           # Start: d1 comes up (again, if it died or the flag was turned on later)
        threading.Thread(target=services_decide.start_decide, daemon=True).start()
    return opened


@router.post("/api/voice/mic/close")
def voice_mic_close():
    services_decide.stop_decide()                     # End: d1 unloads with the conversation (~1.8 GB back)
    if not services_voice._voice_running():          # nothing to close; allowed with the flag off so the mic can always be released
        return {"ok": True}
    return services_voice.voice_request("POST", "/mic/close", timeout=20).json()


@router.post("/api/voice/speak")
def voice_speak(body: dict = Body(...)):
    """Speak one sentence through the echo-cancelling engine; a "played" event follows on /api/voice/events."""
    _need_voice()
    req = {"text": str(body.get("text") or "")[:2000], "voice": str(body.get("voice") or ""), "speed": body.get("speed"),
           "id": int(body.get("id") or 0)}
    return services_voice.voice_request("POST", "/speak", json=req).json()


@router.post("/api/voice/stop")
def voice_stop():
    _need_voice()
    return services_voice.voice_request("POST", "/stop", timeout=5).json()


@router.post("/api/voice/pause")
def voice_pause():
    _need_voice()
    return services_voice.voice_request("POST", "/pause", timeout=5).json()


@router.post("/api/voice/resume")
def voice_resume():
    _need_voice()
    return services_voice.voice_request("POST", "/resume", timeout=5).json()


@router.get("/api/voice/events")
async def voice_events(request: Request):
    """The microphone and playback events of voice.py, relayed as server-sent events while the page listens."""
    _need_voice()
    if not await asyncio.to_thread(services_voice._voice_running):
        raise HTTPException(503, "Voice isn't running. Turn it on first.")
    return StreamingResponse(services_voice.voice_event_stream(request), media_type="text/event-stream",
                             headers={"cache-control": "no-store"})
