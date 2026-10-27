"""Isolated Git worktrees for scoped write workers, with user-reviewed, conflict-checked integration.

A worker is a separate checkout (its own branch) of the parent task's repository. The parent's workspace is never
written until a person reviews the exact patch and integrates it by its digest. The coordinator adds NO model tool: a
worker is an ordinary Code chat opened on the worktree path, so it keeps its own approvals, fences, budgets and task
scope. Children never gain permissions: a worker's file scope must lie inside the parent task's scope and its budgets
inside the parent's remaining budgets. `delegate_write` stays an opaque, blocked tool under test protection.

Scope matching reuses the task policy's fnmatch rules. Glob intersection is undecidable in general, so overlap and
containment use a conservative probe (wildcards replaced by a literal) that is exact for directory-style scopes.
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from studio import config
from studio.security.workflow_policy import _matches
from studio.services import workflow, workflow_runs
from studio.storage.atomic import write_text_atomic
from studio.storage.flags import flag

MAX_PATCH_BYTES = 2_000_000
PREVIEW_CHARS = 20_000
MAX_FILES = 200
BRANCH_PREFIX = "localstudio/"
GIT_PREFIX = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.file.allow=never",
              "-c", "user.name=Local Studio", "-c", "user.email=local-studio@localhost", "-c", "commit.gpgsign=false"]
STATES_ACTIVE = ("active",)


def _enabled():
    if not (flag("harness.workflow") and flag("harness.worktrees")):
        raise ValueError("Isolated write workers need the workflow and worktrees features to be enabled.")


def _root():
    # Not under data/workflow: that folder is the run journal's own state and cannot be a fingerprinted workspace.
    root = config.STATE / "data" / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(worker_id):
    if not isinstance(worker_id, str) or not workflow.ID_PATTERN.fullmatch(worker_id):
        raise ValueError("Invalid worker id.")
    return _root() / (worker_id + ".json")


@contextmanager
def _locked(name="registry"):
    with (_root() / f".{name}.lock").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _git(cwd, args, input_bytes=None, ok=(0,)):
    done = subprocess.run([*GIT_PREFIX, "-C", str(cwd), *args], input=input_bytes, capture_output=True, timeout=60,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"})
    if done.returncode not in ok:
        raise ValueError("git " + " ".join(args[:2]) + " failed: " + done.stderr.decode("utf-8", "replace").strip()[-400:])
    return done


def _text(cwd, args):
    return _git(cwd, args).stdout.decode("utf-8", "replace")


def _read(worker_id):
    path = _path(worker_id)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 500_000:
        raise ValueError("Worker not found.")
    record = json.loads(path.read_text())
    if record.get("schema_version") != 1 or record.get("id") != worker_id:
        raise ValueError("Unsupported or corrupt worker record.")
    return record


def _write(record):
    record["updated_at"] = time.time()
    write_text_atomic(_path(record["id"]), json.dumps(record, indent=2))


def _all():
    records = []
    for path in sorted(_root().glob("*.json")):
        try:
            records.append(_read(path.stem))
        except (ValueError, OSError, json.JSONDecodeError):
            continue
    return records


def _probe(pattern):
    return re.sub(r"[*?\[\]]+", "x", pattern)


def scope_within(child, parent):
    """True when every child pattern is allowed by the parent patterns."""
    return all(_matches(_probe(pattern), parent) for pattern in child)


def scopes_overlap(first, second):
    return (any(_matches(_probe(p), second) for p in first) or any(_matches(_probe(p), first) for p in second))


def _repository(task):
    base = task["workspace"]
    try:
        top = _text(base, ["rev-parse", "--show-toplevel"]).strip()
        head = _text(base, ["rev-parse", "HEAD"]).strip()
    except ValueError:
        raise ValueError("Isolated workers need the workspace to be a Git repository with at least one commit.") from None
    if Path(top).resolve() != Path(base).resolve():
        raise ValueError("Select the repository root as the task workspace; workers cannot be created from a subdirectory.")
    return head, _text(base, ["branch", "--show-current"]).strip() or None


def _remaining(task):
    usage = task.get("usage", {})
    return (max(0, task["budgets"]["max_steps"] - usage.get("steps", 0)),
            max(0, task["budgets"]["max_seconds"] - workflow.task_elapsed(task)))


def _runs_in(record):
    runs = []
    for path in workflow_runs.store.root.glob("*.json"):
        try:
            run = workflow_runs.store.get(path.stem, recover=False)
        except (ValueError, OSError):
            continue
        if run["workspace"] == record["path"]:
            runs.append(run)
    return sorted(runs, key=lambda r: r["created"])


def settle_steps(worker_id):
    """Charge the parent task for tool steps of this worker's finished runs, once each. Accounting happens after the fact:
    a worker chat has no task of its own, so overspending is reported (and blocks integration), not prevented mid-run."""
    with _locked("settle"):                            # its own lock: integrate/abandon hold the registry lock when they settle
        record = _read(worker_id)
        charged = set(record.get("charged_runs", []))
        fresh = [r for r in _runs_in(record) if r["id"] not in charged and r["phase"] not in ("ready", "running", "waiting_approval")]
        if fresh:
            added = sum(len(r["steps"]) for r in fresh)
            with workflow._locked(record["parent_task_id"]):
                parent = workflow._read(record["parent_task_id"])
                parent["usage"]["steps"] = min(parent["budgets"]["max_steps"], parent["usage"]["steps"] + added)
                workflow._write(parent)
            record["charged_runs"] = sorted(charged | {r["id"] for r in fresh})
            record["steps_used"] = record.get("steps_used", 0) + added
            _write(record)
        return record


def create_worker(task_id, owner, scope, budgets=None):
    _enabled()
    task = workflow.get_task(task_id)
    if task["state"] in ("paused", "cancelled"):
        raise ValueError(f"The parent task is {task['state']}.")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 64 or "\n" in owner:
        raise ValueError("A worker needs a short single-line owner label.")
    scope = workflow._scope(scope)
    if not scope_within(scope, task["scope"]):
        raise ValueError("A worker's scope must lie inside the parent task's scope; children cannot gain permissions.")
    steps_left, seconds_left = _remaining(task)
    budgets = budgets or {}
    if not isinstance(budgets, dict) or set(budgets) - {"max_steps", "max_seconds"}:
        raise ValueError("Worker budgets may only set max_steps and max_seconds.")
    wanted = {"max_steps": budgets.get("max_steps", min(steps_left, 24)), "max_seconds": budgets.get("max_seconds", min(int(seconds_left), 600))}
    for key, limit in (("max_steps", steps_left), ("max_seconds", int(seconds_left))):
        if type(wanted[key]) is not int or not 1 <= wanted[key] <= limit:
            raise ValueError(f"{key} must be an integer between 1 and the parent's remaining {limit}.")
    head, branch = _repository(task)
    with _locked():
        active = [r for r in _all() if r["parent_task_id"] == task_id and r["state"] in STATES_ACTIVE]
        if len(active) >= task["budgets"]["max_children"]:
            raise ValueError(f"The task allows at most {task['budgets']['max_children']} active workers.")
        for other in [r for r in _all() if r["workspace"] == task["workspace"] and r["state"] in STATES_ACTIVE]:
            if scopes_overlap(scope, other["scope"]):
                raise ValueError(f"Scope overlaps worker {other['id']} owned by {other['owner']}; write ownership must be disjoint.")
        if sum(r["budgets"]["max_steps"] for r in active) + wanted["max_steps"] > steps_left:
            raise ValueError("Active workers would exceed the parent's remaining step budget.")
        worker_id = uuid.uuid4().hex
        tree = _root() / worker_id / "tree"
        tree.parent.mkdir()
        branch_name = BRANCH_PREFIX + worker_id[:12]
        try:
            _git(task["workspace"], ["worktree", "add", "-b", branch_name, str(tree), head])
        except ValueError:
            shutil.rmtree(tree.parent, ignore_errors=True)
            raise
        record = {"schema_version": 1, "id": worker_id, "version": 1, "parent_task_id": task_id, "parent_task_version": task["version"],
                  "workspace": task["workspace"], "base_commit": head, "base_branch": branch, "branch": branch_name,
                  "path": str(tree.resolve()), "owner": owner.strip(), "scope": scope, "budgets": wanted, "state": "active",
                  "created_at": time.time(), "dirty_at_creation": _dirty(task["workspace"]), "integration": None,
                  "requires_after_integration": ["configured checks", "independent review when the task requires it"]}
        _write(record)
        return record


def _dirty(workspace):
    lines = _text(workspace, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]).split("\x00")
    return sorted({line[3:] for line in lines if len(line) > 3})


def _snapshot(record):
    """Commit the worker's pending changes on its own branch so they have a stable, reviewable identity."""
    tree = record["path"]
    if _text(tree, ["status", "--porcelain=v1"]).strip():
        _git(tree, ["add", "-A"])
        _git(tree, ["commit", "-q", "-m", f"Local Studio worker snapshot ({record['owner']})"])
    return _text(tree, ["rev-parse", "HEAD"]).strip()


