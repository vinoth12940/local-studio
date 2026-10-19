"""The CuaDriver daemon, its permissions and the shared MCP client.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import re
import subprocess
import threading
import time
from pathlib import Path

from studio import config
from studio.infra import mcp_client
from studio.config import AGENT_DIR, agent_lock, CUA_MIN
from studio.infra.mcp_client import McpError


# ---------------------------------------------------------------- agent

AGENT_PERM_MSG = "Grant Accessibility and Screen Recording to CuaDriver first (Settings → Agent)."
_cua_perm: dict = {}                                         # check_permissions of the running daemon: {accessibility, screen_recording}; {} = not looked yet
AGENT_IDLE_S = 600                                           # stop the daemon this long after the last call when no run is active
AGENT_SOCK_WAIT_S = 10                                       # how long to wait for a fresh daemon's socket


def cua_status():
    """What the Settings screen shows about the CuaDriver install."""
    out = {"installed": False, "version": None, "ok_version": False, "telemetry": None}
    if not Path(config.CUA_DRIVER).exists():
        return out
    out["installed"] = True
    try:
        head = subprocess.run([config.CUA_DRIVER, "--help"], capture_output=True, text=True, timeout=5).stdout
        m = re.match(r"\s*cua-driver (\d+)\.(\d+)\.(\d+)", head)
        if m:
            out["version"] = ".".join(m.groups())
            out["ok_version"] = tuple(int(x) for x in m.groups()) >= CUA_MIN
        t = subprocess.run([config.CUA_DRIVER, "telemetry", "status"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"Telemetry:\s*(enabled|disabled)", t)
        out["telemetry"] = m.group(1) == "enabled" if m else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


def agent_daemon_pattern():
    """Matches our CuaDriver daemon (its own socket) and not another app's, such as one on the default socket.
    No `^` anchor: the Mac app's data folder contains spaces."""
    return "cua-driver serve --socket " + re.escape(str(config.AGENT_SOCK))


def _agent_daemon_running():
    return subprocess.run(["pgrep", "-f", agent_daemon_pattern()], capture_output=True).returncode == 0


def start_agent_daemon():
    """Start CuaDriver's daemon through LaunchServices, so it runs with CuaDriver's own macOS permissions
    (not those of this app), on a socket of our own."""
    if len(str(config.AGENT_SOCK).encode()) >= 104:                 # sun_path holds 104 bytes on macOS, the terminating NUL included
        raise McpError(f"The agent's socket path is too long for macOS (104 bytes at most): {config.AGENT_SOCK}. Use a shorter data folder.")
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    if not _agent_daemon_running():                          # one that is still starting just needs its socket awaited
        _cua_perm.clear()                                    # a new daemon: look at its permissions again
        config.AGENT_SOCK.unlink(missing_ok=True)                   # a stale one from a daemon that died
        if subprocess.run(["open", "-n", "-g", "-a", "CuaDriver", "--args", "serve", "--socket", str(config.AGENT_SOCK),
                           "--permission-mode", "standard"], capture_output=True).returncode != 0:
            raise McpError("Couldn't start CuaDriver. Is it installed in Applications?")
    deadline = time.time() + AGENT_SOCK_WAIT_S
    while time.time() < deadline:
        if config.AGENT_SOCK.exists():
            return
        time.sleep(0.1)
    raise McpError("CuaDriver started but didn't open its socket.")


def stop_agent_daemon():
    """Stop our daemon (and the isolated browser it owns, which exits with it)."""
    global _agent_cli
    with _agent_cli_lock:
        if _agent_cli is not None:
            _agent_cli.close()
            _agent_cli = None
    subprocess.run(["pkill", "-f", agent_daemon_pattern()])
    _cua_perm.clear()
    for _ in range(40):
        if not _agent_daemon_running():
            break
        time.sleep(0.1)
    config.AGENT_SOCK.unlink(missing_ok=True)


