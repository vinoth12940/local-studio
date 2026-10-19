"""Feature flags in data/settings.json, for the refactor's behavior changes (docs/refactor-progress.json).

Every flag defaults to the old behavior. Turn one on by adding it to data/settings.json under its dotted path, for example
{"logging": {"json_requests": true}}. The file is re-read when it changes, so no restart is needed unless a flag says so.
"""
import json
import threading

from studio.config import SETTINGS

# flag -> (default, what turning it on does)
FLAGS = {
    "performance.isolated_io": (False, "Use a bounded dedicated pool for search media/render routes"),
    "performance.chat_index": (False, "Cache chat titles/timestamps while validating file signatures on every list request"),
    "search.incremental": (False, "Debounce edited-file reindexing and return 503 plus Retry-After during full index passes"),
    "streams.resume": (False, "Keep bounded chat SSE buffers and allow reattachment with event IDs"),
    "logging.json_requests": (False, "Write one JSON line per slow (>= 250 ms), failing or non-GET request to logs/requests.jsonl"),
    "security.api_token": (False, "Require the private local API token or startup cookie; readiness probes remain public"),
    "harness.sandbox": (False, "Run Python/shell tools under Seatbelt with workspace/temp writes and protected credential reads"),
    "harness.rlimits": (False, "Limit tool CPU time, file size, open files and process-group RSS"),
    "security.mcp_approval": (False, "Require approval of the configured MCP command and environment before discovery or calls"),
    "security.redact": (False, "Redact API keys, tokens and private keys in the chat's live tool output and tool details"),
    "security.upload_sniff": (False, "Refuse uploads whose bytes do not match their type (magic numbers), with 415"),
    "security.csp": ("off", "Content-Security-Policy: \"report-only\" sends the -Report-Only header, \"enforce\" enforces it"),
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
