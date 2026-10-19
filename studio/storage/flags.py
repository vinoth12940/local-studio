"""Feature flags in data/settings.json, for the refactor's behavior changes (docs/refactor-progress.json).

Every flag defaults to the old behavior. Turn one on by adding it to data/settings.json under its dotted path, for example
{"logging": {"json_requests": true}}. The file is re-read when it changes, so no restart is needed unless a flag says so.
"""
import json
import threading

from studio.config import SETTINGS

# flag -> (default, what turning it on does)
FLAGS = {
    "logging.json_requests": (False, "Write one JSON line per slow (>= 250 ms), failing or non-GET request to logs/requests.jsonl"),
}

_lock = threading.Lock()
_cache = {"mtime": None, "data": {}}


def _settings():
    try:
        mtime = SETTINGS.stat().st_mtime_ns
    except OSError:
        return {}
    with _lock:
        if _cache["mtime"] != mtime:
            try:
                _cache["data"] = json.loads(SETTINGS.read_text())
            except (OSError, ValueError):
                _cache["data"] = {}
            _cache["mtime"] = mtime
        return _cache["data"]


def flag(name):
    """The flag's value from data/settings.json, or its default (the old behavior)."""
    default = FLAGS[name][0]
    node = _settings()
    for part in name.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if isinstance(node, type(default)) else default