def _perm_value(v):
    """True / False for a permission CuaDriver reports as granted / not granted; None for anything else."""
    if isinstance(v, bool):
        return v
    if isinstance(v, dict):
        return next((_perm_value(v[k]) for k in ("granted", "authorized", "enabled", "status", "state") if k in v), None)
    if isinstance(v, str):
        t = v.strip().lower().replace("_", " ")
        if t in ("granted", "authorized", "allowed", "yes", "true", "enabled", "on"):
            return True
        if t in ("denied", "not granted", "not authorized", "missing", "no", "false", "not determined", "disabled", "off", "revoked"):
            return False
    return None


def _parse_permissions(res):
    """{accessibility, screen_recording} from CuaDriver's check_permissions: True, False, or None when the reply doesn't say
    clearly (its shape is not verified live, and None never blocks a run)."""
    out = {"accessibility": None, "screen_recording": None}
    d = res.get("data") if isinstance(res, dict) else None
    if isinstance(d, dict):
        for src in (d, d.get("permissions") if isinstance(d.get("permissions"), dict) else {}):
            for k, v in src.items():
                key = re.sub(r"[^a-z]", "", str(k).lower())
                field = "accessibility" if key == "accessibility" else "screen_recording" if key in ("screenrecording", "screencapture") else None
                if field and _perm_value(v) is not None:
                    out[field] = _perm_value(v)
    for line in str((res or {}).get("text") or "").splitlines():
        low = line.lower()
        field = "accessibility" if "accessibility" in low else "screen_recording" if re.search(r"screen\s*(?:recording|capture)", low) else None
        if not field or out[field] is not None:
            continue
        if re.search(r"not granted|denied|missing|not authori[sz]ed|\bfalse\b|\bno\b|✗|❌", low):
            out[field] = False
        elif re.search(r"granted|authori[sz]ed|\btrue\b|\byes\b|✓|✅", low):
            out[field] = True
    return out


def agent_check_permissions(client):
    """CuaDriver's permissions, asked once per daemon start (the cache is cleared when a daemon starts or stops, and after a
    grant). A reply that can't be read is cached as unknown."""
    if not _cua_perm:
        try:
            got = _parse_permissions(client.call("check_permissions", {}))
        except Exception:
            got = {"accessibility": None, "screen_recording": None}
        _cua_perm.update(got)
    return dict(_cua_perm)


def _perm_missing(p):
    return False in ((p or {}).get("accessibility"), (p or {}).get("screen_recording"))


def _ensure_agent_daemon():
    if not (_agent_daemon_running() and config.AGENT_SOCK.exists()):
        start_agent_daemon()


_agent_cli = None
_agent_cli_lock = threading.Lock()
_agent_last_use = None                                       # time of the last call, None while the daemon is not in use
_agent_reaper = None


def agent_touch():
    global _agent_last_use
    _agent_last_use = time.time()


def agent_client():
    """The one MCP client for our daemon. The daemon is started if needed, and again before any restart
    of the client, so a daemon the idle reaper stopped comes back on the next use."""
    global _agent_cli
    with _agent_cli_lock:
        _ensure_agent_daemon()
        if _agent_cli is None:
            _agent_cli = mcp_client.McpClient([config.CUA_DRIVER, "mcp", "--socket", str(config.AGENT_SOCK)], prepare=_ensure_agent_daemon, touch=agent_touch)
        agent_touch()
        _agent_reaper_start()
        return _agent_cli


def agent_reap(now=None):
    """Stop the daemon once it has sat unused for AGENT_IDLE_S and no agent run is active. True if it stopped."""
    global _agent_last_use
    if _agent_last_use is None or agent_lock.locked():
        return False
    if (time.time() if now is None else now) - _agent_last_use < AGENT_IDLE_S:
        return False
    _agent_last_use = None
    stop_agent_daemon()
    return True


def _agent_reaper_start():
    global _agent_reaper
    if _agent_reaper is not None:
        return

    def loop():
        while True:
            time.sleep(60)
            try:
                agent_reap()
            except Exception:
                pass
    _agent_reaper = threading.Thread(target=loop, daemon=True)
    _agent_reaper.start()
