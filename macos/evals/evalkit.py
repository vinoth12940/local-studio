"""Agent-harness evaluation kit: versioned tasks, isolated fixtures, objective scoring, reproducible reports.

Nothing here starts, stops or switches a model engine. Real runs attach to the engine the user already loaded
(LOCAL_STUDIO_EVAL_ATTACH=1, see studio.services.engines.ensure_engine); scripted runs replace the model with a
deterministic transport and exist to prove that each task is solvable and that its checks discriminate.
"""
import collections
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

EVALS = Path(__file__).resolve().parent
ROOT = EVALS.parent.parent
TASKS, FIXTURES, SCRIPTS = EVALS / "tasks", EVALS / "fixtures", EVALS / "scripts"
SCHEMA_VERSION = 1
CATEGORIES = ("coding", "repair", "refusal_approval", "compaction", "recovery")
APPROVALS = ("allow_all", "deny_all", "edits_only")
STATUSES = ("passed", "failed", "blocked", "skipped", "errored")
MEASURED = ("passed", "failed", "errored")
TASK_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,62}\Z")
CHECKS = {"command": {"argv", "expect_exit"}, "file_contains": {"path", "text"}, "file_excludes": {"path", "text"},
          "file_unchanged": {"path"}, "file_exists": {"path"}, "file_absent": {"path"},
          "final_text_contains": {"text"}, "final_text_excludes": {"text"}, "max_tool_calls": {"limit"},
          "tool_never_succeeded": {"name"}, "approval_requested": {"min"}, "file_line_count": {"path", "count"}}
TOOL_LIMIT_MARKER = "Stopped after the tool-call limit"           # the model kept calling tools until the harness stopped it
REPLAY_MARKER = "cannot be automatically replayed"          # the harness refused to re-dispatch an executed action
BLOCKED_MARKERS = ("Eval attach mode", "did not become ready", "failed to start", "not installed", "isn't downloaded", "Local engine")


class TaskError(ValueError):
    pass


