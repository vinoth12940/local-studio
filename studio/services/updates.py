"""App and engine versions, and engine updates.

Moved from app.py in the refactor's P1 split.
"""

import plistlib
import re
import subprocess
import time
from pathlib import Path

from studio.infra.logs import open_log
from studio.config import APP_SUPPORT, APP_VERSION, BREW, DSPARK_PY, LLAMA_SERVER, LOGS, ROOT, UV


update_job: dict = {"status": "idle"}


def _app_build():
    """The bundle's build number (build.sh writes the commit count into Info.plist), or None from a source checkout."""
    try:
        with open(ROOT.parent.parent / "Info.plist", "rb") as f:
            return str(plistlib.load(f).get("CFBundleVersion") or "") or None
    except (OSError, plistlib.InvalidFileException):
        return None


def _versions():
    v = {"app": APP_VERSION, "build": _app_build()}
    try:
        out = subprocess.run([UV, "tool", "list"], capture_output=True, text=True, timeout=20).stdout
        for name in ("mlx-dspark", "mflux"):
            m = re.search(rf"^{re.escape(name)} v(\S+)", out, re.M)
            v[name] = m.group(1) if m else None
    except Exception:
        pass
    try:
        out = subprocess.run([LLAMA_SERVER, "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"version: (\S+)", out.stdout + out.stderr)
        v["llama.cpp"] = m.group(1) if m else None
    except Exception:
        v["llama.cpp"] = None
    try:
        out = subprocess.run([str(DSPARK_PY), "-c", "import importlib.metadata as m; print(m.version('mlx-vlm'), m.version('mlx'))"],
                             capture_output=True, text=True, timeout=20).stdout.split()
        v["mlx-vlm"], v["mlx"] = out[0], out[1]
    except Exception:
        pass
    return v


def _run_update():
    log = open_log(LOGS / "update.log", "w")
    steps = [[UV, "tool", "upgrade", "mlx-dspark"], [UV, "tool", "upgrade", "mflux"]]
    if Path(BREW).exists() and not LLAMA_SERVER.startswith(str(APP_SUPPORT)):
        steps.append([BREW, "upgrade", "llama.cpp"] if Path(LLAMA_SERVER).exists() else [BREW, "install", "llama.cpp"])
    else:                                             # no Homebrew: refresh the app-managed build
        steps.append(["/bin/zsh", str(ROOT / "setup.sh"), "--llama"])
    before = _versions()
    for cmd in steps:
        update_job["step"] = " ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd))
        log.write(f"$ {update_job['step']}\n")
        log.flush()
        rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        if rc != 0 and "upgrade" in cmd and cmd[0] == BREW:
            continue                                  # brew upgrade exits 0 when already current
    after = _versions()
    changed = {k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k)}
    update_job.update(status="done", finished=time.time(), changed=changed, step=None)
