"""Routes: voice (behind the voice.enabled flag; every route except status answers 404 while it is off)."""

import asyncio
import threading

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from studio.services import voice as services_voice

router = APIRouter()
MAX_UPLOAD = 16_000_000                               # same limit as voice.py's /stt


def _need_voice():
    if not services_voice.voice_available():
        raise HTTPException(404, "Voice is off (flag voice.enabled).")


@router.get("/api/voice/status")
def voice_status():
    out = {"available": services_voice.voice_available(), "installed": services_voice.voice_deps_installed(),
           "running": False, "setup": {k: services_voice.voice_job.get(k) for k in ("status", "step", "error")}}
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
