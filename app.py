"""Local Studio: a local web app for on-device chat and image generation on Apple Silicon.

Chat runs on one of three engines (only one at a time, to keep memory free):
  text   — mlx-dspark: MLX models with DFlash/DSpark speculative decoding (text only)
  vision — mlx-vlm server: the same MLX weights when a chat contains images
  gguf   — llama.cpp's llama-server: GGUF models (vision when an mmproj file is present)
Image jobs run the mflux CLI one at a time. Also manages models, storage, updates and uninstall.
"""

import ast
import asyncio
import atexit
import base64
import contextvars
import difflib
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import os
import pty
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import textwrap
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, unquote_plus, urlsplit, urljoin, urlencode, parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import importlib

from studio.api import agent as api_agent
from studio.api import attachments as api_attachments
from studio.api import chat as api_chat
from studio.api import chats as api_chats
from studio.api import engines as api_engines
from studio.api import images as api_images
from studio.api import mcp as api_mcp
from studio.api import memory as api_memory
from studio.api import models as api_models
from studio.api import processes as api_processes
from studio.api import search as api_search
from studio.api import system as _api_system
from studio.api import terminal as api_terminal
from studio.api import undo as api_undo
from studio.api import workspace as api_workspace
from studio.config import (
    APP_SUPPORT, BREW, CHATS, DSPARK_PY, GALLERY, HOME, LLAMA_SERVER, LOGS, ROOT, STATE, UPLOADS, UV,
)
from studio.infra.system import dir_size
from studio.security.local_only import LocalOnly
from studio.services.agent.loop import _forget_chat_context

# ---- studio/ split (refactor P1, docs/refactor-progress.json): code moved out of this file module by module.
_MOVED = (
    "studio.config",
    "studio.infra.sse",
    "studio.infra.system",
    "studio.infra.text",
    "studio.infra.edits",
    "studio.infra.harness_args",
    "studio.infra.workspace",
    "studio.infra.processes",
    "studio.infra.mcp_client",
    "studio.security.fences",
    "studio.storage.settings",
    "studio.storage.memory",
    "studio.storage.backups",
    "studio.storage.chats",
    "studio.security.local_only",
    "studio.services.models",
    "studio.services.engines",
    "studio.api.models",
    "studio.api.engines",
    "studio.services.attachments",
    "studio.api.attachments",
    "studio.services.images",
    "studio.api.images",
    "studio.services.search",
    "studio.api.search",
    "studio.services.terminal",
    "studio.api.terminal",
    "studio.security.agent_gate",
    "studio.services.web",
    "studio.services.agent.cua",
    "studio.services.agent.views",
    "studio.services.mcp_custom",
    "studio.security.harness_gate",
    "studio.services.agent.loop",
    "studio.api.agent",
    "studio.services.harness",
    "studio.api.memory",
    "studio.api.undo",
    "studio.api.processes",
    "studio.api.mcp",
    "studio.api.workspace",
    "studio.api.chat",
    "studio.api.chats",
    "studio.api.system",
)                                                               # dependency order: owners first
for _m in _MOVED:
    importlib.import_module(_m)


def _owner_module(name):
    """The studio module that defines `name` (the first in dependency order that has it)."""
    for m in _MOVED:
        mod = sys.modules[m]
        if name in vars(mod):
            return mod
    return None


def __getattr__(name):
    """The check suites read moved names as app.X: answer live from the owning module, so a rebind there is seen."""
    mod = _owner_module(name)
    if mod is None:
        raise AttributeError(f"module 'app' has no attribute {name!r}")
    return vars(mod)[name]


APP_VERSION = "1.5.0"


app = FastAPI()
app.mount("/gallery", StaticFiles(directory=GALLERY), name="gallery")


app.add_middleware(LocalOnly)


# ---------------------------------------------------------------- versions & updates

update_job: dict = {"status": "idle"}


