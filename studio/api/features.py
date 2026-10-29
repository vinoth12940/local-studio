"""Settings → Voice and web answers: the switches for the flags people turn on themselves.

Each is a flag in data/settings.json (studio/storage/flags.py), read again whenever the file changes, so a switch takes effect
on the next message or the next Voice start without a restart. Only the flags in SWITCHES can be set from the page; turning one
off stops what it runs.
"""

from fastapi import APIRouter, Body, HTTPException

from studio.services import decide as services_decide
from studio.services import voice as services_voice
from studio.storage.flags import FLAGS, flag
from studio.storage.settings import load_settings, save_settings

router = APIRouter()

SWITCHES = ("voice.enabled", "voice.decide", "harness.web_fast")


@router.get("/api/features")
def features():
    return {"features": [{"name": name, "on": bool(flag(name)), "about": FLAGS[name][1]} for name in SWITCHES]}


@router.post("/api/features")
def set_feature(name: str = Body(..., embed=True), on: object = Body(..., embed=True)):
    if name not in SWITCHES:
        raise HTTPException(400, "Only Voice, the live-facts check and faster web answers can be switched here")
    if not isinstance(on, bool):
        raise HTTPException(400, "on must be true or false")
    section, key = name.split(".", 1)
    settings = load_settings()
    settings[section] = {**(settings.get(section) or {}), key: on}
    save_settings(settings)
    if not on and name == "voice.enabled":
        services_voice._stop_voice()                  # also unloads d1
    elif not on and name == "voice.decide":
        services_decide.stop_decide()                 # d1's ~1.8 GB back now, not at the next End
    return {"name": name, "on": on}