def prepare_integration(worker_id):
    """The exact patch a person must review. Returns its digest, files and every blocking problem."""
    _enabled()
    record = _read(worker_id)
    if record["state"] != "active":
        raise ValueError(f"This worker is {record['state']}; only an active worker can be integrated.")
    if not Path(record["path"]).is_dir():
        raise ValueError("The worker's checkout is missing; it cannot be integrated.")
    record = settle_steps(worker_id)
    head = _snapshot(record)
    patch = _git(record["path"], ["diff", "--binary", "--full-index", record["base_commit"], head]).stdout
    if len(patch) > MAX_PATCH_BYTES:
        raise ValueError("The worker's patch is larger than the review limit; split the work.")
    raw = _text(record["path"], ["diff", "--raw", "-z", "--no-renames", record["base_commit"], head]).split("\x00")
    entries = [(raw[i], raw[i + 1]) for i in range(0, len(raw) - 1, 2) if raw[i].startswith(":")]
    files = sorted(path for _, path in entries)
    problems = []
    if record.get("steps_used", 0) > record["budgets"]["max_steps"]:
        problems.append(f"The worker used {record['steps_used']} tool steps, over its allocation of {record['budgets']['max_steps']}.")
    if not files:
        problems.append("The worker made no changes.")
    if len(files) > MAX_FILES:
        problems.append(f"More than {MAX_FILES} changed files.")
    task = workflow.get_task(record["parent_task_id"])
    if task["state"] in ("paused", "cancelled"):
        problems.append(f"The parent task is {task['state']}.")
    out_of_scope = [f for f in files if not _matches(f, record["scope"])]
    if out_of_scope:
        problems.append("Changes outside the worker's scope: " + ", ".join(out_of_scope[:10]))
    unsafe = [path for meta, path in entries if meta.split()[1] == "120000" or meta.split()[0] == ":120000" or path.startswith(".git/") or path == ".git"]
    if unsafe:
        problems.append("Symlinks and .git paths cannot be integrated: " + ", ".join(unsafe[:10]))
    main_head = _text(record["workspace"], ["rev-parse", "HEAD"]).strip()
    stale = main_head != record["base_commit"]
    if stale:
        moved = set(_text(record["workspace"], ["diff", "--name-only", "-z", record["base_commit"], main_head]).split("\x00"))
        clash = sorted(set(files) & moved)
        if clash:
            problems.append("Stale base: the main workspace changed these files since the worker started: " + ", ".join(clash[:10]))
    user_changes = sorted(set(files) & set(_dirty(record["workspace"])))
    if user_changes:
        problems.append("Uncommitted changes in the main workspace would be overwritten: " + ", ".join(user_changes[:10]))
    if patch and not problems:
        check = _git(record["workspace"], ["apply", "--check", "--whitespace=nowarn"], input_bytes=patch, ok=(0, 1))
        if check.returncode:
            problems.append("The patch does not apply cleanly: " + check.stderr.decode("utf-8", "replace").strip()[-300:])
    return {"worker_id": worker_id, "digest": hashlib.sha256(patch).hexdigest(), "snapshot_commit": head, "base_commit": record["base_commit"],
            "files": files, "stale_base": stale, "problems": problems, "can_integrate": not problems,
            "patch_preview": patch.decode("utf-8", "replace")[:PREVIEW_CHARS], "patch_truncated": len(patch) > PREVIEW_CHARS}


