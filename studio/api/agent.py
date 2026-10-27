"""Routes: agent.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import asyncio
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from studio import config
from studio.services.agent import loop
from studio.services import models
from studio.config import agent_lock, CUA_MIN
from studio.infra.sse import _sse
from studio.infra.text import _clip
from studio.security.agent_gate import _TIER_NAMES, APP_TIERS_DEFAULT
from studio.services.agent.cua import _cua_perm, _perm_missing, agent_check_permissions, AGENT_PERM_MSG
from studio.services.agent.loop import (
    _agent_active, _AGENT_RID, _agent_runs, _agent_saved, _AGENT_SHOT, _cua_cache, _cua_grant_lock, _cua_quiet,
    AGENT_ACTIVE, AGENT_HEARTBEAT_S, AGENT_KEEP_RUNS, AgentRun, clip_agent_context, cua_status_cached, run_agent,
)
from studio.services.models import chat_models
from studio.storage.chats import _chat_path
from studio.storage.settings import load_settings, save_settings

router = APIRouter()


def _agent_sse(e):
    return f"id: {e.get('i')}\n" + _sse(e)


@router.get("/api/agent/status")
def agent_status():
    run = loop._agent_current
    s = load_settings()["agent"]
    return {"enabled": s["enabled"], "cua": cua_status_cached(), "running": run.id if run is not None and agent_lock.locked() else None,
            "settings": s, "tier_defaults": APP_TIERS_DEFAULT,
            "permissions": {k: _cua_perm.get(k) for k in ("accessibility", "screen_recording")}}   # from the cache only: never starts CuaDriver


@router.post("/api/agent/enable")
def agent_enable(on: bool = Body(..., embed=True)):
    s = load_settings()
    s["agent"]["enabled"] = bool(on)
    save_settings(s)
    run = loop._agent_current
    if not on and run is not None:                           # turning Agent off stops a task in progress
        with run.cond:
            run.stop = True
            run.cond.notify_all()
    return {"enabled": bool(on)}


@router.put("/api/agent/settings")
def agent_settings(body: dict = Body(...)):
    s = load_settings()
    a = dict(s["agent"])
    for k, lo, hi in (("max_steps", 1, 200), ("max_minutes", 1, 120)):
        if k in body:
            v = body[k]
            if type(v) is not int or not lo <= v <= hi:      # not bool, not float
                raise HTTPException(400, f"{k} must be a whole number from {lo} to {hi}.")
            a[k] = v
    if "tiers" in body:
        t = body["tiers"]
        if not isinstance(t, dict) or not all(isinstance(k, str) and k.strip() and v in _TIER_NAMES for k, v in t.items()):
            raise HTTPException(400, "tiers maps app names to read, ask or allowed.")
        a["tiers"] = {k.strip(): v for k, v in t.items()}
    s["agent"] = a
    save_settings(s)
    return a


@router.post("/api/agent/permissions")
def agent_permissions():
    """Only from the user's click in Settings: CuaDriver asks macOS for Accessibility and Screen Recording."""
    if not Path(config.CUA_DRIVER).exists():
        raise HTTPException(409, "Install CuaDriver first.")
    with _cua_grant_lock:                                    # one at a time: a second click while macOS is asking does nothing new
        if loop._cua_grant is not None and loop._cua_grant.is_alive():
            raise HTTPException(409, "Already asking macOS for permissions.")
        loop._cua_grant = threading.Thread(target=_cua_quiet, args=([config.CUA_DRIVER, "permissions", "grant"], 600), daemon=True)
        loop._cua_grant.start()
    return {"status": "started"}


