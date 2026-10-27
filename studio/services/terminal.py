"""The Code view's terminal: a login zsh on a pseudo-terminal.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import atexit
import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException


# ---- terminal: the Code view's interactive shell, a login zsh on a pseudo-terminal streamed to xterm.js

_TERM_MAX_SESSIONS = 8
_TERM_SCROLLBACK = 512_000                                    # bytes kept for a reconnecting view
# Run between fork and the shell: take the pty as controlling terminal, and start the shell with a clean signal state (no
# blocked or ignored signals inherited from the server), so Ctrl-C, Ctrl-Z and job control behave as in Terminal.app.
_TERM_CTTY = ("import fcntl, os, signal, sys, termios\n"
              "signal.pthread_sigmask(signal.SIG_SETMASK, set())\n"
              "for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU, signal.SIGCHLD, signal.SIGHUP, signal.SIGPIPE):\n"
              "    signal.signal(s, signal.SIG_DFL)\n"
              "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
              "os.execv(sys.argv[1], sys.argv[1:])")
_terminals: dict[str, "_Terminal"] = {}
_terminals_lock = threading.Lock()


def _term_env() -> dict:
    """The user's environment without the app's own: uv's ephemeral Python and our flags would shadow theirs, so PATH starts
    from the system default and the login shell (path_helper, ~/.zprofile) builds the rest."""
    drop = ("VIRTUAL_ENV", "UV_", "PYTHON", "LOCAL_STUDIO_", "HF_HUB_", "__CF")
    env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
    env.update({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "xterm-256color", "COLORTERM": "truecolor",
                "TERM_PROGRAM": "LocalStudio", "LANG": env.get("LANG") or "en_US.UTF-8"})
    return env


def _term_winsize(fd: int, rows: int, cols: int):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(2, min(rows, 500)), max(10, min(cols, 1000)), 0, 0))


class _Terminal:
    """One shell on a pty. A reader thread keeps the last _TERM_SCROLLBACK bytes; `end` is the absolute byte offset of the
    stream so a view can resume where it left off. The shell is a session leader with the pty as its controlling terminal
    (job control works, and it gets SIGHUP when the master closes, even if this server is killed)."""

    def __init__(self, cwd: Path, rows: int, cols: int):
        self.id, self.cwd, self.started = uuid.uuid4().hex[:12], str(cwd), time.time()
        master, slave = pty.openpty()
        try:
            _term_winsize(slave, rows, cols)
            self.proc = subprocess.Popen([sys.executable, "-I", "-c", _TERM_CTTY, "/bin/zsh", "-l"],
                                         stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=_term_env(),
                                         start_new_session=True, close_fds=True)
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.fd, self.buf, self.end, self.exit_code = master, bytearray(), 0, None
        self.cond = threading.Condition()
        threading.Thread(target=self._pump, daemon=True, name=f"term-{self.id}").start()

    def _pump(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self.cond:
                self.buf += data
                self.end += len(data)
                if len(self.buf) > _TERM_SCROLLBACK:
                    del self.buf[:len(self.buf) - _TERM_SCROLLBACK]
                self.cond.notify_all()
        try:
            rc = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rc = -1
        with self.cond:
            self.exit_code = rc
            self.cond.notify_all()

    def read(self, offset: int, timeout: float) -> tuple[bytes, int, int | None]:
        """Bytes after `offset` (waiting up to `timeout` for some), the new offset, and the exit code once the shell is gone."""
        with self.cond:
            if self.end <= offset and self.exit_code is None:
                self.cond.wait(timeout)
            start = self.end - len(self.buf)
            return bytes(self.buf[max(offset, start) - start:]), self.end, self.exit_code

    def write(self, data: bytes):
        if self.exit_code is not None:
            raise ValueError("This terminal has exited.")
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view):]

    def resize(self, rows: int, cols: int):
        _term_winsize(self.fd, rows, cols)               # the kernel sends SIGWINCH to the foreground job

    def close(self):
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
                self.proc.wait(timeout=1)
                break
            except (ProcessLookupError, PermissionError):
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            os.close(self.fd)
        except OSError:
            pass
        with self.cond:
            if self.exit_code is None:
                self.exit_code = self.proc.returncode if self.proc.returncode is not None else -1
            self.cond.notify_all()

    def info(self) -> dict:
        return {"id": self.id, "cwd": self.cwd, "pid": self.proc.pid, "started": self.started,
                "running": self.exit_code is None, "exit_code": self.exit_code}


def _terminal(tid: str) -> _Terminal:
    term = _terminals.get(tid)
    if term is None:
        raise HTTPException(404, "No such terminal.")
    return term


@atexit.register
def _close_terminals():
    with _terminals_lock:
        terms = list(_terminals.values())
        _terminals.clear()
    for term in terms:
        term.close()
