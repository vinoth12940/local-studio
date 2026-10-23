"""Local coding-run inspection, reconciliation and explicit restart recovery."""

from fastapi import APIRouter, Body, HTTPException

from studio.security.fences import _harness_path_fence
from studio.services import workflow_runs as runs
from studio.storage.flags import flag
from studio.storage.settings import load_settings

router = APIRouter()


def _enabled():
    if not (flag("harness.durable_runs") or flag("harness.workflow")):
        raise HTTPException(404, "Durable coding runs are disabled")


def _translate(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError as error:
        raise HTTPException(404, "Workflow run not found") from error
    except runs.RunConflict as error:
        raise HTTPException(409, str(error)) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error


def current_identity(workspace, settings=None):
    settings = settings or load_settings()
    path = _harness_path_fence(workspace, write=False)
    if not path.is_dir():
        raise ValueError("Workspace directory no longer exists")
    workspace = str(path)
    trusted = (settings.get("harness", {}).get("verification_workspaces") or {}).get(workspace, {})
    fingerprint = runs.workspace_fingerprint(workspace, trusted.get("fingerprint_paths"), excludes=trusted.get("fingerprint_excludes"))
    return workspace, fingerprint, runs.policy_fingerprint(settings, workspace)


@router.get("/api/workflow/runs")
def list_runs(limit: int = 50):
    _enabled()
    paths = sorted(runs.store.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, min(limit, 100))]
    records = [_translate(runs.store.get, path.stem) for path in paths]
    return {"runs": [{key: record.get(key) for key in ("id", "created", "updated", "workspace", "phase", "outcome", "seq", "metadata")} for record in records]}


@router.post("/api/workflow/runs/prune")
def prune_runs(body: dict = Body(...)):
    """User-invoked retention: delete finished runs older than N days, keeping the newest K and anything unresolved."""
    _enabled()
    if set(body) - {"older_than_days", "keep_latest"}:
        raise HTTPException(400, "Unknown prune fields.")
    return _translate(runs.prune, body.get("older_than_days"), body.get("keep_latest", 50))


@router.get("/api/workflow/runs/{run_id}")
def get_run(run_id: str):
    _enabled()
    record = _translate(runs.store.get, run_id)
    _, fingerprint, digest = _translate(current_identity, record["workspace"])
    return {**record, "current_fingerprint": fingerprint, "current_policy_digest": digest}


@router.get("/api/workflow/runs/{run_id}/events")
def run_events(run_id: str, after: int = 0):
    _enabled()
    return {"events": _translate(runs.store.events, run_id, after)}


@router.post("/api/workflow/runs/{run_id}/reconcile")
def reconcile_run(run_id: str, body: dict = Body(...)):
    _enabled()
    if type(body.get("expected_seq")) is not int:
        raise HTTPException(400, "expected_seq is required")
    return _translate(runs.store.reconcile, run_id, body.get("step_id"), body.get("disposition"),
                      body.get("evidence"), body["expected_seq"])


@router.post("/api/workflow/runs/{run_id}/resume")
def resume_run(run_id: str, body: dict = Body(...)):
    _enabled()
    if type(body.get("expected_seq")) is not int:
        raise HTTPException(400, "expected_seq is required")
    record = _translate(runs.store.get, run_id)
    identity = record.get("metadata", {}).get("task_identity")
    if identity:
        from studio.services.workflow import get_task
        task = _translate(get_task, identity["id"])
        if runs.task_identity(task) != identity or not all(task.get("artifact_status", {}).values()):
            raise HTTPException(409, "Task version, artifacts, mode, scope or budgets changed; start a fresh run")
    workspace, fingerprint, digest = _translate(current_identity, record["workspace"])
    record, token = _translate(runs.store.prepare_resume, run_id, body["expected_seq"], fingerprint, digest,
                               accept_workspace_digest=body.get("accept_workspace_digest"))
    return {"run": record, "resume_token": token, "workspace": workspace,
            "next_request": {"workflow_run_id": run_id, "workflow_resume_token": token, "workspace": workspace}}


@router.post("/api/workflow/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    _enabled()
    return _translate(runs.store.cancel, run_id)
