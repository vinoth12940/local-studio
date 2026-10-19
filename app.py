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
from studio.api import storage as api_storage
from studio.api import system as _api_system
from studio.api import terminal as api_terminal
from studio.api import undo as api_undo
from studio.api import updates as api_updates
from studio.api import workspace as api_workspace
from studio.config import GALLERY
from studio.security.local_only import LocalOnly

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
    "studio.services.updates",
    "studio.api.updates",
    "studio.api.storage",
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


for _router in (api_models.router, api_engines.router, api_attachments.router, api_images.router, api_search.router, api_terminal.router, api_agent.router, api_memory.router, api_undo.router, api_processes.router, api_mcp.router, api_workspace.router, api_chat.router, api_chats.router, _api_system.router, api_updates.router, api_storage.router):
    app.include_router(_router)
