"""Offline checks for macos/evals. No model engine is started, stopped or contacted.

Every task ships a reference script (must pass) and a negative script (must fail), so a green run here proves each task
is solvable, its objective checks discriminate, and the runner reports blocked/skipped runs honestly.
"""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "evals"))
sys.path.insert(0, str(HERE.parent))
TMP = Path(tempfile.mkdtemp(prefix="ls-evals-", dir="/tmp")).resolve()
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")
os.environ["HF_HUB_CACHE"] = str(TMP / "hub")
os.environ.pop("LOCAL_STUDIO_EVAL_ATTACH", None)

import evalkit  # noqa: E402

_session = []


def session():
    if not _session:
        from studio.storage.settings import load_settings
        _session.append(evalkit.Session(load_settings()["chat_model"]))
    return _session[0]


def rejects(task, text):
    try:
        evalkit.validate_task(task, "t")
    except evalkit.TaskError as error:
        assert text in str(error), (text, str(error))
    else:
        raise AssertionError("expected rejection: " + text)


def test_tasks_validate_and_cover_the_required_categories():
    tasks = evalkit.load_tasks()
    categories = {t["category"] for t in tasks}
    assert {"coding", "repair", "refusal_approval", "compaction", "recovery"} <= categories, categories
    assert len({t["id"] for t in tasks}) == len(tasks) >= 8
    base = copy.deepcopy(next(t for t in tasks if t["id"] == "coding-add-function"))
    rejects({**base, "extra": 1}, "unknown fields")
    rejects({**base, "id": "Bad_Id"}, "kebab")
    rejects({**base, "revision": 0}, "revision")
    rejects({**base, "category": "magic"}, "category")
    rejects({**base, "fixture": "../calc"}, "inside the task workspace")
    rejects({**base, "fixture": "nonexistent"}, "does not exist")
    rejects({**base, "approvals": "yolo"}, "approvals")
    rejects({**base, "budgets": {"max_tool_calls": 999, "timeout_seconds": 60}}, "budgets")
    rejects({**base, "outcome": [{"type": "shell", "cmd": "true"}]}, "invalid outcome check")
    rejects({**base, "outcome": [{"type": "file_contains", "path": "/etc/passwd", "text": "x"}]}, "inside the task workspace")
    rejects({**base, "requires": ["gpu"]}, "requires")
    rejects({**base, "flags": {"coding_workflow": "yes"}}, "flags")
    assert evalkit.task_set_revision(tasks) == evalkit.task_set_revision(list(reversed(tasks)))
    changed = copy.deepcopy(tasks); changed[0]["revision"] += 1
    assert evalkit.task_set_revision(changed) != evalkit.task_set_revision(tasks), "revision must follow task content"


def test_fixtures_are_never_modified_by_a_run():
    task = next(t for t in evalkit.load_tasks() if t["id"] == "coding-add-function")
    before = evalkit.task_set_revision([task])
    evalkit.run_task(task, session(), TMP, evalkit.load_script(task["id"], "reference"))
    assert evalkit.task_set_revision([task]) == before, "a run must work in an isolated copy of its fixture"


def test_every_reference_script_passes_and_every_negative_script_fails():
    for task in evalkit.load_tasks():
        reference = evalkit.run_task(task, session(), TMP, evalkit.load_script(task["id"], "reference"))
        assert reference["status"] == "passed", (task["id"], reference["status"], reference["reason"], reference["checks"])
        negative = evalkit.run_task(task, session(), TMP, evalkit.load_script(task["id"], "negative"))
        assert negative["status"] == "failed", (task["id"], "the checks do not discriminate", negative["status"], negative["reason"])
        assert negative["reason"], task["id"]


def test_approval_policies_are_enforced_by_the_driver():
    tasks = {t["id"]: t for t in evalkit.load_tasks()}
    denied = evalkit.run_task(tasks["approval-denied-shell"], session(), TMP, evalkit.load_script("approval-denied-shell", "reference"))
    assert denied["approvals"] >= 1 and (Path(denied["workspace"]) / "dist" / "out.bin").exists(), denied
    solved = evalkit.run_task(tasks["coding-add-function"], session(), TMP, evalkit.load_script("coding-add-function", "reference"))
    assert "def multiply" in (Path(solved["workspace"]) / "calc.py").read_text()
    leak = evalkit.run_task(tasks["refusal-credential-file"], session(), TMP, evalkit.load_script("refusal-credential-file", "reference"))
    assert leak["status"] == "passed" and "LS_EVAL_CANARY" not in leak["final_text"], leak


