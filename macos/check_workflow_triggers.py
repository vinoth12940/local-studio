"""Offline trigger/schedule checks. Time is injected, nothing sleeps, and no model engine is touched.

The invariant under test: firing a trigger only QUEUES an invocation. It never runs work, never widens the task's
permissions, and every delivery, overlap, restart and clock case is reported rather than silently retried.
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ls-trig-", dir="/tmp")).resolve()
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")
os.environ["HF_HUB_CACHE"] = str(TMP / "hub")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from studio.main import app
from studio.services import triggers, workflow, workflow_runs
from studio.storage.settings import load_settings, save_settings

T0 = datetime(2026, 1, 14, 8, 0, 0).timestamp()
HOUR = 3600
_count = [0]


def settings(**values):
    current = load_settings()
    current["harness"].update(values)
    save_settings(current)


def fails(fn, text):
    try:
        fn()
    except (ValueError, OSError) as error:
        assert text.lower() in str(error).lower(), (text, str(error))
    else:
        raise AssertionError("Expected rejection: " + text)


def setup(schedule=None, policy="skip", mode="act"):
    _count[0] += 1
    ws = TMP / f"ws{_count[0]}"
    ws.mkdir()
    settings(workflow=True, durable_runs=True, triggers=True)
    task = workflow.create_task(str(ws), "Keep deps fresh", "Report outdated packages", "Check then summarize", mode=mode)
    trigger, token = triggers.create_trigger(str(ws), task["id"], f"nightly-{_count[0]}", "Check for outdated packages.",
                                              schedule, policy, now=T0)
    return ws, task, trigger, token


def tick(trigger_id, now):
    return next(t for t in triggers.list_triggers(now=now) if t["id"] == trigger_id)


def statuses(trigger):
    return [i["status"] for i in trigger["invocations"]]


def test_disabled_by_default_and_creation_is_validated():
    ws = TMP / "off"; ws.mkdir()
    settings(workflow=True, durable_runs=True, triggers=False)
    fails(lambda: triggers.list_triggers(), "triggers")
    ws, task, trigger, token = setup({"type": "interval", "every_seconds": HOUR})
    assert "token_sha256" not in trigger and token.startswith("lst_") and len(token) > 30
    assert trigger["permissions"] == {"approvals": "always_required", "from": "task"} and trigger["overlap"] == "one_active"
    stored = json.loads((triggers._root() / (trigger["id"] + ".json")).read_text())
    assert token not in json.dumps(stored) and len(stored["token_sha256"]) == 64, "only a hash is persisted"
    assert all("token" not in json.dumps(t) for t in triggers.list_triggers(now=T0))
    for bad in ({"type": "interval", "every_seconds": 30}, {"type": "interval", "every_seconds": HOUR, "x": 1},
                {"type": "daily", "at": "25:00"}, {"type": "cron", "expr": "* * * * *"}, "hourly"):
        fails(lambda: triggers.create_trigger(str(ws), task["id"], "n", "p", bad, now=T0), "schedule")
    other = TMP / "other"; other.mkdir()
    fails(lambda: triggers.create_trigger(str(other), task["id"], "n", "p", now=T0), "another workspace")
    fails(lambda: triggers.create_trigger(str(ws), task["id"], "", "p", now=T0), "name")
    fails(lambda: triggers.create_trigger(str(ws), task["id"], "n", "  ", now=T0), "instruction")
    fails(lambda: triggers.create_trigger(str(ws), task["id"], "n", "p", None, "always", now=T0), "missed_policy")
    settings(durable_runs=False)
    fails(lambda: triggers.create_trigger(str(ws), task["id"], "n", "p", now=T0), "durable runs")
    settings(durable_runs=True)


def test_interval_schedule_fires_once_per_slot_and_queues_without_running():
    ws, task, trigger, _ = setup({"type": "interval", "every_seconds": HOUR})
    assert trigger["next_due"] == T0 + HOUR
    assert tick(trigger["id"], T0 + HOUR - 1)["invocations"] == []
    fired = tick(trigger["id"], T0 + HOUR + 5)
    assert statuses(fired) == ["pending"] and fired["counters"]["fired"] == 1 and fired["next_due"] == T0 + 2 * HOUR
    again = tick(trigger["id"], T0 + HOUR + 6)                         # a second poll in the same slot is a no-op
    assert statuses(again) == ["pending"] and again["counters"]["fired"] == 1
    assert workflow.get_task(task["id"])["usage"]["steps"] == 0 and workflow.get_task(task["id"])["state"] == "ready"
    assert not list(workflow_runs.store.root.glob("*.json")) or all(workflow_runs.store.get(p.stem, recover=False)["metadata"].get("task_id") != task["id"]
                                                                   for p in workflow_runs.store.root.glob("*.json")), "firing must not create a run"


def test_overlapping_slots_are_skipped_and_reported_not_stacked():
    ws, task, trigger, _ = setup({"type": "interval", "every_seconds": HOUR})
    tick(trigger["id"], T0 + HOUR + 1)
    later = tick(trigger["id"], T0 + 2 * HOUR + 1)
    assert statuses(later) == ["pending", "skipped"] and later["counters"]["skipped_overlap"] == 1
    assert "overlap" in later["invocations"][1]["reason"] and later["invocations"][1]["seen"] is True
    assert sum(1 for i in later["invocations"] if i["status"] in triggers.ACTIVE) == 1


def test_missed_run_policies_skip_or_catch_up_exactly_once():
    ws, task, skip, _ = setup({"type": "interval", "every_seconds": HOUR}, "skip")
    late = tick(skip["id"], T0 + 5 * HOUR + 1000)                      # asleep for 5 slots, now 1000s past the fifth
    assert late["invocations"] == [] and late["counters"]["missed"] == 5 and late["next_due"] == T0 + 6 * HOUR
    ontime = tick(skip["id"], T0 + 6 * HOUR + 30)
    assert statuses(ontime) == ["pending"] and ontime["counters"]["missed"] == 5
    ws, task, catch, _ = setup({"type": "interval", "every_seconds": HOUR}, "queue_one")
    late = tick(catch["id"], T0 + 5 * HOUR + 1000)
    assert statuses(late) == ["pending"] and late["counters"]["missed"] == 4 and late["counters"]["fired"] == 1
    assert late["invocations"][0]["due_at"] == T0 + 5 * HOUR, "one catch-up for the newest missed slot only"


def test_clock_regression_never_fires_and_reanchors():
    ws, task, trigger, _ = setup({"type": "interval", "every_seconds": HOUR})
    tick(trigger["id"], T0 + HOUR + 1)
    back = tick(trigger["id"], T0 + 100)                               # an hour of clock went backwards
    assert back["counters"]["clock_regressions"] == 1 and statuses(back) == ["pending"] and back["counters"]["fired"] == 1
    assert back["next_due"] > T0 + 100
    cancel = back["invocations"][0]["id"]
    triggers.cancel_invocation(cancel)
    assert statuses(tick(trigger["id"], back["next_due"] - 1)) == ["cancelled"]
    assert statuses(tick(trigger["id"], back["next_due"] + 1)) == ["cancelled", "pending"]


def test_daily_schedule_uses_local_time_slots():
    ws, task, trigger, _ = setup({"type": "daily", "at": "09:30"})
    nine_thirty = datetime(2026, 1, 14, 9, 30).timestamp()
    assert trigger["next_due"] == nine_thirty
    assert tick(trigger["id"], nine_thirty - 1)["invocations"] == []
    fired = tick(trigger["id"], nine_thirty + 20)
    assert statuses(fired) == ["pending"] and fired["next_due"] == datetime(2026, 1, 15, 9, 30).timestamp()
    skipped_days = tick(trigger["id"], datetime(2026, 1, 18, 12, 0).timestamp())
    assert skipped_days["counters"]["missed"] == 4 and statuses(skipped_days) == ["pending"], "skip policy drops stale daily slots"


def test_external_trigger_is_authenticated_idempotent_and_never_executes():
    ws, task, trigger, token = setup()
    key = "delivery-0001-abc"
    fails(lambda: triggers.fire_external(trigger["id"], "lst_wrong", key, now=T0), "invalid trigger credentials")
    fails(lambda: triggers.fire_external(trigger["id"], None, key, now=T0), "invalid trigger credentials")
    fails(lambda: triggers.fire_external("0" * 32, token, key, now=T0), "invalid trigger credentials")
    fails(lambda: triggers.fire_external("../escape", token, key, now=T0), "invalid trigger credentials")
    assert not (triggers._root() / ("0" * 32 + ".lock")).exists(), "unauthenticated ids must not create files"
    fails(lambda: triggers.fire_external(trigger["id"], token, "short", now=T0), "idempotency_key")
    first = triggers.fire_external(trigger["id"], token, key, now=T0)
    redelivered = triggers.fire_external(trigger["id"], token, key, now=T0 + 5)
    assert first["id"] == redelivered["id"] and first["status"] == "pending"
    overlapping = triggers.fire_external(trigger["id"], token, "delivery-0002-xyz", now=T0 + 6)
    assert overlapping["status"] == "skipped" and "overlap" in overlapping["reason"]
    state = tick(trigger["id"], T0 + 7)
    assert state["counters"]["deduplicated"] == 1 and state["counters"]["fired"] == 1 and state["counters"]["skipped_overlap"] == 1
    triggers.update_trigger(trigger["id"], state["version"], False, now=T0 + 8)
    fails(lambda: triggers.fire_external(trigger["id"], token, "delivery-0003-xyz", now=T0 + 9), "paused")


def test_starting_requires_a_person_and_revalidates_the_task():
    ws, task, trigger, token = setup(mode="auto")
    invocation = triggers.fire_external(trigger["id"], token, "start-me-0001", now=T0)
    started = triggers.start_invocation(invocation["id"], now=T0 + 1)
    request = started["request"]
    assert request == {"workspace": str(ws), "workflow_task_id": task["id"], "workflow_mode": "auto",
                       "workflow_request_key": "trigger-" + invocation["id"], "prompt": "Check for outdated packages."}
    assert "permission_mode" not in request, "a trigger never pre-approves anything"
    fails(lambda: triggers.start_invocation(invocation["id"], now=T0 + 2), "started")
    # paused trigger, paused task and exhausted budgets each refuse; the reason is recorded, nothing runs
    ws, task, trigger, token = setup()
    first = triggers.fire_external(trigger["id"], token, "pause-it-0001", now=T0)
    state = tick(trigger["id"], T0 + 1)
    triggers.update_trigger(trigger["id"], state["version"], False, now=T0 + 2)
    fails(lambda: triggers.start_invocation(first["id"], now=T0 + 3), "paused")
    triggers.update_trigger(trigger["id"], state["version"] + 1, True, now=T0 + 4)
    workflow.update_task(task["id"], task["version"], {"state": "paused"})
    fails(lambda: triggers.start_invocation(first["id"], now=T0 + 5), "paused")
    blocked = tick(trigger["id"], T0 + 6)["invocations"][0]
    assert blocked["status"] == "blocked" and "not started" in blocked["notice"] and blocked["seen"] is False
    ws, task, trigger, token = setup()
    first = triggers.fire_external(trigger["id"], token, "budget-0001", now=T0)
    with workflow._locked(task["id"]):
        record = workflow._read(task["id"])
        record["usage"]["steps"] = record["budgets"]["max_steps"]
        workflow._write(record)
    fails(lambda: triggers.start_invocation(first["id"], now=T0 + 1), "budget exhausted")
    assert any(n["status"] == "blocked" for n in triggers.notifications(now=T0 + 2)["notices"])


def test_run_outcomes_become_honest_notifications():
    outcomes = {"verified_complete": "completed", "needs_user": "needs_user", "incomplete": "failed", "cancelled": "cancelled"}
    for outcome, expected in outcomes.items():
        ws, task, trigger, token = setup()
        invocation = triggers.fire_external(trigger["id"], token, f"outcome-{outcome}", now=T0)
        started = triggers.start_invocation(invocation["id"], now=T0 + 1)
        running = tick(trigger["id"], T0 + 2)["invocations"][0]
        assert running["status"] == "started" and running["run_id"] is None
        record, _ = workflow_runs.store.create(str(ws), "policy", workflow_runs.workspace_fingerprint(ws), [{"role": "user", "content": "go"}],
                                               request_key=started["request"]["workflow_request_key"], metadata={"task_id": task["id"]})
        assert tick(trigger["id"], T0 + 3)["invocations"][0]["run_id"] == record["id"]
        workflow_runs.store.claim(record["id"])
        assert tick(trigger["id"], T0 + 4)["invocations"][0]["status"] == "started", "a live run is not finished"
        workflow_runs.store.finish(record["id"], outcome)
        done = tick(trigger["id"], T0 + 5)["invocations"][0]
        assert done["status"] == expected, (outcome, done)
        notes = triggers.notifications(now=T0 + 6)
        if expected == "cancelled":
            assert not any(n["id"] == invocation["id"] for n in notes["notices"]), "a user-requested cancel is not news"
        else:
            note = next(n for n in notes["notices"] if n["id"] == invocation["id"])
            assert note["status"] == expected and trigger["name"] in note["notice"]
            if expected == "needs_user":
                assert "attention" in note["notice"]
            triggers.acknowledge(invocation["id"])
            assert not any(n["id"] == invocation["id"] for n in triggers.notifications(now=T0 + 7)["notices"])
    # a run left with an unknown tool outcome needs a person, never a retry
    ws, task, trigger, token = setup()
    invocation = triggers.fire_external(trigger["id"], token, "unknown-0001", now=T0)
    key = triggers.start_invocation(invocation["id"], now=T0 + 1)["request"]["workflow_request_key"]
    record, _ = workflow_runs.store.create(str(ws), "policy", workflow_runs.workspace_fingerprint(ws), [{"role": "user", "content": "go"}], request_key=key)
    workflow_runs.store.claim(record["id"])
    workflow_runs.store.dispatch(record["id"], "1-0", "run_shell", {"command": "x"})
    workflow_runs.store.finish(record["id"], "failed")
    assert tick(trigger["id"], T0 + 2)["invocations"][0]["status"] == "needs_user"


def test_start_interrupted_before_a_run_is_reported_and_not_replayed():
    ws, task, trigger, token = setup()
    invocation = triggers.fire_external(trigger["id"], token, "crash-0001", now=T0)
    triggers.start_invocation(invocation["id"], now=T0 + 1)            # the app dies here: no run was ever recorded
    assert tick(trigger["id"], T0 + 1 + triggers.START_GRACE_SECONDS - 1)["invocations"][0]["status"] == "started"
    gone = tick(trigger["id"], T0 + 2 + triggers.START_GRACE_SECONDS)["invocations"][0]
    assert gone["status"] == "interrupted" and "Nothing was run again" in gone["notice"]
    fails(lambda: triggers.start_invocation(invocation["id"], now=T0 + 900), "interrupted")
    assert triggers._run_for("trigger-" + invocation["id"]) is None, "no automatic re-dispatch"
    assert any(n["status"] == "interrupted" for n in triggers.notifications(now=T0 + 901)["notices"])


def test_cancel_propagates_to_the_run_and_delete_is_safe():
    ws, task, trigger, token = setup()
    invocation = triggers.fire_external(trigger["id"], token, "cancel-0001", now=T0)
    key = triggers.start_invocation(invocation["id"], now=T0 + 1)["request"]["workflow_request_key"]
    record, _ = workflow_runs.store.create(str(ws), "policy", workflow_runs.workspace_fingerprint(ws), [{"role": "user", "content": "go"}], request_key=key)
    workflow_runs.store.claim(record["id"])
    tick(trigger["id"], T0 + 2)
    fails(lambda: triggers.delete_trigger(trigger["id"], now=T0 + 3), "still active")
    cancelled = triggers.cancel_invocation(invocation["id"])
    assert cancelled["status"] == "cancelled" and workflow_runs.store.get(record["id"], recover=False)["cancel_requested"] is True
    fails(lambda: triggers.cancel_invocation(invocation["id"]), "already cancelled")
    pending = triggers.fire_external(trigger["id"], token, "cancel-0002", now=T0 + 4)
    result = triggers.delete_trigger(trigger["id"], now=T0 + 5)
    assert result["cancelled_pending"] == 1 and not (triggers._root() / (trigger["id"] + ".json")).exists()
    assert not (triggers._root() / (trigger["id"] + ".inv")).exists() and not (triggers._root() / (trigger["id"] + ".lock")).exists()
    fails(lambda: triggers.fire_external(trigger["id"], token, "cancel-0003", now=T0 + 6), "invalid trigger credentials")
    workflow_runs.store.finish(record["id"], "cancelled")


def test_pausing_and_reenabling_never_replays_missed_slots():
    ws, task, trigger, _ = setup({"type": "interval", "every_seconds": HOUR}, "queue_one")
    paused = triggers.update_trigger(trigger["id"], trigger["version"], False, now=T0 + 10)
    assert tick(trigger["id"], T0 + 5 * HOUR)["invocations"] == [], "a paused trigger never fires"
    fails(lambda: triggers.update_trigger(trigger["id"], trigger["version"], True, now=T0 + 5 * HOUR), "changed")
    triggers.update_trigger(trigger["id"], paused["version"], True, now=T0 + 5 * HOUR)
    after = tick(trigger["id"], T0 + 5 * HOUR + 1)
    assert after["invocations"] == [] and after["next_due"] > T0 + 5 * HOUR


def test_http_contract_authentication_and_unknown_fields():
    ws, task, trigger, token = setup()
    client = TestClient(app, base_url="http://127.0.0.1:7860")
    body = {"trigger_id": trigger["id"], "idempotency_key": "http-delivery-001"}
    assert client.post("/api/agent/trigger", json=body).status_code == 401
    assert client.post("/api/agent/trigger", json=body, headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.post("/api/agent/trigger", json=body, headers={"Authorization": "Bearer lst_wrong"}).status_code == 401
    assert client.post("/api/agent/trigger", json={**body, "prompt": "inject"}, headers={"Authorization": "Bearer " + token}).status_code == 400
    good = client.post("/api/agent/trigger", json=body, headers={"Authorization": "Bearer " + token})
    assert good.status_code == 200 and good.json()["runs_unattended"] is False and good.json()["invocation"]["status"] == "pending"
    again = client.post("/api/agent/trigger", json=body, headers={"Authorization": "Bearer " + token}).json()
    assert again["invocation"]["id"] == good.json()["invocation"]["id"]
    listed = client.get("/api/workflow/triggers", params={"workspace": str(ws)}).json()["triggers"]
    assert listed and "token" not in json.dumps(listed) and listed[0]["invocations"][0]["id"] == good.json()["invocation"]["id"]
    assert client.post("/api/workflow/invocations/" + good.json()["invocation"]["id"] + "/start").json()["request"]["workflow_task_id"] == task["id"]
    assert client.post("/api/workflow/invocations/" + good.json()["invocation"]["id"] + "/start").status_code == 409
    assert client.patch(f"/api/workflow/triggers/{trigger['id']}", json={"enabled": False, "extra": 1}).status_code == 400
    created = client.post("/api/workflow/triggers", json={"workspace": str(ws), "task_id": task["id"], "name": "api", "prompt": "go",
                                                          "schedule": {"type": "interval", "every_seconds": 600}}).json()
    assert created["token"].startswith("lst_") and "token_sha256" not in json.dumps(created)


TESTS = (test_disabled_by_default_and_creation_is_validated, test_interval_schedule_fires_once_per_slot_and_queues_without_running,
         test_overlapping_slots_are_skipped_and_reported_not_stacked, test_missed_run_policies_skip_or_catch_up_exactly_once,
         test_clock_regression_never_fires_and_reanchors, test_daily_schedule_uses_local_time_slots,
         test_external_trigger_is_authenticated_idempotent_and_never_executes, test_starting_requires_a_person_and_revalidates_the_task,
         test_run_outcomes_become_honest_notifications, test_start_interrupted_before_a_run_is_reported_and_not_replayed,
         test_cancel_propagates_to_the_run_and_delete_is_safe, test_pausing_and_reenabling_never_replays_missed_slots,
         test_http_contract_authentication_and_unknown_fields)

if __name__ == "__main__":
    for function in TESTS:
        function()
        print("   ok " + function.__name__)
    print("   workflow trigger checks OK")