@router.post("/api/agent/telemetry/disable")
def agent_telemetry_disable():
    if not Path(config.CUA_DRIVER).exists():
        raise HTTPException(409, "Install CuaDriver first.")
    try:
        r = subprocess.run([config.CUA_DRIVER, "telemetry", "disable"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, f"cua-driver failed: {_clip(e, 200)}")
    finally:
        _cua_cache.clear()
    if r.returncode != 0:
        raise HTTPException(500, _clip(r.stderr or r.stdout or "cua-driver failed", 200))
    return {"telemetry": False}


@router.post("/api/agent/run")
def agent_run(body: dict = Body(...)):
    if agent_lock.locked():
        raise HTTPException(409, "A task is already running. Stop it or wait for it to finish.")
    task, mid = body.get("task"), body.get("model_id")
    if not isinstance(task, str) or not task.strip():
        raise HTTPException(409, "Describe the task first.")
    context, chat_id = body.get("context"), body.get("chat_id")
    if context is not None and not isinstance(context, str):
        raise HTTPException(400, "context must be text.")
    if chat_id is not None:
        if not isinstance(chat_id, str):
            raise HTTPException(400, "bad id")
        _chat_path(chat_id)                                  # the chats routes' own id rule: 400 "bad id"
    if not load_settings()["agent"]["enabled"]:
        raise HTTPException(409, "Computer access is off. Enable it to continue this request here.")
    cua = cua_status_cached(refresh=True)                    # a run is started rarely: always look (and refresh the cache)
    if not cua["installed"]:
        raise HTTPException(409, "Install CuaDriver first (Settings → Computer access).")
    if not cua["ok_version"]:
        raise HTTPException(409, f"CuaDriver {cua['version'] or '(unknown version)'} is too old; update it to "
                                 f"{'.'.join(map(str, CUA_MIN))} or later.")
    if _perm_missing(_cua_perm):                             # known missing: one fresh look (granted in System Settings since?)
        _cua_perm.clear()
        try:
            agent_check_permissions(loop._agent_client())
        except Exception:
            pass
        if _perm_missing(_cua_perm):
            raise HTTPException(409, AGENT_PERM_MSG)
    m = next((x for x in chat_models() if x["id"] == mid), None)
    if m is None:
        raise HTTPException(409, "Choose a chat model.")
    if not models.model_installed(m):
        raise HTTPException(409, f"Download {m['name']} first (Model library).")
    if not agent_lock.acquire(blocking=False):
        raise HTTPException(409, "A task is already running. Stop it or wait for it to finish.")
    try:
        run = AgentRun(task.strip()[:4000], m["id"], context=clip_agent_context(context or ""), chat_id=chat_id)
        for old in [r for r in _agent_runs.values() if r.status not in AGENT_ACTIVE][:-AGENT_KEEP_RUNS or None]:
            _agent_runs.pop(old.id, None)
        _agent_runs[run.id] = run
        loop._agent_current = run
        run.save()
        run.thread = threading.Thread(target=run_agent, args=(run, True), daemon=True, name=f"agent-{run.id}")
        run.thread.start()
    except BaseException:
        loop._agent_current = None
        agent_lock.release()
        raise
    return {"run_id": run.id}


@router.get("/api/agent/runs/{rid}/events")
async def agent_events(rid: str, after: int = 0):
    """SSE: the run's events with i >= after (inclusive: a client reconnects with its last i + 1 and drops any i it already has),
    then live ones until `end`. Each event carries `id: <i>`. Finished runs replay from memory or disk."""
    after = max(0, after)
    run = _agent_runs.get(rid) if _AGENT_RID.match(rid) else None
    headers = {"Cache-Control": "no-cache"}
    if run is None:
        d = await asyncio.to_thread(_agent_saved, rid)
        evs = [e for e in d.get("events") or [] if isinstance(e, dict)]
        if not any(e.get("type") == "end" for e in evs):     # a run cut off before it could say so
            evs.append({"i": len(evs), "type": "end", "status": d.get("status") or "interrupted"})

        async def stored():
            for e in evs:
                if e.get("i", 0) >= after:
                    yield _agent_sse(e)
        return StreamingResponse(stored(), media_type="text/event-stream", headers=headers)

    async def live():
        i, beat = after, time.monotonic()
        while True:
            with run.cond:
                evs = run.events[i:]
                ended = bool(run.events) and run.events[-1]["type"] == "end"
            for e in evs:
                yield _agent_sse(e)
            if evs:
                i, beat = evs[-1]["i"] + 1, time.monotonic()
            if ended:
                return
            if time.monotonic() - beat >= AGENT_HEARTBEAT_S:
                yield ": ping\n\n"
                beat = time.monotonic()
            await asyncio.sleep(0.2)
    return StreamingResponse(live(), media_type="text/event-stream", headers=headers)


@router.post("/api/agent/runs/{rid}/reply")
def agent_reply(rid: str, body: dict = Body(...)):
    """An answer to the approval or question of step `n` (the body must name it): {n, decision: allow|deny|stop} or {n, text}."""
    run = _agent_active(rid)
    d, text, n = body.get("decision"), body.get("text"), body.get("n")
    with run.cond:
        p = run.pending
        if not p:
            raise HTTPException(409, "Nothing is waiting for an answer.")
        if type(n) is not int or n != p["n"]:                # a stale or duplicated answer never lands on another step
            raise HTTPException(409, "That answer is for another step.")
        if d == "stop":
            run.stop = True
        elif run.status == "paused" or run.takeover:
            raise HTTPException(409, "Resume the task first.")
        elif run.reply is not None:
            raise HTTPException(409, "Already answered.")
        elif p["kind"] == "approval" and d in ("allow", "deny"):
            run.reply = d
        elif p["kind"] == "question" and isinstance(text, str) and text.strip():
            run.reply = text.strip()[:2000]
        else:
            raise HTTPException(409, "That answer doesn't fit what the agent asked.")
        run.cond.notify_all()
    return {"ok": True}


@router.post("/api/agent/runs/{rid}/stop")
def agent_stop(rid: str):
    run = _agent_active(rid)
    with run.cond:                                           # under cond: the run cannot reach `end` in between, so the note comes before it
        if not run.stop and run.status in AGENT_ACTIVE:
            run.emit({"type": "note", "text": "Stopping after the current step…"})
        run.stop = True
        run.cond.notify_all()
    return {"ok": True}


@router.post("/api/agent/runs/{rid}/takeover")
def agent_takeover(rid: str):
    run = _agent_active(rid)
    with run.cond:
        if run.status != "paused" and not run.takeover:     # a second press while pausing or paused does nothing (no re-pause after Resume)
            run.takeover = True
            run.cond.notify_all()
    return {"ok": True}


@router.post("/api/agent/runs/{rid}/resume")
def agent_resume(rid: str):
    run = _agent_active(rid)
    with run.cond:
        if run.status != "paused" and not run.takeover:
            raise HTTPException(409, "The task isn't paused.")
        run.resume = True
        run.cond.notify_all()
    return {"ok": True}


@router.get("/api/agent/runs")
def agent_runs():
    out = {}
    for p in config.AGENT_RUNS.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and _AGENT_RID.match(str(d.get("id") or "")):
            out[d["id"]] = {k: d.get(k) for k in ("id", "task", "status", "started", "chat_id")}
    for rid, run in list(_agent_runs.items()):
        if (config.AGENT_RUNS / f"{rid}.json").exists():
            out[rid] = {"id": rid, "task": run.task, "status": run.status, "started": run.started, "chat_id": run.chat_id}
    return sorted(out.values(), key=lambda r: r.get("started") or 0, reverse=True)


@router.get("/api/agent/runs/{rid}")
def agent_run_get(rid: str):
    return _agent_saved(rid)


@router.get("/api/agent/runs/{rid}/shots/{name}")
def agent_shot(rid: str, name: str):
    if not _AGENT_RID.match(rid) or not _AGENT_SHOT.match(name):
        raise HTTPException(404)
    folder = (config.AGENT_RUNS / rid).resolve()
    p = (folder / name).resolve()
    if p.parent != folder or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg" if name.endswith(".jpg") else "image/png")


@router.delete("/api/agent/runs/{rid}")
def agent_run_delete(rid: str):
    if not _AGENT_RID.match(rid):
        raise HTTPException(404)
    run = _agent_runs.get(rid)
    if run is not None and (run.status in AGENT_ACTIVE or (run.thread is not None and run.thread.is_alive())):
        raise HTTPException(409, "Stop the task first.")
    p = config.AGENT_RUNS / f"{rid}.json"
    if run is None and not p.exists():
        raise HTTPException(404)
    p.unlink(missing_ok=True)
    shutil.rmtree(config.AGENT_RUNS / rid, ignore_errors=True)
    _agent_runs.pop(rid, None)
    return {"deleted": rid}


@router.post("/api/agent/profile/reset")
def agent_profile_reset():
    """Delete the agent's isolated browser profile (cookies, sign-ins). Holds agent_lock so no run starts meanwhile."""
    if not agent_lock.acquire(blocking=False):
        raise HTTPException(409, "Stop the running task first.")
    try:
        if loop._agent_profile_in_use():                          # its Chrome is still closing (or another Local Studio uses it)
            raise HTTPException(409, "The agent's browser is still open. Wait a moment and try again.")
        if loop.AGENT_PROFILE.is_symlink():
            loop.AGENT_PROFILE.unlink()
        elif loop.AGENT_PROFILE.exists():
            shutil.rmtree(loop.AGENT_PROFILE)
    finally:
        agent_lock.release()
    return {"reset": True}
