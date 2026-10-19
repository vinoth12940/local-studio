"""Streamed and background subprocesses for run_shell / run_python.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from studio.config import LOGS


_bg_processes: dict[str, dict] = {}                          # proc_id -> background terminal process entry
_bg_lock = threading.Lock()


def _run_streamed_subprocess(argv, cwd, env, timeout=30, on_output=None):
    """Run a child process, streaming stdout/stderr lines to on_output(chunk) if provided, up to timeout."""
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, start_new_session=True, bufsize=1)
    out_chunks, err_chunks = [], []

    def _reader(pipe, target, prefix=""):
        for line in pipe:
            target.append(line)
            if on_output and sum(len(x) for x in target) <= 6000:
                try:
                    on_output(prefix + line)
                except Exception:
                    pass

    t_out = threading.Thread(target=_reader, args=(proc.stdout, out_chunks), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, err_chunks), daemon=True)
    t_out.start()
    t_err.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        raise ValueError(f"Execution timed out after {timeout} seconds")
    t_out.join(timeout=1)
    t_err.join(timeout=1)
    return proc.returncode, "".join(out_chunks), "".join(err_chunks)


def _start_bg_shell(cmd: str, cwd: Path, env: dict) -> dict:
    """Start a long-running background shell command (e.g., dev server or watcher) and track its log buffer."""
    LOGS.mkdir(parents=True, exist_ok=True)
    proc_id = uuid.uuid4().hex[:10]
    log_path = LOGS / f"bg-{proc_id}.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(["/bin/zsh", "-c", cmd], cwd=cwd, stdout=log_file, stderr=subprocess.STDOUT,
                            text=True, env=env, start_new_session=True)
    entry = {"process_id": proc_id, "pid": proc.pid, "command": cmd, "cwd": str(cwd),
             "started": time.time(), "log_path": str(log_path), "proc": proc, "log_file": log_file}
    with _bg_lock:
        _bg_processes[proc_id] = entry
    # Wait briefly (150ms) to catch immediate startup output or fast-exit errors
    time.sleep(0.15)
    rc = proc.poll()
    initial_out = ""
    try:
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