def integrate(worker_id, expected_digest):
    """Apply the reviewed patch to the parent's working tree (never committed). Fresh checks/review are then required."""
    _enabled()
    with _locked():
        prepared = prepare_integration(worker_id)
        record = _read(worker_id)                      # re-read: settling steps may have updated it
        if not isinstance(expected_digest, str) or expected_digest != prepared["digest"]:
            raise ValueError("The worker's changes differ from the patch you reviewed; review the current changes first.")
        if prepared["problems"]:
            raise ValueError("Integration blocked: " + " | ".join(prepared["problems"]))
        patch = _git(record["path"], ["diff", "--binary", "--full-index", record["base_commit"], prepared["snapshot_commit"]]).stdout
        before = workflow_runs.workspace_fingerprint(record["workspace"])
        _git(record["workspace"], ["apply", "--whitespace=nowarn"], input_bytes=patch)
        with workflow._locked(record["parent_task_id"]):
            parent = workflow._read(record["parent_task_id"])
            parent["post_integration"] = {"worker_id": worker_id, "at": time.time(), "files": prepared["files"]}
            workflow._write(parent)
        record.update(state="integrated", version=record["version"] + 1,
                      integration={"digest": prepared["digest"], "files": prepared["files"], "at": time.time(),
                                   "workspace_fingerprint_before": before.get("digest")})
        _write(record)
        (_root() / f"{worker_id}.patch").write_bytes(patch)
        return {"worker": record, "applied_files": prepared["files"], "committed": False,
                "next": "The main workspace changed: rerun the configured checks and, if required, the independent review."}