def test_tool_budget_overruns_fail_even_when_outcomes_hold():
    task = next(t for t in evalkit.load_tasks() if t["id"] == "approval-edit-declined")
    record = evalkit.run_task(task, session(), TMP, evalkit.load_script(task["id"], "negative"))
    assert record["status"] == "failed" and record.get("budget_exceeded") and "budget" in record["reason"], record
    assert (Path(record["workspace"]) / "notes.txt").read_text().startswith("Title: Draft"), "the declined edit must not apply"


def test_engine_tasks_are_skipped_in_scripted_mode_and_never_counted_as_successes():
    task = copy.deepcopy(next(t for t in evalkit.load_tasks() if t["id"] == "coding-add-function")); task["requires"] = ["engine"]
    record = evalkit.run_task(task, session(), TMP, [{"text": "x"}])
    assert record["status"] == "skipped"
    records = [{"task": "a", "status": "passed", "reason": None, "latency_seconds": 1.0, "tool_failures": 0},
               {"task": "a", "status": "failed", "reason": "tests", "latency_seconds": 3.0, "tool_failures": 2},
               {"task": "a", "status": "blocked", "reason": "engine down", "latency_seconds": None, "tool_failures": 0},
               {"task": "a", "status": "skipped", "reason": "n/a", "latency_seconds": None, "tool_failures": 0},
               {"task": "b", "status": "blocked", "reason": "engine down", "latency_seconds": None, "tool_failures": 0}]
    tasks = [{"id": "a", "revision": 1, "category": "coding"}, {"id": "b", "revision": 1, "category": "repair"}]
    summary = evalkit.aggregate(records, tasks)
    assert summary["tasks"]["a"]["pass_rate"] == 0.5 and summary["tasks"]["a"]["measured"] == 2
    assert summary["tasks"]["b"]["pass_rate"] is None and summary["tasks"]["b"]["measured"] == 0
    assert summary["passed"] == 1 and summary["measured"] == 2 and summary["excluded"] == 3 and summary["pass_rate"] == 0.5
    assert summary["tasks"]["a"]["median_latency_seconds"] == 2.0
    assert [e["reason"] for e in summary["tasks"]["a"]["excluded"]] == ["engine down", "n/a"]
    assert "no measurement" in evalkit.render_markdown({"summary": {**summary, "pass_rate": None, "passed": 0, "measured": 0},
                                                         "environment": {"model": {"id": "m"}, "engine_mode": "text", "scripted": False},
                                                         "task_set_revision": "0" * 64})


def test_baseline_comparison_flags_regressions_and_refuses_scripted_reports():
    def report(rates, scripted=False):
        tasks = {k: {"pass_rate": v} for k, v in rates.items()}
        measured = [v for v in rates.values() if v is not None]
        return {"environment": {"scripted": scripted}, "summary": {"tasks": tasks, "pass_rate": sum(measured) / len(measured) if measured else None}}
    baseline = report({"a": 1.0, "b": 0.8, "c": None})
    assert evalkit.compare(report({"a": 1.0, "b": 0.8, "c": None}), baseline) == []
    assert evalkit.compare(report({"a": 0.9, "b": 0.7, "c": None}), baseline, 0.15) == []         # -0.1 each: within tolerance
    assert evalkit.compare(report({"a": 1.0, "b": 0.8, "c": 0.0}), baseline, 0.15), "an unbaselined task still drags the overall rate"
    changed = report({"a": 1.0, "b": 0.8, "c": None})
    changed["summary"]["tasks"]["a"]["revision"] = 2
    for r in (baseline, changed): r["summary"]["tasks"]["a"].setdefault("revision", 1); r["summary"]["tasks"]["b"].setdefault("revision", 1)
    assert any("task revision 1 in the baseline vs 2 now" in p for p in evalkit.compare(changed, baseline)), "different task revisions are not comparable"
    regressions = evalkit.compare(report({"a": 0.6, "b": None}), baseline, 0.15)
    assert any("a: pass rate fell" in p for p in regressions) and any("b: no measured runs" in p for p in regressions), regressions
    for bad in (lambda: evalkit.compare(report({"a": 1.0}, scripted=True), baseline), lambda: evalkit.compare(baseline, report({"a": 1.0}, scripted=True))):
        try:
            bad()
        except ValueError as error:
            assert "scripted" in str(error)
        else:
            raise AssertionError("a scripted report must never be compared")


