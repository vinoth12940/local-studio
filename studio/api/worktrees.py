"""Explicit local-user controls for isolated write workers. There is no model-facing tool for any of this."""
from fastapi import APIRouter, Body, HTTPException

from studio.services import worktrees

router = APIRouter()


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ValueError, OSError) as error:
        raise HTTPException(409, str(error)) from error


def _only(body, allowed, what):
    if not isinstance(body, dict) or set(body) - set(allowed):
        raise HTTPException(400, f"Unknown {what} fields.")
    return body


@router.get("/api/workflow/worktrees")
def list_workers(task_id: str | None = None):
    return _call(worktrees.list_workers, task_id)


@router.post("/api/workflow/worktrees")
def create_worker(body: dict = Body(default={})):
    _only(body, {"task_id", "owner", "scope", "budgets"}, "worker")
    return _call(worktrees.create_worker, body.get("task_id"), body.get("owner"), body.get("scope"), body.get("budgets"))


@router.get("/api/workflow/worktrees/{worker_id}/integration")
def review_integration(worker_id: str):
    return _call(worktrees.prepare_integration, worker_id)


@router.post("/api/workflow/worktrees/{worker_id}/integrate")
def integrate_worker(worker_id: str, body: dict = Body(default={})):
    _only(body, {"expected_digest"}, "integration")
    return _call(worktrees.integrate, worker_id, body.get("expected_digest"))


@router.post("/api/workflow/worktrees/{worker_id}/abandon")
def abandon_worker(worker_id: str):
    return _call(worktrees.abandon, worker_id)


@router.post("/api/workflow/worktrees/{worker_id}/cleanup")
def cleanup_worker(worker_id: str, body: dict = Body(default={})):
    _only(body, {"delete_branch", "force"}, "cleanup")
    return _call(worktrees.cleanup, worker_id, bool(body.get("delete_branch")), bool(body.get("force")))