def _versions():
    v = {"app": APP_VERSION}
    try:
        out = subprocess.run([UV, "tool", "list"], capture_output=True, text=True, timeout=20).stdout
        for name in ("mlx-dspark", "mflux"):
            m = re.search(rf"^{re.escape(name)} v(\S+)", out, re.M)
            v[name] = m.group(1) if m else None
    except Exception:
        pass
    try:
        out = subprocess.run([LLAMA_SERVER, "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"version: (\S+)", out.stdout + out.stderr)
        v["llama.cpp"] = m.group(1) if m else None
    except Exception:
        v["llama.cpp"] = None
    try:
        out = subprocess.run([str(DSPARK_PY), "-c", "import importlib.metadata as m; print(m.version('mlx-vlm'), m.version('mlx'))"],
                             capture_output=True, text=True, timeout=20).stdout.split()
        v["mlx-vlm"], v["mlx"] = out[0], out[1]
    except Exception:
        pass
    return v


@app.get("/api/versions")
async def versions():
    return {**(await asyncio.to_thread(_versions)), "update": {k: v for k, v in update_job.items() if k != "proc"}}


def _run_update():
    log = open(LOGS / "update.log", "w")
    steps = [[UV, "tool", "upgrade", "mlx-dspark"], [UV, "tool", "upgrade", "mflux"]]
    if Path(BREW).exists() and not LLAMA_SERVER.startswith(str(APP_SUPPORT)):
        steps.append([BREW, "upgrade", "llama.cpp"] if Path(LLAMA_SERVER).exists() else [BREW, "install", "llama.cpp"])
    else:                                             # no Homebrew: refresh the app-managed build
        steps.append(["/bin/zsh", str(ROOT / "setup.sh"), "--llama"])
    before = _versions()
    for cmd in steps:
        update_job["step"] = " ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd))
        log.write(f"$ {update_job['step']}\n")
        log.flush()
        rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        if rc != 0 and "upgrade" in cmd and cmd[0] == BREW:
            continue                                  # brew upgrade exits 0 when already current
    after = _versions()
    changed = {k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k)}
    update_job.update(status="done", finished=time.time(), changed=changed, step=None)


@app.post("/api/versions/update")
def update_engines():
    if update_job.get("status") == "running":
        return update_job
    update_job.clear()
    update_job.update(status="running", started=time.time())
    threading.Thread(target=_run_update, daemon=True).start()
    return update_job

# ---------------------------------------------------------------- storage & uninstall


@app.get("/api/storage")
def storage():
    engine_cache = HOME / ".cache/mlx_dspark"
    return {
        "gallery": {"bytes": dir_size(GALLERY), "count": len(list(GALLERY.glob("*.png")))},
        "uploads": {"bytes": dir_size(UPLOADS), "count": len(list(UPLOADS.glob("*.json")))},
        "chats": {"bytes": dir_size(CHATS), "count": len(list(CHATS.glob("*.json")))},
        "logs": {"bytes": dir_size(LOGS), "count": len(list(LOGS.glob("*.log")))},
        "engine_cache": {"bytes": dir_size(engine_cache) if engine_cache.exists() else 0},
        "disk_free_bytes": shutil.disk_usage(HOME).free,
        "root": str(STATE),
        "bundled": STATE != ROOT,
    }


@app.post("/api/storage/clear")
def clear_storage(what: str = Body(..., embed=True)):
    if what == "gallery":
        for p in GALLERY.iterdir():
            p.unlink(missing_ok=True)
    elif what == "uploads":
        for p in UPLOADS.iterdir():
            p.unlink(missing_ok=True)
    elif what == "chats":
        for p in CHATS.glob("*.json"):
            p.unlink(missing_ok=True)
        _forget_chat_context(None)
    elif what == "logs":
        for p in LOGS.glob("*.log"):
            p.write_text("")                    # truncate: running servers still hold them open
    elif what == "engine_cache":
        shutil.rmtree(HOME / ".cache/mlx_dspark", ignore_errors=True)
    else:
        raise HTTPException(400, "unknown target")
    return {"ok": True}


@app.post("/api/uninstall")
def uninstall(remove_app: bool = Body(False, embed=True)):
    log = HOME / "local-studio-uninstall.log"
    args = ["/bin/zsh", str(ROOT / "uninstall.sh"), "--yes"] + (["--remove-app"] if remove_app else [])
    subprocess.Popen(args, stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    return {"ok": True, "log": str(log)}


for _router in (api_models.router, api_engines.router, api_attachments.router, api_images.router, api_search.router, api_terminal.router, api_agent.router, api_memory.router, api_undo.router, api_processes.router, api_mcp.router, api_workspace.router, api_chat.router, api_chats.router, _api_system.router):
    app.include_router(_router)
