"""Paths, ports, the engine table and other constants (ROOT holds code, STATE holds user data).

Moved from app.py in the refactor's P1 split.
"""

import json
import os
import re
import shutil
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent                # code (inside the .app when bundled): app.py's folder
# The version lives in app.py, where macos/build.sh and release.sh read it with sed.
APP_VERSION = re.search(r'^APP_VERSION = "(.*)"$', (ROOT / "app.py").read_text(), re.M).group(1)
STATE = Path(os.environ.get("LOCAL_STUDIO_HOME", ROOT))      # user data: images, chats, logs
GALLERY = STATE / "gallery"
UPLOADS = STATE / "uploads"
LOGS = STATE / "logs"
DATA = STATE / "data"
CHATS = DATA / "chats"
SETTINGS = DATA / "settings.json"
for d in (GALLERY, UPLOADS, LOGS, CHATS):
    d.mkdir(parents=True, exist_ok=True)

HOME = Path.home()
BIN = HOME / ".local/bin"
UV = shutil.which("uv") or str(BIN / "uv")
HF_HUB = Path(os.environ.get("HF_HUB_CACHE", HOME / ".cache/huggingface/hub"))
HF_CLI = HOME / ".local/share/uv/tools/mlx-dspark/bin/hf"
DSPARK_PY = HOME / ".local/share/uv/tools/mlx-dspark/bin/python"   # its env also ships mlx-vlm
APP_SUPPORT = HOME / "Library/Application Support/Local Studio"
LLAMA_SERVER = next((p for p in (shutil.which("llama-server"), "/opt/homebrew/bin/llama-server",
                                 str(APP_SUPPORT / "llama.cpp/llama-server")) if p and Path(p).exists()),
                    "/opt/homebrew/bin/llama-server")
BREW = shutil.which("brew") or "/opt/homebrew/bin/brew"
DOCUMENT_READER = (ROOT.parent.parent / "MacOS" / "DocumentReader") if (ROOT.parent.parent / "MacOS" / "DocumentReader").exists() else ROOT / "build" / "DocumentReader"
# The model's context window in tokens: what the engine is started with, and what every prompt budget is measured against.
# Settings -> Context window changes it (data/settings.json "context_window"). Agent turns replay every tool result and the model's
# reasoning, so a smaller window runs out within a few rounds; 100k is the floor and 128k the default. A model with a smaller native
# limit is clamped to it (models.effective_context). LEGACY_CONTEXT is what engines started before the window was recorded used.
DEFAULT_CONTEXT, MIN_CONTEXT, MAX_CONTEXT, LEGACY_CONTEXT = 131072, 100000, 262144, 32768


def valid_context_window(value):
    return type(value) is int and MIN_CONTEXT <= value <= MAX_CONTEXT


def _configured_context():
    try:
        value = json.loads(SETTINGS.read_text()).get("context_window")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_CONTEXT
    return value if valid_context_window(value) else DEFAULT_CONTEXT


CONTEXT = _configured_context()
# mflux editing needs a complete checkpoint; the q4 export used here is text-to-image only.
EDIT_MODEL = os.environ.get("EDIT_MODEL")
# Testing: LOCAL_STUDIO_SEARCH_PORT gives a development server its own search service, so it can
# never reach or stop the one belonging to the app the user is running.
SEARCH_PORT = int(os.environ.get("LOCAL_STUDIO_SEARCH_PORT", 8083))
SEARCH_URL = f"http://127.0.0.1:{SEARCH_PORT}"                # search.py, see the search section
SEARCH_DIR = DATA / "search"                                 # its index
SEARCH_REQS = ROOT / "search-requirements.txt"
SEARCH_DEPS_OK = DATA / "search-deps.ok"                     # written by setup.sh --search
SEARCH_TOKEN = DATA / "search.token"                         # per-install secret the service requires (0600)
SEARCH_DEFAULTS = {"enabled": False, "folders": [], "removed": [], "paused": False, "on_battery": False}
# Voice: voice.py is a separate process like search.py (echo-cancelled mic and speaker, Parakeet, Phonon). Off unless the
# voice.enabled flag is on and the user turns it on. LOCAL_STUDIO_VOICE_PORT gives a development server its own service.
VOICE_PORT = int(os.environ.get("LOCAL_STUDIO_VOICE_PORT", 8084))
VOICE_URL = f"http://127.0.0.1:{VOICE_PORT}"
VOICE_REQS = ROOT / "voice-requirements.txt"
VOICE_DEPS_OK = DATA / "voice-deps.ok"                       # written by setup.sh --voice
VOICE_TOKEN = DATA / "voice.token"                           # per-install secret the service requires (0600)
# Local Agent: drives the Mac through CuaDriver (MCP over a unix socket in standard mode, no manifest).
AGENT_DIR = (DATA / "agent").absolute()                      # LaunchServices starts CuaDriver elsewhere: never relative
AGENT_RUNS = AGENT_DIR / "runs"
AGENT_SOCK = AGENT_DIR / "cua.sock"                          # absolute; a unix socket path must stay under 104 bytes
AGENT_DOWNLOADS = AGENT_DIR / "downloads"                    # the agent's browser saves here, and only after Chrome's save dialog
AGENT_MEMORY_FILE = AGENT_DIR / "memory.json"                # durable user preferences and project facts across chats
AGENT_SCRATCH_DIR = AGENT_DIR / "scratch"                    # isolated working directory for run_python snippets
AGENT_BACKUPS_DIR = AGENT_DIR / "backups"                    # pre-edit snapshots behind the chat's Undo button
CUA_DRIVER = shutil.which("cua-driver") or str(BIN / "cua-driver")
CUA_MIN = (0, 28, 2)
AGENT_DEFAULTS = {"enabled": False, "max_steps": 40, "max_minutes": 15, "tiers": {}}   # tiers: app name -> read|ask|allowed
AGENT_BUSY_MSG = "The agent is running a task. Stop it or wait for it to finish."
agent_lock = threading.Lock()                                # held for the length of an agent run
agent_engine = {"mode": None, "model_id": None}              # the engine the run uses, set by the loop

CATALOG = json.loads((ROOT / "catalog.json").read_text())


ENGINES = {
    "text": {"pattern": r"^[^ ]*python[^ ]* [^ ]*mlx-dspark serve", "pid": "llm", "url": "http://127.0.0.1:8080", "ready": "/health"},
    "vision": {"pattern": r"^[^ ]*python[^ ]* -m mlx_vlm server", "pid": "vlm", "url": "http://127.0.0.1:8081", "ready": "/v1/models"},
    "gguf": {"pattern": r"^[^ ]*llama-server .*--port 8082", "pid": "gguf", "url": "http://127.0.0.1:8082", "ready": "/health"},
}
