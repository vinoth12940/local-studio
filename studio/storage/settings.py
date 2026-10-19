"""data/settings.json.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import json

from studio.config import AGENT_DEFAULTS, CATALOG, SEARCH_DEFAULTS, SETTINGS


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
    SETTINGS.write_text(json.dumps(s, indent=2))
