"""Local Studio search service: indexes the user's files with EmbeddingGemma 2 (MLX) and answers
plain-language queries. Runs as its own process on 127.0.0.1:8083, started by app.py.

Only the standard library and numpy are imported at module level. Everything heavy (mlx,
transformers, PIL, pypdf, fastapi) is imported inside the function that needs it, so the crawl,
store and ranking logic can be checked without a model (macos/check_search.py).
"""

import argparse
import fnmatch
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent                       # code (inside the .app when bundled)
STATE = Path(os.environ.get("LOCAL_STUDIO_HOME", ROOT))      # user data
HOME = Path.home()
SETTINGS = STATE / "data" / "settings.json"
INDEX_DIR = STATE / "data" / "search"
HF_HUB = Path(os.environ.get("HF_HUB_CACHE", HOME / ".cache/huggingface/hub"))
MODEL_REPO = "mlx-community/embeddinggemma-2-8bit"
DIM = 768
APP_URL = "http://127.0.0.1:7860"

# ---------------------------------------------------------------- config

ICLOUD = HOME / "Library/Mobile Documents/com~apple~CloudDocs"
DEFAULT_ROOTS = [HOME / d for d in ("Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music")] + [ICLOUD]


def _under(path, base):
    """True when `path` is `base` or inside it (string paths, no filesystem access)."""
    base = str(base).rstrip("/")
    return str(path) == base or str(path).startswith(base + "/")


def load_config(settings_path=None):
    """The `search` block of settings.json as the crawl needs it."""
    try:
        s = json.loads((settings_path or SETTINGS).read_text()).get("search") or {}
    except Exception:
        s = {}
    removed = [str(Path(p).expanduser()) for p in s.get("removed", [])]
    # Testing: LOCAL_STUDIO_SEARCH_ROOTS replaces the default folders, so a dev run never reads real ones.
    env = os.environ.get("LOCAL_STUDIO_SEARCH_ROOTS")
    defaults = env.split(":") if env else [str(r) for r in DEFAULT_ROOTS]
    roots = defaults + [str(Path(p).expanduser()) for p in s.get("folders", [])]
    roots = [r for r in dict.fromkeys(roots) if r not in removed]
    return {"roots": roots, "removed": removed, "paused": bool(s.get("paused")), "on_battery": bool(s.get("on_battery"))}


# ---------------------------------------------------------------- chunking

CHUNK_CHARS = 1200                # ~300 tokens
MAX_CHUNKS = 200                  # per file
_SENT = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text, limit=CHUNK_CHARS):
    """Pack paragraphs into chunks of at most `limit` characters. A paragraph longer than the limit
    is split at sentence ends, and a sentence longer than the limit is cut hard."""
    pieces = []                                           # (text, separator that joins it to the previous piece)
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            pieces.append((para, "\n\n"))
            continue
        sep = "\n\n"
        for sent in _SENT.split(para):
            while len(sent) > limit:
                pieces.append((sent[:limit], sep))
                sent, sep = sent[limit:], " "
            if sent:
                pieces.append((sent, sep))
                sep = " "
    chunks, cur = [], ""
    for piece, sep in pieces:
        if cur and len(cur) + len(sep) + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = cur + sep + piece if cur else piece
    if cur:
        chunks.append(cur)
    return chunks