def abandon(worker_id):
    _enabled()
    with _locked():
        record = _read(worker_id)
        if record["state"] != "active":
            raise ValueError(f"This worker is already {record['state']}.")
        _retain(record)
        record.update(state="abandoned", version=record["version"] + 1)
        _write(record)
        return record


def _retain(record):
    """Keep the worker's work recoverable (patch file) before anything is removed."""
    if Path(record["path"]).is_dir():
        try:
            head = _snapshot(record)
            patch = _git(record["path"], ["diff", "--binary", "--full-index", record["base_commit"], head]).stdout
            (_root() / f"{record['id']}.patch").write_bytes(patch)
        except ValueError:
            pass


def cleanup(worker_id, delete_branch=False, force=False):
    """Remove the checkout. The branch and saved patch are kept unless delete_branch is explicit."""
    _enabled()
    with _locked():
        record = _read(worker_id)
        if record["state"] == "active":
            raise ValueError("Integrate or abandon the active worker before cleaning it up.")
        tree = Path(record["path"])
        if tree.is_dir():
            if _text(tree, ["status", "--porcelain=v1"]).strip() and not force:
                raise ValueError("The worker's checkout has uncommitted changes; pass force to discard them (the saved patch is kept).")
            _retain(record)
            _git(record["workspace"], ["worktree", "remove", "--force", str(tree)])
        shutil.rmtree(tree.parent, ignore_errors=True)
        if delete_branch:
            _git(record["workspace"], ["branch", "-D", record["branch"]], ok=(0, 1))
        _git(record["workspace"], ["worktree", "prune"])
        record.update(cleaned=True, branch_deleted=bool(delete_branch), version=record["version"] + 1)
        _write(record)
        return record


def cancel_children(task_id):
    """Parent paused/cancelled: stop every active worker and cancel durable runs working in its checkout."""
    cancelled = []
    with _locked():
        for record in _all():
            if record["parent_task_id"] != task_id or record["state"] != "active":
                continue
            for path in workflow_runs.store.root.glob("*.json"):
                try:
                    run = workflow_runs.store.get(path.stem, recover=False)
                except (ValueError, OSError):
                    continue
                if run["workspace"] == record["path"] and run["phase"] in ("ready", "running", "waiting_approval"):
                    workflow_runs.store.cancel(run["id"])
            _retain(record)
            record.update(state="cancelled", version=record["version"] + 1)
            _write(record)
            cancelled.append(record["id"])
    return cancelled


def list_workers(task_id=None):
    """Registered workers plus anything on disk or in Git that the registry does not explain."""
    _enabled()
    workers = []
    for record in _all():
        if task_id and record["parent_task_id"] != task_id:
            continue
        present = Path(record["path"]).is_dir()
        health = "ok" if present or record.get("cleaned") else "missing"
        runs = [{"id": r["id"], "phase": r["phase"], "outcome": r.get("outcome"), "steps": len(r["steps"])} for r in _runs_in(record)]
        workers.append({**record, "checkout_present": present, "health": health, "runs": runs})
    known = {r["id"] for r in _all()}
    orphans = [p.name for p in _root().iterdir() if p.is_dir() and p.name not in known]
    return {"workers": workers, "orphaned_directories": sorted(orphans)}