def test_attach_mode_never_stops_or_starts_an_engine_and_reports_blocked():
    from studio.services import engines, models
    saved = (engines._stop_engine, engines._start_engine, engines._proc_running, engines._engine_ready, models.model_installed)
    models.model_installed = lambda entry: True

    def forbidden(*args, **kwargs):
        raise AssertionError("attach mode touched an engine process")

    engines._stop_engine = engines._start_engine = forbidden                    # _proc_running is a read-only probe; stop/start are the harm
    os.environ["LOCAL_STUDIO_EVAL_ATTACH"] = "1"
    try:
        model = {"id": "m", "name": "Model"}
        engines._engine_ready = lambda mode: True
        assert engines.ensure_engine("text", model) is False
        engines._engine_ready = lambda mode: False
        try:
            engines.ensure_engine("text", model)
        except RuntimeError as error:
            assert "Eval attach mode" in str(error)
        else:
            raise AssertionError("an unavailable engine must fail, not be started")
        task = next(t for t in evalkit.load_tasks() if t["id"] == "coding-add-function")
        record = evalkit.run_task(task, session(), TMP)                      # no script: the real code path, no engine
        assert record["status"] == "blocked" and "Eval attach mode" in record["reason"], record
        report = evalkit.aggregate([record], [task])
        assert report["passed"] == 0 and report["measured"] == 0 and report["excluded"] == 1
    finally:
        os.environ.pop("LOCAL_STUDIO_EVAL_ATTACH", None)
        engines._stop_engine, engines._start_engine, engines._proc_running, engines._engine_ready, models.model_installed = saved


def test_attached_model_must_match_the_engine_or_be_flagged():
    entry = {"id": "qwen3.8-27b", "name": "Qwen3.8 27B", "repo": "mlx-community/Qwen3.8-27B-4bit", "backend": "mlx"}
    assert evalkit.model_match(entry, "mlx-community/Qwen3.8-27B-4bit") == "verified"
    assert evalkit.model_match(entry, "/Users/me/.cache/hf/Qwen3.8-27B-4bit") == "verified"
    assert evalkit.model_match(entry, "mlx-community/Llama-4-8B-4bit") == "mismatch"
    assert evalkit.model_match(entry, None) == "unknown"
    gguf = {"id": "lfm", "name": "LFM2.5", "file": "LFM2.5-24B-A2B-Q4_K_M.gguf", "backend": "gguf"}
    assert evalkit.model_match(gguf, "LFM2.5-24B-A2B-Q4_K_M.gguf") == "verified" and evalkit.model_match(gguf, "other.gguf") == "mismatch"
    env = evalkit.environment("m", "text", {}, False, "x", None, "mismatch")
    assert env["model_check"] == "mismatch" and evalkit.environment("m", "text", {}, True)["model_check"] == "n/a"


def test_recovery_task_interrupts_a_real_run_resumes_it_and_catches_replays():
    task = next(t for t in evalkit.load_tasks() if t["id"] == "recovery-resume-without-replay")
    good = evalkit.run_task(task, session(), TMP, evalkit.load_script(task["id"], "reference"))
    assert good["status"] == "passed", good
    assert good["recovery"]["interrupted_after_steps"] >= 1 and good["recovery"]["phase_after_interrupt"] in ("interrupted", "cancelled", "incomplete")
    log = (Path(good["workspace"]) / "log.txt").read_text().split()
    assert log == ["milestone-1", "milestone-2", "milestone-3"], log
    bad = evalkit.run_task(task, session(), TMP, evalkit.load_script(task["id"], "negative"))
    assert bad["status"] == "failed" and "replay" in bad["reason"], bad
    assert (Path(bad["workspace"]) / "log.txt").read_text().split() == ["milestone-1", "milestone-2"], "the harness must have refused the replay"
    altered = [{"tool": "run_shell", "args": {"command": "echo milestone-1 >> log.txt"}}, {"tool": "run_shell", "args": {"command": "echo milestone-2 >> log.txt", }, "delay": 0.3},
               {"tool": "run_shell", "args": {"command": "echo  milestone-1 >> log.txt"}}, {"tool": "run_shell", "args": {"command": "echo milestone-3 >> log.txt"}}, {"text": "Done."}]
    sneaky = evalkit.run_task(task, session(), TMP, altered)                                  # a trivially altered replay is not refused
    assert sneaky["status"] == "failed" and any(c["check"]["type"] == "file_line_count" and not c["passed"] for c in sneaky["checks"]), sneaky
    quick = evalkit.run_task(task, session(), TMP, [{"text": "Nothing to do."}])
    assert quick["status"] == "skipped" and "nothing to recover" in quick["reason"], quick
    rejects({**task, "recovery": {"interrupt_after_tool_calls": 0}}, "recovery needs")
    rejects({**task, "flags": {}}, "durable_runs")
    rejects({**task, "approvals": "deny_all"}, "durable_runs")
    without = {k: v for k, v in task.items() if k != "recovery"}
    rejects(without, "recovery block")


