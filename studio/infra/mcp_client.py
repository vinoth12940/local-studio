"""The stdio MCP JSON-RPC client (CuaDriver and custom MCP servers).

Moved from app.py in the refactor's P1 split.
"""

import base64
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

from studio.config import APP_VERSION


AGENT_CUA_TOOLS = frozenset((
    "start_session end_session get_session check_permissions health_report "
    "browser_prepare get_browser_state browser_navigate browser_click browser_type browser_pointer browser_dialog browser_download "
    "list_apps list_windows launch_app get_window_state click double_click type_text set_value press_key hotkey scroll invoke_menu zoom bring_to_front"
).split())                                                   # everything else CuaDriver offers (kill_app, clipboard_*, set_config, ...) is never reachable


class McpError(Exception):
    pass


class AppUnresolved(McpError):
    """get_window_state answered, but the app's window can't be resolved through accessibility (degraded ax_window_unresolved):
    clicks and keys can't reach it until the user quits and reopens the app."""


class McpClient:
    """MCP over stdio to a child process: newline-delimited JSON-RPC 2.0. Calls are serialized; a reader
    thread routes each response to the queue of the request that is waiting for it. If the process has
    exited, the next call starts it again (once). A call that dies mid-flight is reported, never replayed:
    a click that may have happened must not happen twice."""

    def __init__(self, argv, allowed=AGENT_CUA_TOOLS, prepare=None, touch=None, env=None):
        self.argv, self.allowed, self.prepare, self.touch, self.env = argv, allowed, prepare, touch, env
        self.proc = None
        self.pending: dict = {}
        self.next_id = 0
        self.eof = True
        self.lock = threading.RLock()

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None and not self.eof

    def start(self):
        with self.lock:
            self._start()

    def _start(self):
        self._kill()
        if self.prepare:
            self.prepare()
        self.pending, self.eof = {}, False
        p_env = {**os.environ, **(self.env or {})} if self.env else None
        try:
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, env=p_env)
        except OSError as e:
            self.eof = True
            raise McpError(f"Couldn't start {Path(self.argv[0]).name}: {e}")
        threading.Thread(target=self._read, args=(self.proc, self.pending), daemon=True).start()
        try:
            self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "local-studio", "version": APP_VERSION}}, 15)
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except McpError:
            self._kill()
            raise

    def _read(self, proc, pending):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            q = pending.get(msg.get("id")) if isinstance(msg, dict) and "method" not in msg else None
            if q is not None:
                q.put(msg)
        if proc is self.proc:
            self.eof = True
        for q in list(pending.values()):                      # wake every waiter: the process is gone
            q.put(None)

    def _write(self, msg):
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError):
            raise McpError("The computer-control helper stopped responding.")

    def _request(self, method, params, timeout):
        self.next_id += 1
        rid, q = self.next_id, queue.Queue()
        self.pending[rid] = q
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            try:
                msg = q.get(timeout=timeout)
            except queue.Empty:
                raise McpError(f"{method} timed out after {timeout:g}s")
            if msg is None:
                raise McpError("The computer-control helper stopped responding.")
            if "error" in msg:
                raise McpError(str(msg["error"].get("message") or msg["error"]))
            return msg.get("result") or {}
        finally:
            self.pending.pop(rid, None)

    def list_tools(self, timeout=15):
        with self.lock:
            if self.touch:
                self.touch()
            if not self.alive:
                self._start()
            res = self._request("tools/list", {}, timeout)
            if self.touch:
                self.touch()
        return res.get("tools") or []

    def call(self, name, args, timeout=30):
        if self.allowed is not None and name not in self.allowed:
            raise McpError(f"{name} is not allowed")
        with self.lock:
            if self.touch:
                self.touch()
            if not self.alive:
                self._start()
            res = self._request("tools/call", {"name": name, "arguments": args or {}}, timeout)
            if self.touch:
                self.touch()
        parts = res.get("content") or []
        text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
        data = res.get("structuredContent")
        if data is None:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        image = next((base64.b64decode(p["data"]) for p in parts if p.get("type") == "image" and p.get("data")), None)
        return {"ok": not res.get("isError"), "text": text, "data": data if isinstance(data, dict) else None, "image": image}

    def _kill(self):
        proc, self.proc, self.eof = self.proc, None, True
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
            try:
                proc.stdout.close()
            except OSError:
                pass

    def close(self):
        with self.lock:
            self._kill()
