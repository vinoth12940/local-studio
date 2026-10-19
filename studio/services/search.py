"""The file-search service: launch, stop and proxy.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import hashlib
import os
import shutil
import signal
import subprocess
import time

import httpx
from fastapi import HTTPException

from studio import config
from studio.infra.logs import open_log
from studio.services import models
from studio.config import LOGS, ROOT, SEARCH_DEPS_OK, SEARCH_DIR, SEARCH_PORT, SEARCH_REQS, STATE, UV
from studio.infra.system import _pid_alive, _read_pid
from studio.services.models import downloads, search_model, start_download
from studio.storage.settings import load_settings, save_settings


# ---------------------------------------------------------------- search
# search.py is a fourth process (port 8083): it indexes the user's files and answers queries. At
# ~2 GB it stays loaded next to a chat engine, so ensure_engine never touches it. It is off until
# the user turns it on; that is also when its dependencies and model are fetched.

search_job: dict = {"status": "idle"}


def search_deps_installed():
    """setup.sh --search records the requirements file it installed; a changed file means reinstall."""
    try:
        return SEARCH_DEPS_OK.read_text().strip() == hashlib.sha256(SEARCH_REQS.read_bytes()).hexdigest()
    except OSError:
        return False


def _search_running():
    try:
        return httpx.get(config.SEARCH_URL + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _search_process():
    """True while a search.py process exists, including one that is still loading."""
    return subprocess.run(["pgrep", "-f", rf"^[^ ]*python[^ ]* search.py --port {SEARCH_PORT}"], capture_output=True).returncode == 0


def _start_search():
    if _search_running() or _search_process():
        return
    log = open_log(LOGS / "search.log", "a")
    # Relative file names with cwd=ROOT: the bundled path contains a space, which would defeat
    # stop.sh's pkill patterns.
    proc = subprocess.Popen([UV, "run", "--no-project", "--python", "3.12", "--with-requirements", SEARCH_REQS.name,
                             "python", "search.py", "--port", str(SEARCH_PORT)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "LOCAL_STUDIO_HOME": str(STATE)}, start_new_session=True)
    (LOGS / "search.pid").write_text(str(proc.pid))


def _stop_search():
    pid = _read_pid("search")
    if pid and _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)            # uv and its python child share the session
        except OSError:
            pass
    subprocess.run(["pkill", "-f", rf"^[^ ]*uv run .*python search.py --port {SEARCH_PORT}"])
    subprocess.run(["pkill", "-f", rf"^[^ ]*python[^ ]* search.py --port {SEARCH_PORT}"])
    (LOGS / "search.pid").unlink(missing_ok=True)
    for _ in range(40):                               # wait for the port to free before any restart
        if not _search_running():
            break
        time.sleep(0.25)


def _enable_search():
    """Background job behind the Search switch: install what's missing, then start the service."""
    try:
        if not search_deps_installed():
            search_job.update(step="Installing search components (about 1 GB, one time)")
            with open_log(LOGS / "search-setup.log", "w") as log:
                rc = subprocess.run(["/bin/zsh", str(ROOT / "setup.sh"), "--search"], stdout=log, stderr=subprocess.STDOUT,
                                    env={**os.environ, "LOCAL_STUDIO_HOME": str(STATE)}).returncode
            if rc != 0 or not search_deps_installed():
                raise RuntimeError("Couldn't install the search components. See logs/search-setup.log.")
        m = search_model()
        if not models.model_installed(m):
            search_job.update(step=f"Downloading {m['name']} ({m['size_gb']} GB)")
            start_download(m)
            while downloads[m["id"]].get("status") == "running":
                time.sleep(1)
            if not models.model_installed(m):
                raise RuntimeError("The model download didn't finish. Check your connection and try again.")
        search_job.update(step="Starting Search")
        s = load_settings()
        s["search"]["enabled"] = True
        save_settings(s)
        _start_search()
        for _ in range(180):                          # the first start imports PyTorch and MLX
            if _search_running():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Search didn't start. See logs/search.log.")
        search_job.update(status="done", step=None)
    except Exception as e:
        search_job.update(status="error", error=str(e), step=None)


def _search_proxy(method, path, **kw):
    try:
        r = httpx.request(method, config.SEARCH_URL + path, timeout=60, **kw)
    except httpx.HTTPError:
        raise HTTPException(503, "Search isn't running. Turn it on in Settings.")
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        raise HTTPException(r.status_code, detail or "Search error")
    return r


def _reset_index(restart):
    _stop_search()
    shutil.rmtree(SEARCH_DIR, ignore_errors=True)
    if restart:
        _start_search()
    return {"ok": True}
