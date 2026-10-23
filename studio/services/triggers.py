"""Durable local triggers and schedules that QUEUE work; they never run it.

A schedule tick or an authenticated external call creates a pending *invocation*. Nothing executes until a person
starts it in the app, which prepares an ordinary /api/chat turn against an existing workflow task. That turn keeps the
task's mode, scope and budgets and goes through every approval, fence and gate, so a trigger cannot widen permissions
or act while nobody is there to approve. Unattended execution is a policy decision this module does not make.

Time is always passed in (`now`), so schedule, missed-run and clock-change behaviour is testable without sleeping.
"""
import fcntl
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from studio import config
from studio.services import workflow, workflow_runs
from studio.storage.atomic import write_text_atomic
from studio.storage.flags import flag

ID = workflow.ID_PATTERN
MIN_INTERVAL = 60
ON_TIME_SECONDS = 120          # a due slot older than this counts as missed under the "skip" policy
START_GRACE_SECONDS = 600      # a started invocation with no recorded run after this long is reported interrupted
KEEP_INVOCATIONS = 50
MISSED_POLICIES = ("skip", "queue_one")
ACTIVE = ("pending", "started")
TERMINAL_NOTICE = ("completed", "failed", "needs_user", "interrupted", "blocked")
RUN_OUTCOMES = {"verified_complete": "completed", "complete": "completed", "planned": "completed",
                "incomplete": "failed", "failed": "failed", "abandoned": "failed",
                "needs_user": "needs_user", "needs_reconciliation": "needs_user", "interrupted": "needs_user",
                "cancelled": "cancelled"}


def _root():
    root = config.STATE / "data" / "workflow" / "triggers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _enabled():
    if not (flag("harness.workflow") and flag("harness.durable_runs") and flag("harness.triggers")):
        raise ValueError("Triggers need the workflow, durable runs and triggers features to be enabled.")


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid trigger or invocation id.")
    return value


@contextmanager
def _locked(trigger_id):
    with (_root() / (_id(trigger_id) + ".lock")).open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _path(trigger_id):
    return _root() / (_id(trigger_id) + ".json")


def _read(trigger_id):
    path = _path(trigger_id)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 200_000:
        raise ValueError("Trigger not found.")
    trigger = json.loads(path.read_text())
    if trigger.get("schema_version") != 1 or trigger.get("id") != trigger_id:
        raise ValueError("Unsupported or corrupt trigger.")
    return trigger


def _write(trigger, now):
    trigger["updated_at"] = now
    write_text_atomic(_path(trigger["id"]), json.dumps(trigger, indent=2))


def _inv_dir(trigger_id):
    path = _root() / (_id(trigger_id) + ".inv")
    path.mkdir(exist_ok=True)
    return path


def _inv_write(invocation):
    write_text_atomic(_inv_dir(invocation["trigger_id"]) / (invocation["id"] + ".json"), json.dumps(invocation, indent=2))


def _invocations(trigger_id):
    items = []
    for path in sorted(_inv_dir(trigger_id).glob("*.json")):
        try:
            items.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(items, key=lambda item: (item["created_at"], item["id"]))


def _prune(trigger_id):
    items = _invocations(trigger_id)
    for old in [i for i in items if i["status"] not in ACTIVE][:-KEEP_INVOCATIONS]:
        (_inv_dir(trigger_id) / (old["id"] + ".json")).unlink(missing_ok=True)


