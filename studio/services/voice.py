"""The voice service: install, launch, stop and talk to voice.py.

voice.py is a separate process (port 8084) like search.py: it holds the speech models (about 1.5 GB) and the
echo-cancelling audio engine, so it is exempt from the one-engine rule. It is off unless the voice.enabled flag is on
and the user turns Voice on; that is also when its dependencies are installed (setup.sh --voice). Every request to it
carries a per-install token, so no other local process or web page can drive the microphone.
"""

import hashlib
import os
import secrets
import signal
import subprocess
import time

import httpx
from fastapi import HTTPException

from studio import config
from studio.config import LOGS, ROOT, STATE, UV, VOICE_DEPS_OK, VOICE_PORT, VOICE_REQS, VOICE_TOKEN
from studio.infra.logs import open_log
from studio.infra.system import _pid_alive, _read_pid
from studio.storage.flags import flag

voice_job: dict = {"status": "idle"}


def voice_available():
    return bool(flag("voice.enabled"))


def voice_deps_installed():
    """setup.sh --voice records the requirements file it installed; a changed file means reinstall."""
    try:
        return VOICE_DEPS_OK.read_text().strip() == hashlib.sha256(VOICE_REQS.read_bytes()).hexdigest()
    except OSError:
        return False


def _voice_token():
    """The secret voice.py requires, created once with owner-only permissions."""
    try:
        token = VOICE_TOKEN.read_text().strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    VOICE_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(VOICE_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def _voice_running():
    try:
        return httpx.get(config.VOICE_URL + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _voice_process():
    """True while a voice.py process exists, including one that is still loading."""
    return subprocess.run(["pgrep", "-f", rf"^[^ ]*python[^ ]* voice.py --port {VOICE_PORT}"], capture_output=True).returncode == 0


def _voice_command():
    # Relative file names with cwd=ROOT: the bundled path contains a space, which would defeat stop.sh's pkill patterns.
    return [UV, "run", "--no-project", "--python", "3.12", "--with-requirements", VOICE_REQS.name,
            "python", "voice.py", "--port", str(VOICE_PORT)]


def _start_voice():
    if _voice_running() or _voice_process():
        return
    log = open_log(LOGS / "voice.log", "a")
    proc = subprocess.Popen(_voice_command(), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "LOCAL_STUDIO_HOME": str(STATE), "LOCAL_STUDIO_VOICE_TOKEN": _voice_token(),
                                 "PYTHONDONTWRITEBYTECODE": "1"},
                            start_new_session=True)
    (LOGS / "voice.pid").write_text(str(proc.pid))


def _stop_voice():
    """Stop voice.py; it closes the microphone on SIGTERM."""
    pid = _read_pid("voice")
    if pid and _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)            # uv and its python child share the session
        except OSError:
            pass
    subprocess.run(["pkill", "-f", rf"^[^ ]*uv run .*python voice.py --port {VOICE_PORT}"])
    subprocess.run(["pkill", "-f", rf"^[^ ]*python[^ ]* voice.py --port {VOICE_PORT}"])
    (LOGS / "voice.pid").unlink(missing_ok=True)
    for _ in range(40):                               # wait for the port to free before any restart
        if not _voice_running():
            break
        time.sleep(0.25)


def _enable_voice():
    """Background job behind turning Voice on: install what's missing, then start the service and wait for it."""
    try:
        if not voice_deps_installed():
            voice_job.update(step="Installing voice components (one time)")
            with open_log(LOGS / "voice-setup.log", "w") as log:
                rc = subprocess.run(["/bin/zsh", str(ROOT / "setup.sh"), "--voice"], stdout=log, stderr=subprocess.STDOUT,
                                    env={**os.environ, "LOCAL_STUDIO_HOME": str(STATE)}).returncode
            if rc != 0 or not voice_deps_installed():
                raise RuntimeError("Couldn't install the voice components. See logs/voice-setup.log.")
        voice_job.update(step="Starting Voice")
        _start_voice()
        for _ in range(120):
            if _voice_running():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Voice didn't start. See logs/voice.log.")
        voice_job.update(status="done", step=None)
    except Exception as e:
        voice_job.update(status="error", error=str(e), step=None)


def voice_request(method, path, timeout=60, **kw):
    """One request to voice.py with the token. 503 when it is not running; its own errors pass through."""
    try:
        headers = {"Authorization": "Bearer " + _voice_token(), **kw.pop("headers", {})}
        r = httpx.request(method, config.VOICE_URL + path, timeout=timeout, headers=headers, **kw)
    except httpx.HTTPError:
        raise HTTPException(503, "Voice isn't running. Turn it on first.")
    if r.status_code != 200:
        detail = r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else None
        raise HTTPException(r.status_code, detail or "Voice error")
    return r
