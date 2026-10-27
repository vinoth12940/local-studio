"""Trigger and schedule controls. Firing only queues work; a person starts it, and approvals still apply."""
from fastapi import APIRouter, Body, Header, HTTPException

from studio.services import triggers

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


@router.get("/api/workflow/triggers")
def list_triggers(workspace: str | None = None):
    return {"triggers": _call(triggers.list_triggers, workspace)}


@router.post("/api/workflow/triggers")
def create_trigger(body: dict = Body(default={})):
    _only(body, {"workspace", "task_id", "name", "prompt", "schedule", "missed_policy"}, "trigger")
    trigger, token = _call(triggers.create_trigger, body.get("workspace"), body.get("task_id"), body.get("name"), body.get("prompt"),
                           body.get("schedule"), body.get("missed_policy", "skip"))
    return {"trigger": trigger, "token": token, "token_note": "Shown once. Send it as 'Authorization: Bearer <token>' to POST /api/agent/trigger."}


@router.patch("/api/workflow/triggers/{trigger_id}")
def update_trigger(trigger_id: str, body: dict = Body(default={})):
    _only(body, {"expected_version", "enabled"}, "trigger update")
    return _call(triggers.update_trigger, trigger_id, body.get("expected_version"), body.get("enabled"))


@router.delete("/api/workflow/triggers/{trigger_id}")
def delete_trigger(trigger_id: str):
    return _call(triggers.delete_trigger, trigger_id)


@router.get("/api/workflow/notifications")
def trigger_notifications():
    return _call(triggers.notifications)


@router.post("/api/workflow/invocations/{invocation_id}/start")
def start_invocation(invocation_id: str):
    return _call(triggers.start_invocation, invocation_id)


@router.post("/api/workflow/invocations/{invocation_id}/cancel")
def cancel_invocation(invocation_id: str):
    return _call(triggers.cancel_invocation, invocation_id)


@router.post("/api/workflow/invocations/{invocation_id}/ack")
def acknowledge_invocation(invocation_id: str):
    return _call(triggers.acknowledge, invocation_id)


@router.post("/api/agent/trigger")
def fire_trigger(body: dict = Body(default={}), authorization: str | None = Header(default=None)):
    """Queue (never run) one invocation. Per-trigger bearer token; idempotency_key makes redelivery harmless."""
    _only(body, {"trigger_id", "idempotency_key"}, "trigger call")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "A bearer token is required.")
    try:
        invocation = triggers.fire_external(body.get("trigger_id"), token.strip(), body.get("idempotency_key"))
    except ValueError as error:
        raise HTTPException(401 if "credentials" in str(error) else 409, str(error)) from error
    return {"invocation": {key: invocation[key] for key in ("id", "status", "reason", "key")}, "runs_unattended": False}