def _text(value, name, limit=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise TaskError(f"{name} must be a nonempty string of at most {limit} characters")
    return value


def _relative(value, name):
    path = Path(_text(value, name, 300))
    if path.is_absolute() or ".." in path.parts:
        raise TaskError(f"{name} must stay inside the task workspace")
    return value


def validate_task(task, source=""):
    if not isinstance(task, dict):
        raise TaskError(f"{source}: a task is a JSON object")
    where = f"{source or task.get('id')}"
    allowed = {"schema_version", "id", "revision", "category", "title", "fixture", "prompt", "approvals", "budgets", "flags",
               "outcome", "history_filler", "requires", "checks", "recovery"}
    if set(task) - allowed:
        raise TaskError(f"{where}: unknown fields {sorted(set(task) - allowed)}")
    if task.get("schema_version") != SCHEMA_VERSION:
        raise TaskError(f"{where}: schema_version must be {SCHEMA_VERSION}")
    if not isinstance(task.get("id"), str) or not TASK_ID.fullmatch(task["id"]):
        raise TaskError(f"{where}: id must be lowercase kebab-case")
    if type(task.get("revision")) is not int or task["revision"] < 1:
        raise TaskError(f"{where}: revision must be a positive integer (bump it whenever the task or fixture changes)")
    if task.get("category") not in CATEGORIES:
        raise TaskError(f"{where}: category must be one of {CATEGORIES}")
    _text(task.get("title"), "title", 200)
    _text(task.get("prompt"), "prompt")
    fixture = _relative(task.get("fixture"), "fixture")
    if not (FIXTURES / fixture).is_dir():
        raise TaskError(f"{where}: fixture {fixture!r} does not exist")
    if task.get("approvals") not in APPROVALS:
        raise TaskError(f"{where}: approvals must be one of {APPROVALS}")
    budgets = task.get("budgets")
    if (not isinstance(budgets, dict) or set(budgets) != {"max_tool_calls", "timeout_seconds"}
            or type(budgets["max_tool_calls"]) is not int or not 0 <= budgets["max_tool_calls"] <= 60
            or type(budgets["timeout_seconds"]) is not int or not 10 <= budgets["timeout_seconds"] <= 900):
        raise TaskError(f"{where}: budgets need max_tool_calls (0..60) and timeout_seconds (10..900)")
    flags = task.get("flags", {})
    if not isinstance(flags, dict) or not all(isinstance(k, str) and type(v) is bool for k, v in flags.items()):
        raise TaskError(f"{where}: flags must map harness flag names to booleans")
    filler = task.get("history_filler")
    if filler is not None and (not isinstance(filler, dict) or set(filler) != {"fact", "turns", "chars"}
                               or type(filler["turns"]) is not int or not 1 <= filler["turns"] <= 200
                               or type(filler["chars"]) is not int or not 200 <= filler["chars"] <= 8000):
        raise TaskError(f"{where}: history_filler needs fact, turns (1..200) and chars (200..8000)")
    if not isinstance(task.get("requires", []), list) or not set(task.get("requires", [])) <= {"engine"}:
        raise TaskError(f"{where}: requires may only list 'engine'")
    checks = task.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 4:
        raise TaskError(f"{where}: checks lists at most 4 trusted workspace verification commands")
    recovery = task.get("recovery")
    if recovery is not None:
        if (not isinstance(recovery, dict) or set(recovery) != {"interrupt_after_tool_calls"} or type(recovery["interrupt_after_tool_calls"]) is not int
                or not 1 <= recovery["interrupt_after_tool_calls"] <= 10):
            raise TaskError(f"{where}: recovery needs interrupt_after_tool_calls (1..10)")
        if task.get("category") != "recovery" or flags.get("durable_runs") is not True or task.get("approvals") != "allow_all":
            raise TaskError(f"{where}: a recovery task uses category 'recovery', flag durable_runs=true and approvals 'allow_all'")
    elif task.get("category") == "recovery":
        raise TaskError(f"{where}: a recovery task needs a recovery block")
    outcome = task.get("outcome")
    if not isinstance(outcome, list) or not 1 <= len(outcome) <= 12:
        raise TaskError(f"{where}: outcome needs 1..12 objective checks")
    for check in outcome:
        if not isinstance(check, dict) or check.get("type") not in CHECKS or set(check) != CHECKS[check["type"]] | {"type"}:
            raise TaskError(f"{where}: invalid outcome check {check!r}")
        for key in ("path",):
            if key in check:
                _relative(check[key], "outcome path")
        if check["type"] == "command" and (not isinstance(check["argv"], list) or not check["argv"]
                                           or not all(isinstance(a, str) for a in check["argv"]) or type(check["expect_exit"]) is not int):
            raise TaskError(f"{where}: command checks take an argv list and an integer expect_exit")
    return task


def load_tasks(selected=None, directory=TASKS):
    tasks, seen = [], set()
    for path in sorted(Path(directory).glob("*.json")):
        task = validate_task(json.loads(path.read_text()), path.name)
        if path.stem != task["id"] or task["id"] in seen:
            raise TaskError(f"{path.name}: file name must equal the unique task id")
        seen.add(task["id"])
        tasks.append(task)
    unknown = set(selected or []) - seen
    if unknown:
        raise TaskError(f"unknown task ids: {sorted(unknown)}")
    return [t for t in tasks if not selected or t["id"] in selected]


def load_script(task_id, kind, directory=SCRIPTS):
    """Reference (must pass) and negative (must fail) scripted model turns for a task."""
    path = Path(directory) / f"{task_id}.json"
    data = json.loads(path.read_text())
    turns = data.get(kind)
    if not isinstance(turns, list) or not turns:
        raise TaskError(f"{path.name}: missing {kind!r} turns")
    return turns


def task_set_revision(tasks):
    digest = hashlib.sha256()
    for task in sorted(tasks, key=lambda t: t["id"]):
        digest.update(json.dumps(task, sort_keys=True).encode())
        base = FIXTURES / task["fixture"]
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            digest.update(str(path.relative_to(base)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def materialize(task, destination):
    destination = Path(destination)
    shutil.copytree(FIXTURES / task["fixture"], destination)
    return destination.resolve()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_messages(task):
    messages = []
    filler = task.get("history_filler")
    if filler:
        messages += [{"role": "user", "content": f"Remember this for later: {filler['fact']}. Acknowledge in one short sentence."},
                     {"role": "assistant", "content": "Noted."}]
        block = ("Unrelated planning notes about meeting logistics, rota changes and archive tidying. " * 60)[:filler["chars"]]
        for index in range(filler["turns"]):
            messages += [{"role": "user", "content": f"Background note {index}: {block}"}, {"role": "assistant", "content": f"Read note {index}."}]
    return messages + [{"role": "user", "content": task["prompt"]}]


def parse_stream(text):
    events = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            try:
                events.append(json.loads(line[6:]))
            except ValueError:
                continue
    return events


def summarize_events(events):
    final, tools, approvals, error = [], {}, [], None
    for event in events:
        for choice in event.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if content:
                final.append(content)
        if "error" in event:
            error = str(event["error"])
        if "x_tool" in event:
            tool = event["x_tool"]
            entry = tools.setdefault(tool.get("id"), {"name": tool.get("name"), "status": None})
            entry["status"] = tool.get("status")
        if "x_approval" in event:
            approvals.append({"name": event["x_approval"].get("name"), "kind": event["x_approval"].get("kind")})
    steps = list(tools.values())
    return {"final_text": "".join(final), "tool_calls": len(steps), "tool_failures": sum(1 for s in steps if s["status"] == "error"),
            "tools": steps, "approvals": approvals, "error": error}


def score(task, workspace, summary, fixture_dir=None):
    """Evaluate the declarative outcome checks. Each result is {check, passed, detail}."""
    workspace, fixture = Path(workspace), Path(fixture_dir or FIXTURES / task["fixture"])
    results = []
    for check in task["outcome"]:
        kind, ok, detail = check["type"], False, ""
        try:
            if kind == "command":
                done = subprocess.run(check["argv"], cwd=str(workspace), capture_output=True, text=True, timeout=60,
                                      env={"PATH": os.environ.get("PATH", ""), "HOME": str(workspace), "PYTHONDONTWRITEBYTECODE": "1"})
                ok, detail = done.returncode == check["expect_exit"], (done.stdout + done.stderr)[-400:]
            elif kind in ("file_contains", "file_excludes"):
                target = workspace / check["path"]
                present = target.is_file() and check["text"] in target.read_text(errors="replace")
                ok = present if kind == "file_contains" else (target.is_file() and not present)
                detail = f"{check['path']} {'has' if present else 'lacks'} the text"
            elif kind == "file_unchanged":
                ok = (workspace / check["path"]).is_file() and _file_hash(workspace / check["path"]) == _file_hash(fixture / check["path"])
            elif kind == "file_line_count":
                target = workspace / check["path"]
                lines = [l for l in target.read_text(errors="replace").splitlines() if l.strip()] if target.is_file() else []
                ok, detail = len(lines) == check["count"], f"{len(lines)} non-empty lines"
            elif kind == "file_exists":
                ok = (workspace / check["path"]).exists()
            elif kind == "file_absent":
                ok = not (workspace / check["path"]).exists()
            elif kind == "final_text_contains":
                ok = check["text"] in summary["final_text"]
            elif kind == "final_text_excludes":
                ok = check["text"] not in summary["final_text"]
            elif kind == "max_tool_calls":
                ok, detail = summary["tool_calls"] <= check["limit"], f"{summary['tool_calls']} tool calls"
            elif kind == "tool_never_succeeded":
                ok = not any(s["name"] == check["name"] and s["status"] == "done" for s in summary["tools"])
            elif kind == "approval_requested":
                ok, detail = len(summary["approvals"]) >= check["min"], f"{len(summary['approvals'])} approvals"
        except (OSError, subprocess.SubprocessError) as error:
            detail = f"{type(error).__name__}: {error}"
        results.append({"check": check, "passed": bool(ok), "detail": detail})
    return results


# Set by Session.run_recovery once it has cancelled the run. The chat loop checks for a cancel at the start of each round
# and again before dispatching a tool. A scripted turn marked "until_interrupted" that is requested before the cancel holds
# its response until the cancel lands (so its tool call is refused, never run) and goes back into the script, so the
# resumed run is asked again, as a real model would be. Either way the interruption falls between the tool calls, however
# slow the machine (a fixed 0.3 s delay raced the interrupter and lost on CI runners).
INTERRUPTED = threading.Event()
HOLD_LIMIT_SECONDS = 30


class Scripted:
    """Deterministic model transport (same seam the offline suites use). Never reaches an engine."""
    def __init__(self, turns):
        self.turns, self.seen = list(turns), []

    @staticmethod
    def events(turn):
        if "text" in turn:
            return [{"choices": [{"delta": {"content": turn["text"]}}]}]
        name, args = turn["tool"], json.dumps(turn.get("args", {}))
        cut = max(1, len(name) // 2)
        return [{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": name[:cut], "arguments": args[:1]}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": name[cut:], "arguments": args[1:]}}]}}]}]

    def __enter__(self):
        import httpx
        from studio.services import engines
        from studio.services import models
        self._saved = (httpx.AsyncClient, engines.engine_state, engines._engine_ready, models.model_installed,
                       engines._start_engine, engines._stop_engine)
        outer, pending = self, collections.deque(self.turns)

        class Response:
            status_code = 200
            def __init__(self, events, delay=0, requeue=None): self._events, self._delay, self._requeue = events, delay, requeue
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def aiter_lines(self):
                import asyncio
                if self._requeue is not None:
                    deadline = time.monotonic() + HOLD_LIMIT_SECONDS
                    while not INTERRUPTED.is_set() and time.monotonic() < deadline:
                        await asyncio.sleep(0.01)
                    pending.appendleft(self._requeue)
                if self._delay:
                    await asyncio.sleep(self._delay)
                for event in self._events:
                    yield "data: " + json.dumps(event)
                yield "data: [DONE]"

        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def stream(self, method, url, json=None, **kwargs):
                outer.seen.append(json)
                turn = pending.popleft() if pending else {"text": "Done."}
                held = turn.get("until_interrupted") and not INTERRUPTED.is_set()
                again = {k: v for k, v in turn.items() if k != "until_interrupted"} if held else None
                return Response(Scripted.events(turn), turn.get("delay", 0), again)

        httpx.AsyncClient = Client
        from studio.storage.settings import load_settings
        mode_model = load_settings()["chat_model"]
        # The fake engine must match what the chat route asks for in every field, the context window included: a mismatch
        # sends ensure_engine down its restart path, which stops engines machine-wide (the running app's too).
        engines.engine_state = lambda: {"mode": "text", "model_id": mode_model,
                                        "context_window": models.effective_context(models.chat_model(mode_model))}
        engines._engine_ready = lambda mode: True
        models.model_installed = lambda entry: True

        def no_engine(*args, **kwargs):
            raise RuntimeError("a scripted eval run tried to start or stop a real engine")
        engines._start_engine = engines._stop_engine = no_engine
        return self

    def __exit__(self, *exc):
        import httpx
        from studio.services import engines, models
        (httpx.AsyncClient, engines.engine_state, engines._engine_ready, models.model_installed,
         engines._start_engine, engines._stop_engine) = self._saved


