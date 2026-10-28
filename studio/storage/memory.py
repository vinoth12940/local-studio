"""The agent's cross-chat memory file.

Moved from app.py in the refactor's P1 split.
"""

import json
import os
import threading

from studio.config import AGENT_DIR, AGENT_MEMORY_FILE


_memory_lock = threading.Lock()


def _load_agent_memory() -> dict:
    with _memory_lock:
        try:
            data = json.loads(AGENT_MEMORY_FILE.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _save_agent_memory(data: dict):
    with _memory_lock:
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = AGENT_MEMORY_FILE.with_name(".memory.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, AGENT_MEMORY_FILE)
