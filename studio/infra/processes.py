"""Streamed and background subprocesses for run_shell / run_python.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import os
import codecs
import contextvars
import selectors
import signal
import subprocess
import threading
import tempfile
import time
import uuid
from pathlib import Path

from studio.infra.logs import open_log
from studio.security.execution import sandbox_command, resource_limits, limit_child, start_rss_watchdog
from studio.config import LOGS


_bg_processes: dict[str, dict] = {}                          # proc_id -> background terminal process entry
_bg_lock = threading.Lock()


# Bound to one chat turn in its execution worker; Stop never targets another chat.
_execution_cancel = contextvars.ContextVar("_execution_cancel", default=None)


class ExecutionStopped(ValueError):
    def __init__(self, message, stdout="", stderr=""):
        super().__init__(message)
        self.stdout, self.stderr = stdout, stderr


def _run_streamed_subprocess(argv, cwd, env, timeout=30, on_output=None, python=False, network=False):
    """Drain bounded output while enforcing wall time and per-turn cancellation."""
    cancel = _execution_cancel.get()
    if cancel and cancel.is_set():
        raise ExecutionStopped("Execution stopped by the user.")
    temp = tempfile.TemporaryDirectory(prefix='ls-exec-', dir='/tmp')
    proc, watchdog = None, None
    selector = selectors.DefaultSelector()
    captured, streamed = {"stdout": "", "stderr": ""}, {"stdout": 0, "stderr": 0}
    line_start = {"stdout": True, "stderr": True}
    decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in captured}

    def capture(name, chunk):
        value = captured[name] + chunk
        # Preserve the start and the most recent diagnostics, including the final traceback.
        captured[name] = value if len(value) <= 12000 else value[:3000] + "\n... output truncated ...\n" + value[-8900:]
        if on_output and streamed[name] < 6000:
            visible = chunk[:6000 - streamed[name]]
            streamed[name] += len(visible)
            for part in visible.splitlines(keepends=True):
                try:
                    on_output(("[stderr] " if name == "stderr" and line_start[name] else "") + part)
                except Exception:
                    pass
                line_start[name] = part.endswith("\n")

    def kill_group():
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if proc.poll() is None:
                proc.kill()

    try:
        argv, env = sandbox_command(argv, cwd, env, python=python, temp=temp.name, network=network)
        limits = resource_limits()
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True,
                                **({'preexec_fn': limit_child(limits)} if limits else {}))
        watchdog = start_rss_watchdog(proc, limits['rss_bytes']) if limits else None
        for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        deadline, exited_at, stopped = time.monotonic() + timeout, None, None
        while selector.get_map() or proc.poll() is None:
            now = time.monotonic()
            if stopped is None:
                if cancel and cancel.is_set():
                    stopped = "Execution stopped by the user."
                elif now >= deadline:
                    stopped = f"Execution timed out after {timeout} seconds"
                if stopped:
                    kill_group()
            if proc.poll() is not None:
                if exited_at is None:
                    exited_at = now
                    # Foreground jobs must not leave children holding pipes open.
                    kill_group()
                if now - exited_at >= 1:
                    break
            for key, _ in selector.select(.05):
                try:
                    chunk = os.read(key.fileobj.fileno(), 4096)
                except BlockingIOError:
                    continue
                capture(key.data, decoders[key.data].decode(chunk, final=not chunk))
                if not chunk:
                    selector.unregister(key.fileobj)
        proc.wait(timeout=5)
        if stopped:
            raise ExecutionStopped(stopped, captured["stdout"], captured["stderr"])
        if watchdog and watchdog[1].is_set():
            raise ExecutionStopped('Execution exceeded the RSS memory limit; process group stopped.',
                                   captured["stdout"], captured["stderr"])
        return proc.returncode, captured["stdout"], captured["stderr"]
    finally:
        if watchdog:
            watchdog[0].set()
        kill_group()
        if proc:
            proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
        selector.close()
        temp.cleanup()


def _start_bg_shell(cmd: str, cwd: Path, env: dict) -> dict:
    """Start a long-running background shell command (e.g., dev server or watcher) and track its log buffer."""
    LOGS.mkdir(parents=True, exist_ok=True)
    proc_id = uuid.uuid4().hex[:10]
    log_path = LOGS / f"bg-{proc_id}.log"
    log_file = open_log(log_path, "w", encoding="utf-8")
    temp = tempfile.TemporaryDirectory(prefix='ls-bg-', dir='/tmp')
    try:
        argv, env = sandbox_command(['/bin/zsh', '-c', cmd], cwd, env, temp=temp.name)
        limits = resource_limits()
        proc = subprocess.Popen(argv, cwd=cwd, stdout=log_file, stderr=subprocess.STDOUT,
                                text=True, env=env, start_new_session=True,
                                **({'preexec_fn': limit_child(limits)} if limits else {}))
    except BaseException:
        log_file.close()
        temp.cleanup()
        raise
    watchdog = start_rss_watchdog(proc, limits['rss_bytes']) if limits else None
    def reap():
        proc.wait()
        if watchdog:
            watchdog[0].set()
        log_file.close()
        temp.cleanup()
    threading.Thread(target=reap, daemon=True).start()
    entry = {"process_id": proc_id, "pid": proc.pid, "command": cmd, "cwd": str(cwd),
             "started": time.time(), "log_path": str(log_path), "proc": proc, "log_file": log_file}
    with _bg_lock:
        _bg_processes[proc_id] = entry
    # Wait briefly (150ms) to catch immediate startup output or fast-exit errors
    time.sleep(0.15)
    rc = proc.poll()
    initial_out = ""
    try:
        if not getattr(log_file, "writer", log_file).closed:
            log_file.flush()
        initial_out = log_path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        pass
    return {"process_id": proc_id, "pid": proc.pid, "command": cmd, "cwd": str(cwd),
            "status": "exited" if rc is not None else "running", "returncode": rc, "stdout": initial_out}


def _manage_bg_process(action: str, process_id: str | None = None) -> dict:
    action = (action or "list").strip().lower()
    with _bg_lock:
        items = list(_bg_processes.values())
    if action == "list" or not process_id:
        out = []
        for it in items:
            rc = it["proc"].poll()
            out.append({"process_id": it["process_id"], "pid": it["pid"], "command": it["command"],
                        "cwd": it["cwd"], "status": "exited" if rc is not None else "running",
                        "returncode": rc, "elapsed_s": round(time.time() - it["started"], 1)})
        return {"processes": out, "count": len(out)}
    with _bg_lock:
        entry = _bg_processes.get(process_id)
    if not entry:
        raise ValueError(f"Unknown background process_id: {process_id}")
    proc = entry["proc"]
    log_path = Path(entry["log_path"])
    if action == "stop":
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
        try:
            entry["log_file"].close()
        except Exception:
            pass
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:] if log_path.is_file() else ""
        return {"process_id": process_id, "status": "stopped", "returncode": proc.poll(), "logs": tail}
    rc = proc.poll()
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.is_file() else ""
    return {"process_id": process_id, "pid": entry["pid"], "command": entry["command"],
            "status": "exited" if rc is not None else "running", "returncode": rc,
            "elapsed_s": round(time.time() - entry["started"], 1), "logs": tail}