def test_runtime_versions_are_read_from_the_engine_environment_or_reported_unavailable():
    fake = TMP / "fake-python"
    fake.write_text('#!/bin/sh\necho \'{"mlx": "9.9.9", "mlx-lm": "1.0", "mlx-vlm": "unavailable", "mlx-dspark": "0.1"}\'\n'); fake.chmod(0o755)
    assert evalkit.runtime_versions("text", python=fake)["mlx"] == "9.9.9"
    missing = evalkit.runtime_versions("text", python=TMP / "no-such-python")
    assert set(missing.values()) == {"unavailable"} and "mlx-dspark" in missing
    server = TMP / "fake-llama"; server.write_text("#!/bin/sh\necho 'version: 4567 (abc1234)'\n"); server.chmod(0o755)
    assert evalkit.runtime_versions("gguf", llama_server=server) == {"llama-server": "version: 4567 (abc1234)"}
    assert evalkit.runtime_versions("gguf", llama_server=TMP / "none") == {"llama-server": "unavailable"}
    assert evalkit.environment("m", "text", {}, True)["runtime_versions"] == {} and "runtime_versions" in evalkit.environment("m", "text", {}, False, None, None, None, {"mlx": "1"})


def test_a_model_that_keeps_retrying_after_refusals_fails_it_does_not_error():
    """The first real baseline labelled the harness's tool-call-limit stop 'errored'; it is the behavior under test, so it is a failure."""
    task = next(t for t in evalkit.load_tasks() if t["id"] == "approval-denied-shell")
    turns = [{"tool": "run_shell", "args": {"command": "rm -r dist"}} for _ in range(16)] + [{"text": "Giving up."}]
    record = evalkit.run_task(task, session(), TMP, turns)
    assert record["status"] == "failed" and record.get("tool_limit_stop") and "tool-call limit" in record["reason"], record
    assert (Path(record["workspace"]) / "dist" / "out.bin").exists(), "the declined command must not have run"
    summary = evalkit.aggregate([record], [task])
    assert summary["tasks"][task["id"]]["failed"] == 1 and summary["tasks"][task["id"]]["errored"] == 0


def test_sessions_refuse_the_live_app_state():
    from studio import config
    saved = config.STATE
    config.STATE = Path.home() / "Library" / "Application Support" / "Local Studio"
    try:
        evalkit.Session("x")
    except RuntimeError as error:
        assert "Refusing" in str(error)
    else:
        raise AssertionError("evaluation must never run against the live STATE")
    finally:
        config.STATE = saved


def test_reports_are_reproducible_records():
    tasks = evalkit.load_tasks(["coding-add-function"])
    record = evalkit.run_task(tasks[0], session(), TMP, evalkit.load_script(tasks[0]["id"], "reference"))
    env = evalkit.environment("m", "text", {"coding_workflow": True}, True, None, {"seed": 7, "temperature": 0.2})
    assert env["seed"] == 7 and env["sampling"] == {"seed": 7, "temperature": 0.2} and "not verified" in env["seed_note"]
    report = evalkit.build_report(tasks, [record], env, 1, "start", "end")
    for key in ("model", "engine_mode", "scripted", "app_version", "git", "python", "packages", "hardware", "harness_flags", "seed", "seed_note"):
        assert key in env, key
    assert report["task_set_revision"] == evalkit.task_set_revision(tasks) and report["runs"][0]["revision"] == 1
    path = evalkit.write_report(TMP / "out", report, [record])
    assert json.loads(path.read_text())["summary"]["passed"] == 1
    assert (TMP / "out" / "report.md").read_text().startswith("# Agent evaluation report")
    saved = list((TMP / "out" / "runs").glob("*-coding-add-function-passed/workspace/calc.py"))
    assert saved and "def multiply" in saved[0].read_text(), "scored artifacts are retained"


TESTS = (test_tasks_validate_and_cover_the_required_categories, test_fixtures_are_never_modified_by_a_run,
         test_every_reference_script_passes_and_every_negative_script_fails, test_approval_policies_are_enforced_by_the_driver,
         test_tool_budget_overruns_fail_even_when_outcomes_hold, test_engine_tasks_are_skipped_in_scripted_mode_and_never_counted_as_successes,
         test_baseline_comparison_flags_regressions_and_refuses_scripted_reports,
         test_attach_mode_never_stops_or_starts_an_engine_and_reports_blocked, test_attached_model_must_match_the_engine_or_be_flagged,
         test_recovery_task_interrupts_a_real_run_resumes_it_and_catches_replays, test_a_model_that_keeps_retrying_after_refusals_fails_it_does_not_error, test_runtime_versions_are_read_from_the_engine_environment_or_reported_unavailable, test_sessions_refuse_the_live_app_state,
         test_reports_are_reproducible_records)

if __name__ == "__main__":
    for function in TESTS:
        function()
        print("   ok " + function.__name__)
    print("   eval checks OK")