def _hardware():
    def sysctl(key):
        try:
            return subprocess.run(["/usr/sbin/sysctl", "-n", key], capture_output=True, text=True, timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return {"machine": platform.machine(), "model": sysctl("hw.model"), "chip": sysctl("machdep.cpu.brand_string"),
            "memory_bytes": int(sysctl("hw.memsize") or 0) or None, "macos": platform.mac_ver()[0] or None}


def runtime_versions(mode, python=None, llama_server=None):
    """Versions of the engine runtime that will actually answer, read from its own environment. 'unavailable' is explicit."""
    from studio import config
    python, llama_server = python or config.DSPARK_PY, llama_server or config.LLAMA_SERVER
    if mode == "gguf":
        try:
            done = subprocess.run([str(llama_server), "--version"], capture_output=True, text=True, timeout=10)
            lines = (done.stdout + done.stderr).strip().splitlines()
            return {"llama-server": lines[-1][:200] if lines else "unavailable"}
        except (OSError, subprocess.SubprocessError):
            return {"llama-server": "unavailable"}
    code = ("import importlib.metadata as m, json\nout = {}\nfor p in ('mlx', 'mlx-lm', 'mlx-vlm', 'mlx-dspark'):\n"
            "    try: out[p] = m.version(p)\n    except m.PackageNotFoundError: out[p] = 'unavailable'\nprint(json.dumps(out))")
    try:
        done = subprocess.run([str(python), "-I", "-c", code], capture_output=True, text=True, timeout=20)
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {p: "unavailable" for p in ("mlx", "mlx-lm", "mlx-vlm", "mlx-dspark")}


def served_model(engine_url):
    """What the running engine says it serves (best effort; recorded next to the requested model id)."""
    import httpx
    try:
        data = httpx.get(engine_url + "/v1/models", timeout=5, trust_env=False).json().get("data") or []
        return data[0].get("id") if data else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def model_match(entry, served):
    """'verified' | 'mismatch' | 'unknown': does the engine's reported model correspond to the catalog entry?"""
    if not served:
        return "unknown"
    norm = lambda text: re.sub(r"[^a-z0-9]+", "", str(text).lower())
    wanted = {norm(Path(str(entry[k])).stem if k == "file" else str(entry[k]).split("/")[-1]) for k in ("id", "repo", "file", "name") if entry.get(k)}
    seen = norm(Path(str(served)).stem if str(served).endswith(".gguf") else str(served).split("/")[-1])
    return "verified" if any(w and (w in seen or seen in w) for w in wanted if seen) else "mismatch"


def _git_state():
    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return {"commit": git("rev-parse", "HEAD") or None, "dirty": bool(git("status", "--porcelain"))}


def environment(model_id, mode, flags, scripted, served_model=None, sampling=None, model_check=None, runtime=None):
    from importlib import metadata
    from studio.config import APP_VERSION
    from studio.services.models import chat_model
    try:
        entry = chat_model(model_id)
        model = {key: entry.get(key) for key in ("id", "name", "backend", "repo", "file", "drafter", "mode", "quant", "quantization")
                 if entry.get(key) is not None}
    except Exception:
        model = {"id": model_id}
    versions = {}
    for package in ("fastapi", "httpx", "uvicorn", "starlette"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return {"model": model, "engine_mode": mode, "served_model": served_model, "runtime_versions": runtime if runtime is not None else ({} if scripted else runtime_versions(mode)), "model_check": model_check or ("n/a" if scripted else "unknown"), "scripted": scripted,
            "app_version": APP_VERSION, "git": _git_state(), "python": sys.version.split()[0], "packages": versions,
            "hardware": _hardware(), "harness_flags": flags, "sampling": sampling or {},
            "seed": (sampling or {}).get("seed"),
            "seed_note": "A requested seed is forwarded in the /api/chat body to the engine; whether the engine honors it is not verified, "
                         "so repeated runs are treated as measuring natural variance."}


class Session:
    """One isolated in-process app. The caller must have set LOCAL_STUDIO_HOME to a throwaway directory first."""
    def __init__(self, model_id, sampling=None):
        from fastapi.testclient import TestClient
        from studio import config
        live = Path.home() / "Library" / "Application Support" / "Local Studio"
        if config.STATE.resolve() == live.resolve() or not str(config.STATE).startswith(("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")):
            raise RuntimeError(f"Refusing to evaluate against {config.STATE}: use a throwaway LOCAL_STUDIO_HOME.")
        from studio.api import chat as api_chat
        from studio.main import app
        self.model_id, self.api_chat, self.sampling = model_id, api_chat, dict(sampling or {})
        self.client = TestClient(app, base_url="http://127.0.0.1:7860")

    def configure(self, flags, workspace, checks):
        from studio.storage.settings import load_settings, save_settings
        settings = load_settings()
        settings["harness"] = {"auto_approve_readonly": True, **{k: v for k, v in flags.items()}}
        if checks:
            settings["harness"]["verification_workspaces"] = {str(workspace): {"commands": checks}}
        save_settings(settings)

    def _watch(self, mode, stop, answered):
        while not stop.is_set():
            for session in list(self.api_chat._chat_sessions.values()):
                pending = session.pending_id
                if pending and not session.event.is_set() and pending not in answered:
                    answered.append(pending)
                    session.resolve({"decision": "allow" if mode == "allow_all" else "deny", "prompt_id": pending})
            time.sleep(0.02)

    def run(self, task, workspace):
        body = {"model_id": self.model_id, "chat_id": uuid.uuid4().hex[:16], "workspace": str(workspace),
                "messages": build_messages(task), **self.sampling}
        if task["approvals"] == "edits_only":
            body["permission_mode"] = "accept_edits"
        return self._exchange(task, body)

    def _runs_for_chat(self, chat_id):
        from studio.services import workflow_runs
        for path in workflow_runs.store.root.glob("*.json"):
            try:
                record = workflow_runs.store.get(path.stem, recover=False)
            except (ValueError, OSError):
                continue
            if (record.get("metadata") or {}).get("chat_id") == chat_id:
                return record
        return None

    def run_recovery(self, task, workspace):
        """Interrupt a real durable run after N completed tool steps, resume it through the resume API, score the second turn."""
        from studio.api.workflow_runs import current_identity
        from studio.services import workflow_runs
        n = task["recovery"]["interrupt_after_tool_calls"]
        chat_id = uuid.uuid4().hex[:16]
        body = {"model_id": self.model_id, "chat_id": chat_id, "workspace": str(workspace), "messages": build_messages(task), **self.sampling}
        info = {}
        INTERRUPTED.clear()

        def interrupter(stop):
            while not stop.is_set():
                record = self._runs_for_chat(chat_id)
                if record:
                    info["run_id"] = record["id"]
                    done = sum(1 for s in record["steps"].values() if s["status"] in ("completed", "failed"))
                    if done >= n and not record.get("cancel_requested"):
                        workflow_runs.store.cancel(record["id"])
                        info["interrupted_after_steps"] = done
                        INTERRUPTED.set()
                        return
                time.sleep(0.02)

        first = self._exchange(task, body, interrupter)
        info["first_status"] = first.get("status")
        if first.get("exception") or first.get("timed_out"):
            return {**first, "recovery": info}
        if "interrupted_after_steps" not in info:
            return {**first, "recovery": info, "recovery_skipped": "the model finished before the interruption point, so there was nothing to recover"}
        record = workflow_runs.store.get(info["run_id"])
        info["phase_after_interrupt"] = record["phase"]
        _, fingerprint, digest = current_identity(record["workspace"])
        prepared = self.client.post(f"/api/workflow/runs/{info['run_id']}/resume",
                                    json={"expected_seq": record["seq"], "accept_workspace_digest": fingerprint.get("digest")})
        if prepared.status_code != 200:
            return {**first, "recovery": info, "recovery_error": f"resume refused: HTTP {prepared.status_code} {prepared.text[:200]}"}
        second = self._exchange(task, {"model_id": self.model_id, "messages": body["messages"], "chat_id": chat_id, "workspace": str(workspace),
                                       **self.sampling, **prepared.json()["next_request"]})
        second["recovery"] = info
        second["latency_seconds"] = round(first.get("latency_seconds", 0) + second.get("latency_seconds", 0), 3)
        return second

    def _exchange(self, task, body, background=None):
        stop, answered, outcome = threading.Event(), [], {}

        def post():
            try:
                response = self.client.post("/api/chat", json=body)
                outcome["status"], outcome["text"] = response.status_code, response.text
            except Exception as error:                                    # noqa: BLE001 - recorded, never raised into the runner
                outcome["exception"] = f"{type(error).__name__}: {error}"

        watcher = threading.Thread(target=self._watch, args=(task["approvals"], stop, answered), daemon=True)
        worker = threading.Thread(target=post, daemon=True)
        started = time.monotonic()
        extra = threading.Thread(target=background, args=(stop,), daemon=True) if background else None
        watcher.start(); worker.start()
        if extra:
            extra.start()
        worker.join(task["budgets"]["timeout_seconds"])
        stop.set()
        outcome["timed_out"] = worker.is_alive()
        outcome["latency_seconds"] = round(time.monotonic() - started, 3)
        return outcome


def run_task(task, session, workroot, scripted_turns=None):
    """Run one task once; returns a run record with a status in STATUSES. Blocked/skipped are never successes."""
    record = {"task": task["id"], "revision": task["revision"], "category": task["category"], "status": "errored",
              "reason": None, "latency_seconds": None, "tool_calls": 0, "tool_failures": 0, "approvals": 0, "checks": []}
    if "engine" in task.get("requires", []) and scripted_turns is not None:
        record.update(status="skipped", reason="requires a real engine")
        return record
    workspace = materialize(task, Path(workroot) / f"{task['id']}-{uuid.uuid4().hex[:6]}")
    record["workspace"] = str(workspace)
    session.configure(task.get("flags", {}), workspace,
                      [{"id": c["id"], "command": c["command"]} for c in task.get("checks", [])] if task.get("checks") else None)
    context = Scripted(scripted_turns) if scripted_turns is not None else None
    if context:
        context.__enter__()
    try:
        outcome = session.run_recovery(task, workspace) if task.get("recovery") else session.run(task, workspace)
    finally:
        if context:
            context.__exit__(None, None, None)
    record["latency_seconds"] = outcome.get("latency_seconds")
    if outcome.get("exception") or outcome.get("timed_out"):
        record.update(status="errored", reason=outcome.get("exception") or f"timed out after {task['budgets']['timeout_seconds']}s")
        return record
    record["recovery"] = outcome.get("recovery")
    if outcome.get("recovery_skipped"):
        record.update(status="skipped", reason=outcome["recovery_skipped"])
        return record
    if outcome.get("recovery_error"):
        record.update(status="errored", reason=outcome["recovery_error"])
        return record
    text = outcome.get("text", "")
    if outcome.get("status") != 200:
        reason = f"HTTP {outcome.get('status')}: {text[:200]}"
        record.update(status="blocked" if any(m in text for m in BLOCKED_MARKERS) else "errored", reason=reason)
        return record
    events = parse_stream(text)
    summary = summarize_events(events)
    record.update(tool_calls=summary["tool_calls"], tool_failures=summary["tool_failures"], approvals=len(summary["approvals"]),
                  final_text=summary["final_text"][-2000:], tools=summary["tools"], events=len(events))
    if summary["error"] and REPLAY_MARKER in summary["error"] and task.get("recovery"):
        # The model tried to repeat a completed side effect after resume: a real failure of the behavior under test.
        record.update(status="failed", reason="attempted to replay a completed side effect after resume (refused by the harness)")
        record["checks"] = score(task, workspace, summary)
        return record
    if summary["error"] and TOOL_LIMIT_MARKER in summary["error"]:
        record.update(status="failed", reason="kept calling tools until the harness's tool-call limit stopped it", tool_limit_stop=True)
        record["checks"] = score(task, workspace, summary)
        return record
    if summary["error"]:
        # An error event means the turn did not run to a normal end: nothing here may be scored as an agent result.
        blocked = any(m in summary["error"] for m in BLOCKED_MARKERS)
        record.update(status="blocked" if blocked else "errored", reason=summary["error"][:300])
        return record
    if not summary["final_text"].strip() and not summary["tool_calls"]:
        record.update(status="errored", reason="the turn produced no assistant output")
        return record
    if record["recovery"]:
        summary["tool_calls"] += record["recovery"].get("interrupted_after_steps", 0)
        record["tool_calls"] = summary["tool_calls"]
    if summary["tool_calls"] > task["budgets"]["max_tool_calls"]:
        record["reason"] = f"tool budget exceeded ({summary['tool_calls']} > {task['budgets']['max_tool_calls']})"
        record["budget_exceeded"] = True
    record["checks"] = score(task, workspace, summary)
    passed = all(c["passed"] for c in record["checks"]) and not record.get("budget_exceeded")
    record["status"] = "passed" if passed else "failed"
    if not passed and not record["reason"]:
        record["reason"] = "; ".join(c["check"]["type"] for c in record["checks"] if not c["passed"])
    record["_events"] = events
    return record


def aggregate(records, tasks):
    """Per-task pass counts; blocked and skipped runs are listed but never counted as successes or failures."""
    by_task = {}
    for task in tasks:
        runs = [r for r in records if r["task"] == task["id"]]
        counts = {s: sum(1 for r in runs if r["status"] == s) for s in STATUSES}
        measured = sum(counts[s] for s in MEASURED)
        by_task[task["id"]] = {"revision": task["revision"], "category": task["category"], "runs": len(runs), **counts,
                               "measured": measured, "pass_rate": round(counts["passed"] / measured, 4) if measured else None,
                               "failures": [{"reason": r["reason"], "status": r["status"]} for r in runs if r["status"] in ("failed", "errored")],
                               "excluded": [{"reason": r["reason"], "status": r["status"]} for r in runs if r["status"] in ("blocked", "skipped")],
                               "median_latency_seconds": _median([r["latency_seconds"] for r in runs if r["latency_seconds"] is not None]),
                               "tool_failures": sum(r["tool_failures"] for r in runs)}
    passed = sum(t["passed"] for t in by_task.values())
    measured = sum(t["measured"] for t in by_task.values())
    return {"tasks": by_task, "passed": passed, "measured": measured,
            "excluded": sum(t["blocked"] + t["skipped"] for t in by_task.values()),
            "pass_rate": round(passed / measured, 4) if measured else None}


def _median(values):
    values = sorted(values)
    return None if not values else values[len(values) // 2] if len(values) % 2 else round((values[len(values) // 2 - 1] + values[len(values) // 2]) / 2, 3)


def compare(report, baseline, max_drop=0.15):
    """Regressions against a measured baseline. Scripted reports can never be a baseline or a candidate."""
    for name, value in (("candidate", report), ("baseline", baseline)):
        if value.get("environment", {}).get("scripted"):
            raise ValueError(f"A scripted {name} report measures no model and cannot be compared.")
    problems = []
    for task_id, before in baseline["summary"]["tasks"].items():
        if before["pass_rate"] is None:
            continue
        now = report["summary"]["tasks"].get(task_id)
        if now is not None and None not in (now.get("revision"), before.get("revision")) and now["revision"] != before["revision"]:
            problems.append(f"{task_id}: task revision {before.get('revision')} in the baseline vs {now.get('revision')} now; not comparable, re-measure the baseline")
            continue
        if now is None or now["pass_rate"] is None:
            problems.append(f"{task_id}: no measured runs (baseline {before['pass_rate']:.2f})")
        elif before["pass_rate"] - now["pass_rate"] > max_drop:
            problems.append(f"{task_id}: pass rate fell {before['pass_rate']:.2f} -> {now['pass_rate']:.2f}")
    b, c = baseline["summary"]["pass_rate"], report["summary"]["pass_rate"]
    if b is not None and (c is None or b - c > max_drop):
        problems.append(f"overall: pass rate {b} -> {c}")
    return problems


def render_markdown(report):
    summary, env = report["summary"], report["environment"]
    lines = [f"# Agent evaluation report", "",
             f"Model: {env['model'].get('id')} · engine: {env['engine_mode']} · scripted: {env['scripted']} · task set {report['task_set_revision'][:12]}",
             f"Overall: {summary['passed']}/{summary['measured']} measured runs passed"
             + (f" ({summary['pass_rate']:.0%})" if summary["pass_rate"] is not None else " (no measurement)")
             + f"; {summary['excluded']} blocked/skipped excluded.", "",
             "| task | category | passed | failed | errored | blocked | skipped | pass rate | median s |", "|---|---|---|---|---|---|---|---|---|"]
    for task_id, row in summary["tasks"].items():
        rate = "n/a" if row["pass_rate"] is None else f"{row['pass_rate']:.0%}"
        lines.append(f"| {task_id} | {row['category']} | {row['passed']} | {row['failed']} | {row['errored']} | {row['blocked']} | {row['skipped']} | {rate} | {row['median_latency_seconds']} |")
    for task_id, row in summary["tasks"].items():
        for item in row["failures"] + row["excluded"]:
            lines.append(f"\n- {task_id} [{item['status']}]: {item['reason']}")
    return "\n".join(lines) + "\n"


def write_report(out, report, records):
    out = Path(out)
    (out / "runs").mkdir(parents=True, exist_ok=True)
    for index, record in enumerate(records):
        run_dir = out / "runs" / f"{index:03d}-{record['task']}-{record['status']}"
        run_dir.mkdir(exist_ok=True)
        events = record.pop("_events", [])
        (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        workspace = record.get("workspace")
        if workspace and Path(workspace).is_dir():
            shutil.copytree(workspace, run_dir / "workspace", dirs_exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps(record, indent=2, default=str))
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (out / "report.md").write_text(render_markdown(report))
    return out / "report.json"


def build_report(tasks, records, env, repeat, started, finished):
    return {"schema_version": SCHEMA_VERSION, "task_set_revision": task_set_revision(tasks), "repeat": repeat,
            "started": started, "finished": finished, "environment": env, "summary": aggregate(records, tasks),
            "runs": [{k: v for k, v in r.items() if k not in ("_events", "tools", "workspace", "final_text")} for r in records]}


def temp_home():
    """A throwaway, short LOCAL_STUDIO_HOME (the cua socket path limit is 104 bytes)."""
    return tempfile.mkdtemp(prefix="ls-eval-", dir="/tmp")
