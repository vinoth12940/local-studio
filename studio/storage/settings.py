"""data/settings.json.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import json

from studio.config import AGENT_DEFAULTS, CATALOG, SEARCH_DEFAULTS, SETTINGS
from studio.storage.atomic import write_text_atomic


def load_settings():
    try:
        s = json.loads(SETTINGS.read_text())
    except Exception:
        s = {}
    s.setdefault("chat_model", next(m["id"] for m in CATALOG["chat"] if m.get("default")))
    s.setdefault("image_model", next(m["id"] for m in CATALOG["image"] if m.get("default")))
    s.setdefault("custom", [])
    s.setdefault("mcp_servers", [])
    s["harness"] = {"auto_approve_readonly": True, **(s.get("harness") or {})}
    s["search"] = {**SEARCH_DEFAULTS, **(s.get("search") or {})}
    s["agent"] = {**AGENT_DEFAULTS, **(s.get("agent") or {})}
    return s


def save_settings(s):
    write_text_atomic(SETTINGS, json.dumps(s, indent=2))           # never a half-written settings.json (refactor P3)
