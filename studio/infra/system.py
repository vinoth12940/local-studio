"""Mac system facts: memory, sizes, pids, the clock tool.

Moved from app.py in the refactor's P1 split.
"""

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from studio.config import LOGS


# ---------------------------------------------------------------- helpers

def _sysctl(name):
    try:
        return int(subprocess.check_output(["sysctl", "-n", name], text=True).strip())
    except Exception:
        return None


def system_memory():
    """Used = app (anonymous - purgeable) + wired + compressed, as Activity Monitor counts it."""
    total = _sysctl("hw.memsize") or 0
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", out).group(1))
    pages = {k.strip().strip('"'): int(v) for k, v in re.findall(r"^(.+?):\s+(\d+)\.?$", out, re.M)}
    used = (pages.get("Anonymous pages", 0) - pages.get("Pages purgeable", 0)
            + pages.get("Pages wired down", 0) + pages.get("Pages occupied by compressor", 0)) * page
    return {"total_bytes": total, "used_bytes": used, "free_percent": _sysctl("kern.memorystatus_level")}


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _read_pid(name):
    try:
        return int((LOGS / f"{name}.pid").read_text().strip())
    except Exception:
        return None


def _chat_datetime(zone=None):
    if zone:
        try:
            now = datetime.now(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError("Use a valid IANA timezone, such as America/Chicago or UTC")
    else:
        now = datetime.now().astimezone()
        try:
            zone = str(Path('/etc/localtime').resolve()).split('/zoneinfo/', 1)[1]
        except (OSError, IndexError):
            zone = str(now.tzinfo)
    return {"datetime": now.isoformat(timespec="seconds"), "date": now.date().isoformat(),
            "weekday": now.strftime("%A"), "timezone": zone, "utc_offset": now.strftime("%z")}
