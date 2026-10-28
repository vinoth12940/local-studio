"""Routes: engines.

Moved from app.py in the refactor's P1 split.
"""

import asyncio
import json
import os
import threading

import httpx
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from studio import config
from studio.services import engines
from studio.services import models
from studio.config import AGENT_BUSY_MSG, agent_lock, ENGINES
from studio.services.engines import _ensure_quiet, engine_lock
from studio.services.models import chat_model
from studio.storage.settings import load_settings, save_settings

router = APIRouter()


CONTEXT_PRESETS = (100000, 131072, 196608, 262144)


def _ram_gb():
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30)
    except (ValueError, OSError, AttributeError):
        return None


def _memory_estimate(m, window):
    """A rough upper bound (GB) for the weights plus a full key-value cache at `window` tokens, or None when unknown.

    Only the layers that keep a KV cache count (hybrid models mark the rest in `layer_types`); the cache is 8-bit for the MLX
    engine's lm path and 16-bit otherwise. It is an advisory figure, never a limit: real use is usually lower."""
    cfg = models.snapshot_file(m["repo"], "config.json") if m.get("repo") else None
    try:
        data = json.loads(cfg.read_text()) if cfg else {}
    except (OSError, ValueError):
        return None
    text = data.get("text_config") if isinstance(data.get("text_config"), dict) else data
    layers, heads = text.get("num_hidden_layers"), text.get("num_key_value_heads") or text.get("num_attention_heads")
    hidden, attn = text.get("hidden_size"), text.get("num_attention_heads")
    if not all(type(v) is int and v > 0 for v in (layers, heads, hidden, attn)):
        return None
    kinds = text.get("layer_types")
    if isinstance(kinds, list) and kinds:
        layers = sum(1 for k in kinds if "full" in str(k))
    head_dim = text.get("head_dim") if type(text.get("head_dim")) is int else hidden // attn
    kv_bytes = 2 * layers * heads * head_dim * window * (1 if engines._uses_mlx_lm(m["repo"]) else 2)
    return round((models._repo_bytes(m["repo"]) + kv_bytes) / 2**30, 1)


def _context_payload():
    state = engines.engine_state()
    running = state.get("mode") is not None
    m = chat_model()
    effective, native = models.effective_context(m), models.native_context(m)
    started = state.get("context_window", config.LEGACY_CONTEXT) if running else None
    ram = _ram_gb()
    notes = ["Image chats use the vision engine. Its window is set by the model, but prompts are still budgeted to this value."]
    if native and native < config.CONTEXT:
        notes.append(f"{m['name']} supports at most {native:,} tokens, so it runs at {effective:,} even though the setting is "
                     f"{config.CONTEXT:,}. Larger windows apply to models that support them.")
    estimate = _memory_estimate(m, effective)
    if estimate is not None and ram is not None and estimate > 0.75 * ram:
        notes.append(f"{m['name']} with a {effective:,}-token window may need up to about {estimate} GB for weights and its key-value cache; "
                     f"this Mac has {ram} GB. If the engine fails to load or the Mac slows down, lower the window.")
    return {"value": config.CONTEXT, "effective": effective, "native_max": native, "default": config.DEFAULT_CONTEXT,
            "min": config.MIN_CONTEXT, "max": config.MAX_CONTEXT, "presets": list(CONTEXT_PRESETS), "ram_gb": ram,
            "memory_estimate_gb": estimate,
            "engine": {"mode": state.get("mode"), "model_id": state.get("model_id"), "started_with": started},
            "restart_required": bool(running and state.get("model_id") == m["id"] and started != effective),
            "applies": "The engine restarts with the new window automatically on your next message (a model reload). Nothing is interrupted now.",
            "notes": notes}


@router.get("/api/context-window")
def context_window_get():
    return _context_payload()


@router.post("/api/context-window")
def context_window_set(body: dict = Body(default={})):
    """Set the maximum context window in tokens (null resets to the default). Takes effect when the engine next starts."""
    if set(body) - {"value"} or "value" not in body:
        raise HTTPException(400, "Send {\"value\": <tokens>} (or null to reset).")
    value = body["value"]
    if value is None:
        value = config.DEFAULT_CONTEXT
    if not config.valid_context_window(value):
        raise HTTPException(400, f"The context window must be a whole number of tokens between {config.MIN_CONTEXT:,} and {config.MAX_CONTEXT:,}.")
    settings = load_settings()
    if value == config.DEFAULT_CONTEXT:
        settings.pop("context_window", None)
    else:
        settings["context_window"] = value
    save_settings(settings)
    config.CONTEXT = value
    return _context_payload()


@router.post("/api/engine/start")
def engine_start():
    if agent_lock.locked():
        raise HTTPException(409, AGENT_BUSY_MSG)
    m = chat_model()
    if not models.model_installed(m):
        raise HTTPException(409, f"Download {m['name']} first (Model library).")
    mode = "gguf" if m.get("backend") == "gguf" else "text"
    threading.Thread(target=_ensure_quiet, args=(mode, m), daemon=True).start()
    return {"status": "starting", "mode": mode, "model_id": m["id"]}


@router.post("/api/engine/stop")
def engine_stop():
    if agent_lock.locked():
        raise HTTPException(409, AGENT_BUSY_MSG)
    with engine_lock:
        for mode in ENGINES:
            engines._stop_engine(mode)
    return {"status": "stopped"}


@router.get("/api/llm/{route:path}")
async def llm_get(route: str):
    st = await asyncio.to_thread(engines.engine_state)
    mode, m = st.get("mode"), chat_model(st.get("model_id"))
    if mode in ("vision", "gguf"):
        ready = await asyncio.to_thread(engines._engine_ready, mode)
        if route == "health":
            return {"status": "ok" if ready else "loading", "mode": mode, "target": m["repo"],
                    "model_id": m["id"], "model_name": m["name"], "drafter": m.get("drafter") if mode == "vision" else None,
                    "max_draft": None, "kv_bits": None, "context_window": st.get("context_window") or models.effective_context(m), "warnings": []}
        return JSONResponse({"engine": mode}, status_code=200 if ready else 503)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ENGINES['text']['url']}/{route}")
        j = r.json()
        if route == "health" and isinstance(j, dict):
            j.update(model_id=m["id"], model_name=m["name"])
        return JSONResponse(j, status_code=r.status_code)
    except (httpx.HTTPError, ValueError) as e:                 # ValueError: an answer that isn't JSON (an engine still starting, a proxy page)
        return JSONResponse({"status": "starting" if mode else "down", "model_id": m["id"], "model_name": m["name"],
                             "error": str(e)}, status_code=503)
