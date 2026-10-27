#!/usr/bin/env python3
"""Seed a THROWAWAY Local Studio state with demo data for manual testing of reviews, triggers and isolated workers.

    LOCAL_STUDIO_HOME=build/agent-ui-state python3-with-pinned-requirements macos/seed_workflow_demo.py

It refuses to run against the live app state, enables the workflow flags only in that throwaway state, builds a small Git
workspace under it, and uses a stubbed reviewer (no model is contacted). Re-running resets the demo workspace.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
home = os.environ.get("LOCAL_STUDIO_HOME")
if not home:
    sys.exit("Set LOCAL_STUDIO_HOME to a throwaway directory (for example build/agent-ui-state).")
STATE = Path(home).expanduser()
STATE = (STATE if STATE.is_absolute() else Path.cwd() / STATE).resolve()
live = (Path.home() / "Library" / "Application Support" / "Local Studio").resolve()
if STATE == live or live in STATE.parents:
    sys.exit("Refusing to seed the live app state.")
os.environ["LOCAL_STUDIO_HOME"] = str(STATE)
sys.path.insert(0, str(ROOT))

from studio.services import reviews, triggers, workflow, workflow_runs, worktrees  # noqa: E402
from studio.services.verification import VerificationGate  # noqa: E402
from studio.storage.settings import load_settings, save_settings  # noqa: E402

ws = STATE / "ws-m7"
shutil.rmtree(STATE / "data" / "workflow", ignore_errors=True)                  # a clean demo every run (throwaway state only)
shutil.rmtree(ws, ignore_errors=True)
ws.mkdir(parents=True)


def git(*args):
    subprocess.run(["git", "-C", str(ws), "-c", "user.name=demo", "-c", "user.email=demo@example.com", *args], check=True, capture_output=True)


(ws / "src.py").write_text("def add(a, b):\n    return a + b\n")
git("init", "-q"); git("add", "."); git("commit", "-q", "-m", "base")
ws = ws.resolve()
CHECK = 'python3 -c "import sys; sys.exit(0)"'
settings = load_settings()
settings["harness"].update(workflow=True, durable_runs=True, review=True, triggers=True, worktrees=True, verification_gate=True,
                           verification_workspaces={str(ws): {"commands": [{"id": "tests", "command": CHECK}]}})
save_settings(settings)
task = workflow.create_task(str(ws), "Fix add", "add(1,2) == 3", "Edit src.py then run the tests", mode="act", review_required=True)
(ws / "src.py").write_text("def add(a, b):\n    return a + b + 0\n")                    # uncommitted work to review
gate = VerificationGate(True, str(ws), load_settings(), fingerprints=True)
args = {"command": CHECK}
gate.before_execution("run_shell", args)
done = subprocess.run(CHECK, shell=True, cwd=str(ws), capture_output=True, text=True)
gate.observe("run_shell", args, {"command": CHECK, "cwd": str(ws), "returncode": done.returncode, "stdout": "", "stderr": ""}, executed=True)
run, _ = workflow_runs.store.create(str(ws), workflow_runs.policy_fingerprint(load_settings(), str(ws)), workflow_runs.workspace_fingerprint(ws),
                                    [{"role": "user", "content": "work"}], metadata={"task_id": task["id"], "workflow_mode": "act"})
workflow_runs.store.claim(run["id"]); workflow_runs.store.finish(run["id"], "needs_user", gate.snapshot())
payload = {"complete": True, "summary": "Reviewed src.py against the acceptance criteria.", "findings": [
    {"id": "F1", "title": "Redundant no-op term", "severity": "high", "path": "src.py", "line": 2, "side": "right", "evidence": "return a + b + 0 adds a no-op term."},
    {"id": "F2", "title": "Missing docstring", "severity": "low", "path": "src.py", "line": 1, "side": "right", "evidence": "add() has no docstring."}]}
reviews._call_local_reviewer = lambda model_id, ctx: (json.dumps(payload), {"model_id": "stub", "mode": "text", "served_model": "stub", "max_tokens": 4096, "temperature": 0, "fresh_context": True})
current = workflow.get_task(task["id"])
reviews.create_review(current["id"], current["version"], None, run["id"])
triggers.create_trigger(str(ws), task["id"], "Nightly dependency check", "Check for outdated packages.", {"type": "interval", "every_seconds": 3600})
external, token = triggers.create_trigger(str(ws), task["id"], "From CI", "Summarize the failing job.", None)
triggers.fire_external(external["id"], token, "ci-delivery-0001")
worker = worktrees.create_worker(task["id"], "notes-writer", ["NOTES.md"], {"max_steps": 10, "max_seconds": 120})
print(json.dumps({"state": str(STATE), "workspace": str(ws), "task": task["id"], "worker_checkout": worker["path"], "external_trigger_token": token}, indent=2))