def _validate_schedule(schedule):
    if schedule is None:
        return None
    if isinstance(schedule, dict) and schedule.get("type") == "interval" and set(schedule) == {"type", "every_seconds"}:
        every = schedule["every_seconds"]
        if type(every) is int and MIN_INTERVAL <= every <= 7 * 86400:
            return {"type": "interval", "every_seconds": every}
    if isinstance(schedule, dict) and schedule.get("type") == "daily" and set(schedule) == {"type", "at"}:
        if isinstance(schedule["at"], str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", schedule["at"]):
            return {"type": "daily", "at": schedule["at"]}
    raise ValueError(f"Schedule must be null (external only), an interval of {MIN_INTERVAL}..604800 seconds, or a daily HH:MM time.")


def next_slot(schedule, after):
    """First schedule slot strictly after `after`."""
    if schedule["type"] == "interval":
        raise ValueError("Interval slots are anchored to the trigger; use _interval_slots.")
    hour, minute = map(int, schedule["at"].split(":"))
    moment = datetime.fromtimestamp(after)
    slot = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while slot.timestamp() <= after:
        slot += timedelta(days=1)
    return slot.timestamp()


def _first_due(trigger, after):
    schedule = trigger["schedule"]
    if schedule is None:
        return None
    if schedule["type"] == "interval":
        every = schedule["every_seconds"]
        return trigger["anchor"] + every * (int((after - trigger["anchor"]) // every) + 1)
    return next_slot(schedule, after)


def create_trigger(workspace, task_id, name, prompt, schedule=None, missed_policy="skip", now=None):
    """Returns (trigger, token). The token is shown once; only its SHA-256 is stored."""
    _enabled()
    now = time.time() if now is None else now
    base = workflow.workspace_path(workspace)
    task = workflow.get_task(task_id)
    if task["workspace"] != str(base):
        raise ValueError("The task belongs to another workspace.")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("A trigger needs a short name.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise ValueError("A trigger needs the instruction to send (at most 4000 characters).")
    if missed_policy not in MISSED_POLICIES:
        raise ValueError(f"missed_policy must be one of {MISSED_POLICIES}.")
    token = "lst_" + secrets.token_urlsafe(32)
    trigger = {"schema_version": 1, "id": uuid.uuid4().hex, "version": 1, "name": name.strip(), "workspace": str(base),
               "task_id": task_id, "prompt": prompt.strip(), "schedule": _validate_schedule(schedule), "enabled": True,
               "missed_policy": missed_policy, "overlap": "one_active", "permissions": {"approvals": "always_required", "from": "task"},
               "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "anchor": now, "created_at": now, "last_tick": now,
               "counters": {"fired": 0, "missed": 0, "skipped_overlap": 0, "deduplicated": 0, "clock_regressions": 0}}
    trigger["next_due"] = _first_due(trigger, now)
    with _locked(trigger["id"]):
        _write(trigger, now)
    return public(trigger), token


def public(trigger):
    return {key: value for key, value in trigger.items() if key != "token_sha256"}


def _enqueue(trigger, key, source, due_at, now):
    """Dedupe by key, then enforce the one-active-invocation overlap limit. Caller holds the trigger lock."""
    existing = next((i for i in _invocations(trigger["id"]) if i["key"] == key), None)
    if existing:
        trigger["counters"]["deduplicated"] += 1
        return existing
    overlapped = any(i["status"] in ACTIVE for i in _invocations(trigger["id"]))
    invocation = {"id": uuid.uuid4().hex, "trigger_id": trigger["id"], "key": key, "source": source, "due_at": due_at,
                  "created_at": now, "status": "skipped" if overlapped else "pending", "reason": "overlap: a previous invocation is still active" if overlapped else None,
                  "notice": None, "seen": overlapped, "run_id": None, "request_key": None}
    trigger["counters"]["skipped_overlap" if overlapped else "fired"] += 1
    _inv_write(invocation)
    _prune(trigger["id"])
    return invocation


def _tick_one(trigger, now):
    """Advance one trigger's schedule. Fires at most one invocation per tick. Caller holds the lock."""
    if not trigger["enabled"]:
        trigger["last_tick"] = now
        return
    if now < trigger["last_tick"] - 5:                                   # the clock moved backwards: re-anchor, never fire
        trigger["counters"]["clock_regressions"] += 1
        trigger["last_tick"] = now
        trigger["anchor"] = now                                           # interval slots continue from the corrected clock
        trigger["next_due"] = _first_due(trigger, now)
        return
    trigger["last_tick"] = now
    due = trigger.get("next_due")
    if due is None or now < due:
        return
    schedule = trigger["schedule"]
    if schedule["type"] == "interval":
        every = schedule["every_seconds"]
        count = int((now - due) // every) + 1
        latest = due + (count - 1) * every
    else:
        slots, cursor = [due], next_slot(schedule, due)
        while cursor <= now and len(slots) < 400:
            slots.append(cursor)
            cursor = next_slot(schedule, cursor)
        count, latest = len(slots), slots[-1]
    on_time = now - latest <= ON_TIME_SECONDS
    fire = on_time or trigger["missed_policy"] == "queue_one"
    trigger["counters"]["missed"] += count - (1 if fire else 0)
    trigger["next_due"] = _first_due(trigger, now)
    if fire:
        _enqueue(trigger, f"sched:{trigger['id']}:{int(latest)}", "schedule", latest, now)


def _run_for(request_key):
    digest = hashlib.sha256(request_key.encode()).hexdigest()
    for path in workflow_runs.store.root.glob("*.json"):
        try:
            record = workflow_runs.store.get(path.stem, recover=False)
        except (ValueError, OSError):
            continue
        if record.get("request_key") == digest:
            return record
    return None


def _sync(trigger, invocation, now):
    """Reflect the durable run's real outcome onto a started invocation. Never re-runs anything."""
    if invocation["status"] != "started":
        return
    record = _run_for(invocation["request_key"])
    if record is None:
        if now - invocation["started_at"] > START_GRACE_SECONDS:
            invocation.update(status="interrupted", seen=False, notice=f"{trigger['name']}: the start was interrupted before a run was recorded. "
                                                                      "Nothing was run again automatically; start it again if you still want it.")
            _inv_write(invocation)
        return
    invocation["run_id"] = record["id"]
    if record.get("phase") in ("ready", "running", "waiting_approval"):
        _inv_write(invocation)
        return
    status = RUN_OUTCOMES.get(record.get("outcome") or record.get("phase"), "needs_user")    # e.g. unknown_outcome needs a person
    detail = {"completed": "finished and its checks passed", "failed": "ended without completing", "cancelled": "was cancelled",
              "needs_user": "needs your attention (an action was declined, a budget ran out, or an outcome is unknown)"}[status]
    invocation.update(status=status, seen=status == "cancelled", notice=f"{trigger['name']}: the run {detail}.")
    _inv_write(invocation)


def _refresh(trigger_id, now):
    with _locked(trigger_id):
        trigger = _read(trigger_id)
        _tick_one(trigger, now)
        for invocation in _invocations(trigger_id):
            _sync(trigger, invocation, now)
        _write(trigger, now)
        return trigger


def list_triggers(workspace=None, now=None):
    _enabled()
    now = time.time() if now is None else now
    selected = str(workflow.workspace_path(workspace)) if workspace else None
    result = []
    for path in sorted(_root().glob("*.json")):
        try:
            trigger = _refresh(path.stem, now)
        except (ValueError, OSError, json.JSONDecodeError):
            continue
        if selected is None or trigger["workspace"] == selected:
            result.append({**public(trigger), "invocations": _invocations(trigger["id"])[-20:]})
    return sorted(result, key=lambda item: item["created_at"])


def update_trigger(trigger_id, expected_version, enabled, now=None):
    _enabled()
    now = time.time() if now is None else now
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean.")
    with _locked(trigger_id):
        trigger = _read(trigger_id)
        if type(expected_version) is not int or trigger["version"] != expected_version:
            raise ValueError("Trigger changed; reload it first.")
        trigger["enabled"] = enabled
        trigger["version"] += 1
        trigger["last_tick"] = now
        if enabled:
            trigger["next_due"] = _first_due(trigger, now)           # re-enabling never replays what was missed while paused
        _write(trigger, now)
        return public(trigger)


def delete_trigger(trigger_id, now=None):
    _enabled()
    now = time.time() if now is None else now
    with _locked(trigger_id):
        trigger = _read(trigger_id)
        items = _invocations(trigger_id)
        if any(i["status"] == "started" for i in items):
            raise ValueError("A run started from this trigger is still active; cancel or finish it first.")
        for item in items:
            (_inv_dir(trigger_id) / (item["id"] + ".json")).unlink(missing_ok=True)
        _inv_dir(trigger_id).rmdir()
        _path(trigger_id).unlink()
        (_root() / (trigger_id + ".lock")).unlink(missing_ok=True)
        return {"deleted": trigger["id"], "cancelled_pending": sum(1 for i in items if i["status"] == "pending")}


def fire_external(trigger_id, token, idempotency_key, now=None):
    """Authenticated queueing from outside the app (cron, launchd, a script). Never starts a run."""
    _enabled()
    now = time.time() if now is None else now
    denied = ValueError("Invalid trigger credentials.")
    try:
        trigger_id = _id(trigger_id)
        if not _path(trigger_id).is_file():                           # no lock files for ids that do not exist
            raise denied
        with _locked(trigger_id):
            trigger = _read(trigger_id)
            given = hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) else ""
            if not hmac.compare_digest(given, trigger["token_sha256"]):
                raise denied
            if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
                raise ValueError("idempotency_key must be a string of 8..128 characters.")
            if not trigger["enabled"]:
                raise ValueError("This trigger is paused.")
            invocation = _enqueue(trigger, "ext:" + idempotency_key, "external", now, now)
            _write(trigger, now)
            return invocation
    except ValueError as error:
        if str(error) in ("Trigger not found.", "Invalid trigger or invocation id."):
            raise denied from None
        raise


def _find(invocation_id):
    _id(invocation_id)
    for path in _root().glob("*.inv"):
        candidate = path / (invocation_id + ".json")
        if candidate.is_file():
            return path.name[:-4]
    raise ValueError("Invocation not found.")


def start_invocation(invocation_id, now=None):
    """User-attended start. Validates the task now and returns the exact /api/chat fields to send."""
    _enabled()
    now = time.time() if now is None else now
    trigger_id = _find(invocation_id)
    with _locked(trigger_id):
        trigger = _read(trigger_id)
        invocation = json.loads((_inv_dir(trigger_id) / (invocation_id + ".json")).read_text())
        if invocation["status"] != "pending":
            raise ValueError(f"This invocation is {invocation['status']}; only a pending one can be started.")
        if not trigger["enabled"]:
            raise ValueError("The trigger is paused; resume it before starting its work.")
        try:
            task = workflow.get_task(trigger["task_id"])
            if task["workspace"] != trigger["workspace"]:
                raise ValueError("The task no longer belongs to the trigger's workspace.")
            workflow.validate_session(task)
            if task["usage"]["steps"] >= task["budgets"]["max_steps"]:
                raise ValueError("Task total execution budget exhausted.")
        except ValueError as error:
            invocation.update(status="blocked", seen=False, notice=f"{trigger['name']}: not started — {error}")
            _inv_write(invocation)
            raise ValueError(invocation["notice"]) from None
        invocation.update(status="started", started_at=now, request_key=f"trigger-{invocation_id}")
        _inv_write(invocation)
        return {"invocation": invocation,
                "request": {"workspace": trigger["workspace"], "workflow_task_id": trigger["task_id"], "workflow_mode": task["mode"],
                            "workflow_request_key": invocation["request_key"], "prompt": trigger["prompt"]}}


def cancel_invocation(invocation_id, now=None):
    _enabled()
    trigger_id = _find(invocation_id)
    with _locked(trigger_id):
        invocation = json.loads((_inv_dir(trigger_id) / (invocation_id + ".json")).read_text())
        if invocation["status"] not in ACTIVE:
            raise ValueError(f"This invocation is already {invocation['status']}.")
        if invocation["status"] == "started" and invocation.get("run_id"):
            workflow_runs.store.cancel(invocation["run_id"])
        invocation.update(status="cancelled", seen=True, notice=None)
        _inv_write(invocation)
        return invocation


def acknowledge(invocation_id):
    _enabled()
    trigger_id = _find(invocation_id)
    with _locked(trigger_id):
        invocation = json.loads((_inv_dir(trigger_id) / (invocation_id + ".json")).read_text())
        invocation["seen"] = True
        _inv_write(invocation)
        return invocation


def notifications(now=None):
    """Meaningful, unacknowledged outcomes plus the number of invocations ready to start."""
    now = time.time() if now is None else now
    ready, notices = 0, []
    for trigger in list_triggers(now=now):
        for invocation in trigger["invocations"]:
            if invocation["status"] == "pending":
                ready += 1
            elif invocation["status"] in TERMINAL_NOTICE and not invocation["seen"] and invocation["notice"]:
                notices.append({"id": invocation["id"], "trigger_id": trigger["id"], "trigger": trigger["name"],
                                "status": invocation["status"], "notice": invocation["notice"]})
    return {"ready": ready, "notices": notices}
