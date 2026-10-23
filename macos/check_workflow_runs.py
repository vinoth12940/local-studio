"""Offline durable-run/verification regression suite. Never starts model engines.

Crash cases run fresh Python processes and actually exit between journal points.
Chat cases reuse the isolated STATE and synthetic engine fixture from check_agent.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_agent as fixture  # owns an isolated short STATE and synthetic engines

from studio.services import workflow_runs as runs
from studio.services.verification import VerificationGate


class DurableRuns(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="ls-workflow-")
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "source.py").write_text("value = 1\n")
        self.store = runs.RunStore(self.root / "runs")
        self.policy = "trusted-policy-v1"

    def tearDown(self):
        for lease in self.store._leases.values():
            lease.close()
        self.temp.cleanup()

    def create(self, key=None, messages=None):
        return self.store.create(str(self.workspace), self.policy, runs.workspace_fingerprint(self.workspace),
                                 messages or [{"role": "user", "content": "Update source"}], key)[0]

    def test_actual_crashes_before_after_dispatch_and_result(self):
        source = r'''
import json, os, sys
from pathlib import Path
from studio.services.workflow_runs import RunStore, workspace_fingerprint
root, workspace, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
store = RunStore(root)
messages = [{"role":"user","content":"Write marker"}, {"role":"assistant","content":"","tool_calls":[{"id":"same-model-id","type":"function","function":{"name":"run_shell","arguments":"{}"}}]}]
record,_ = store.create(str(workspace), "trusted-policy-v1", workspace_fingerprint(workspace), messages)
(root / "id").write_text(record["id"])
store.claim(record["id"])
if stage == "before_dispatch": os._exit(31)
store.dispatch(record["id"], "1-0", "run_shell", {"command":"write marker"}, call_id="same-model-id")
if stage == "after_dispatch": os._exit(32)
(workspace / "marker").write_text("one side effect")
if stage == "after_effect": os._exit(33)
store.result(record["id"], "1-0", {"returncode":0,"stdout":"created"})
os._exit(34)
'''
        for stage in ("before_dispatch", "after_dispatch", "after_effect", "after_result"):
            directory = self.root / stage
            workspace = self.root / (stage + "-workspace"); workspace.mkdir()
            child = subprocess.run([sys.executable, "-c", source, str(directory), str(workspace), stage],
                                   cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True)
            self.assertIn(child.returncode, (31, 32, 33, 34), child.stderr)
            store = runs.RunStore(directory)
            record = store.get((directory / "id").read_text())
            unknown = stage in ("after_dispatch", "after_effect")
            self.assertEqual(record["phase"], "needs_reconciliation" if unknown else "interrupted")
            self.assertEqual((workspace / "marker").exists(), stage in ("after_effect", "after_result"))
            seq = [event["seq"] for event in store.events(record["id"])]
            self.assertEqual(seq, sorted(set(seq)))
            fingerprint = runs.workspace_fingerprint(workspace)
            if unknown:
                with self.assertRaises(runs.RunConflict):
                    store.prepare_resume(record["id"], record["seq"], fingerprint, self.policy)
                record = store.reconcile(record["id"], "1-0", "executed" if stage == "after_effect" else "not_executed",
                                         "User inspected the marker and confirmed the outcome", record["seq"])
            record, token = store.prepare_resume(record["id"], record["seq"], fingerprint, self.policy,
                                                  accept_workspace_digest=fingerprint["digest"])
            record = store.claim(record["id"], token)
            recovered = store.continuation_messages(record)
            result = [m for m in recovered if m.get("tool_call_id") == "same-model-id"]
            self.assertEqual(len(result), 1)
            if stage in ("after_effect", "after_result"):
                with self.assertRaises(runs.RunConflict):
                    store.dispatch(record["id"], "2-0", "run_shell", {"command": "write marker"})
                self.assertEqual((workspace / "marker").read_text(), "one side effect")
            store.finish(record["id"], "incomplete")

    def test_duplicate_clients_idempotency_and_failed_effects(self):
        record = self.create("same-request")
        duplicate, created = self.store.create(str(self.workspace), self.policy, runs.workspace_fingerprint(self.workspace),
                                                [{"role": "user", "content": "Update source"}], "same-request")
        self.assertFalse(created); self.assertEqual(record["id"], duplicate["id"])
        with self.assertRaises(runs.RunConflict):
            self.store.create(str(self.workspace), self.policy, runs.workspace_fingerprint(self.workspace),
                              [{"role": "user", "content": "Different task"}], "same-request")
        record = self.store.claim(record["id"])
        competitor = runs.RunStore(self.store.root)
        with self.assertRaises(runs.RunConflict):
            competitor.claim(record["id"])
        self.store.dispatch(record["id"], "1-0", "mcp__server__send", {"target": "test"})
        self.store.result(record["id"], "1-0", {"error": "Timed out after sending; side effect may have happened"})
        record = self.store.finish(record["id"], "failed")
        record, token = self.store.prepare_resume(record["id"], record["seq"], record["fingerprint"], self.policy)
        self.store.claim(record["id"], token)
        with self.assertRaises(runs.RunConflict):
            self.store.dispatch(record["id"], "2-0", "mcp__server__send", {"target": "test"})
        with self.assertRaises(runs.RunConflict):
            competitor.claim(record["id"], token)
        self.store.finish(record["id"], "incomplete")

    def test_result_persistence_failure_is_unknown_and_not_replayed(self):
        record = self.store.claim(self.create()["id"])
        self.store.dispatch(record["id"], "1-0", "run_shell", {"command": "write then fail journal"})
        marker = self.workspace / "marker"; marker.write_text("executed once")
        with patch.object(runs, "write_text_atomic", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                self.store.result(record["id"], "1-0", {"returncode": 0})
        self.store._leases.pop(record["id"]).close()
        recovered = runs.RunStore(self.store.root).get(record["id"])
        self.assertEqual(recovered["steps"]["1-0"]["status"], "unknown_outcome")
        self.assertEqual(marker.read_text(), "executed once")

    def test_prune_keeps_unresolved_recent_and_quarantined_runs(self):
        import time
        DAY = 86400
        def aged(phase, days):
            record = self.create(f"{phase}-{days}-{time.time_ns()}")
            record = self.store.claim(record["id"])
            self.store.finish(record["id"], phase)
            path = self.store._path(record["id"])
            data = json.loads(path.read_text()); data["updated"] = time.time() - days * DAY; path.write_text(json.dumps(data))
            return record["id"]
        old_done, old_failed = aged("complete", 90), aged("failed", 60)
        old_needs, old_unknown = aged("needs_user", 90), aged("cancelled", 40)
        recent = aged("complete", 1)
        corrupt = self.store.root / ("f" * 32 + ".corrupt-abcd1234"); corrupt.write_text("{broken")
        self.assertRaises(ValueError, runs.prune, 0)
        self.assertRaises(ValueError, runs.prune, 30, -1)
        report = runs.prune(30, keep_latest=1, run_store=self.store)
        self.assertEqual(set(report["removed_runs"]), {old_done, old_failed, old_unknown})
        for kept in (old_needs, recent):
            self.assertTrue(self.store._path(kept).exists(), "unresolved and recent runs survive")
        self.assertTrue(corrupt.exists(), "quarantined evidence is never deleted")
        self.assertEqual(runs.prune(30, keep_latest=1, run_store=self.store)["removed_runs"], [], "idempotent")
        self.assertEqual(runs.prune(30, keep_latest=5, run_store=self.store)["removed_runs"], [])

    def test_cas_cancellation_corruption_and_retention(self):
        record = self.create()
        with self.assertRaises(runs.RunConflict):
            self.store.prepare_resume(record["id"], record["seq"] - 1, record["fingerprint"], self.policy)
        self.store.claim(record["id"])
        self.store.cancel(record["id"])
        with self.assertRaises(runs.RunConflict):
            self.store.dispatch(record["id"], "1-0", "run_shell", {"command": "must not execute"})
        self.store.finish(record["id"], "cancelled")
        with patch.object(runs, "MAX_EVENTS", 3):
            for _ in range(5): self.store.append(record["id"], "test", {})
        with self.assertRaises(runs.RunConflict): self.store.events(record["id"], 0)
        self.assertEqual(len(self.store.get(record["id"])["events"]), 3)
        with self.assertRaises(ValueError): self.store.get("../outside")
        path = self.store._path(record["id"]); path.write_text("{corrupt")
        with self.assertRaises(runs.RunConflict): self.store.get(record["id"])
        self.assertFalse(path.exists()); self.assertEqual(len(list(self.store.root.glob("*.corrupt-*"))), 1)

    def test_resume_revalidates_workspace_policy_and_approval(self):
        record = self.store.claim(self.create()["id"])
        self.store.approval(record["id"], {"id": "approval", "perm_scope": "exec"})
        self.store._leases.pop(record["id"]).close()
        record = runs.RunStore(self.store.root).get(record["id"])
        self.assertIsNone(record["pending_approval"])
        with self.assertRaises(runs.RunConflict):
            self.store.prepare_resume(record["id"], record["seq"], record["fingerprint"], "changed policy")
        (self.workspace / "external.py").write_text("external edit")
        changed = runs.workspace_fingerprint(self.workspace)
        with self.assertRaises(runs.RunConflict):
            self.store.prepare_resume(record["id"], record["seq"], changed, self.policy)
        record, token = self.store.prepare_resume(record["id"], record["seq"], changed, self.policy, changed["digest"])
        self.assertIsNone(record["verification"])
        self.assertIsNone(record["pending_approval"])
        self.store.claim(record["id"], token)
        self.store.finish(record["id"], "complete")


class FingerprintedVerification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="ls-verify-")
        self.workspace = Path(self.temp.name).resolve()
        (self.workspace / "source.py").write_text("value=1\n")
        self.now = 0
        self.options = {"commands": [{"id": "tests", "command": "trusted check"}], "elapsed_budget_seconds": 10,
                        "max_repair_attempts": 1, "fingerprint_excludes": ["generated"]}
        self.settings = {"harness": {"verification_workspaces": {str(self.workspace): self.options}}}
        self.gate = VerificationGate(True, str(self.workspace), self.settings, fingerprints=True, clock=lambda: self.now)

    def tearDown(self): self.temp.cleanup()

    def run_check(self, rc=0):
        args = {"command": "trusted check"}
        self.gate.before_execution("run_shell", args)
        self.gate.observe("run_shell", args, {"command": "trusted check", "cwd": str(self.workspace), "returncode": rc,
                                            **({"error": "failed"} if rc else {})}, executed=True)

    def test_external_source_and_checker_edit_invalidates_pass(self):
        (self.workspace / "verify.py").write_text("assert True\n")
        self.run_check()
        self.assertEqual(self.gate.status()["status"], "verified")
        (self.workspace / "verify.py").write_text("assert False\n")
        self.assertEqual(self.gate.status()["status"], "unverified")
        self.assertEqual(self.gate.snapshot()["check_outcomes"][0]["status"], "stale")
        self.run_check(); self.assertEqual(self.gate.status()["status"], "verified")
        (self.workspace / "untracked.py").write_text("external new file")
        self.assertEqual(self.gate.status()["status"], "unverified")

    def test_pre_post_fingerprint_and_explicit_generated_scope(self):
        args = {"command": "trusted check"}
        self.gate.before_execution("run_shell", args)
        (self.workspace / "source.py").write_text("changed during check")
        self.gate.observe("run_shell", args, {"command": "trusted check", "cwd": str(self.workspace), "returncode": 0}, executed=True)
        self.assertEqual(self.gate.status()["status"], "unverified")
        self.assertEqual(self.gate.check_outcomes[-1]["status"], "stale")
        self.run_check()
        generated = self.workspace / "generated"; generated.mkdir()
        (generated / "cache").write_text("excluded by trusted policy")
        self.assertEqual(self.gate.status()["status"], "verified")

    def test_budget_failures_skips_timeout_and_partial_scan(self):
        self.run_check(1); self.run_check(1)
        self.assertTrue(self.gate.exhausted())
        self.assertFalse(self.gate.completion()[1])
        fresh = VerificationGate(True, str(self.workspace), self.settings, fingerprints=True, clock=lambda: self.now)
        fresh.observe("run_shell", {"command": "trusted check"}, {"declined": True, "error": "declined"}, executed=False)
        self.assertEqual(fresh.check_outcomes[-1]["status"], "skipped")
        self.now = 11
        self.assertTrue(self.gate.exhausted())
        self.assertFalse(runs.workspace_fingerprint(self.workspace, max_bytes=1)["digest"])
        (self.workspace / "link").symlink_to("source.py")
        self.assertFalse(runs.workspace_fingerprint(self.workspace)["digest"])


class DurableChat(unittest.TestCase):
    def test_real_edit_check_is_persisted_before_terminal_answer(self):
        with fixture.Env() as env:
            workspace = fixture.TMP / "durable-chat"; command = fixture._verification_setup(workspace)
            settings = fixture.app.load_settings(); settings["harness"]["durable_runs"] = True; fixture.app.save_settings(settings)
            turns = [fixture._verification_edit(), fixture.chat_action_deltas("run_shell", json.dumps({"command": command})),
                     [fixture.chat_delta(content="Durably verified")]]
            response, _, content, status = fixture._verification_request(env, workspace, turns)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(content, "Durably verified", response.text)
            record = runs.store.get(response.headers["x-workflow-run-id"])
            self.assertEqual(record["outcome"], "verified_complete")
            self.assertEqual(record["verification"]["check_outcomes"][-1]["status"], "passed")
            events = record["events"]
            dispatched = [e["seq"] for e in events if e["kind"] == "tool_dispatched"]
            results = [e["seq"] for e in events if e["kind"] == "tool_result"]
            self.assertEqual(len(dispatched), 2); self.assertEqual(len(results), 2)
            self.assertTrue(all(before < after for before, after in zip(dispatched, results)))
            final = next(e["seq"] for e in events if e["kind"] == "sse" and "Durably verified" in json.dumps(e["payload"]))
            self.assertGreater(final, max(results))

    def test_external_edit_between_check_and_final_cannot_claim_success(self):
        with fixture.Env() as env:
            workspace = fixture.TMP / "durable-external"; command = fixture._verification_setup(workspace)
            settings = fixture.app.load_settings(); settings["harness"]["durable_runs"] = True; fixture.app.save_settings(settings)
            def turns():
                yield fixture._verification_edit()
                yield fixture.chat_action_deltas("run_shell", json.dumps({"command": command}))
                (workspace / "verify.py").write_text("raise AssertionError('external checker edit')\n")
                for _ in range(3): yield [fixture.chat_delta(content="UNSUPPORTED_SUCCESS")]
            response, _, content, _ = fixture._verification_request(env, workspace, turns())
            self.assertNotIn("UNSUPPORTED_SUCCESS", response.text)
            self.assertIn("Unverified:", content)
            record = runs.store.get(response.headers["x-workflow-run-id"])
            self.assertEqual(record["outcome"], "incomplete")
            self.assertEqual(record["verification"]["check_outcomes"][0]["status"], "stale")

    def test_final_publication_revalidates_after_verified_status_event(self):
        import asyncio
        import studio.api.chat as chat_api
        with fixture.Env():
            workspace = fixture.TMP / "durable-final-boundary"; command = fixture._verification_setup(workspace)
            settings = fixture.app.load_settings(); settings["harness"]["durable_runs"] = True; fixture.app.save_settings(settings)
            chunks = []
            class Request:
                async def json(self):
                    return {"model_id": fixture.TEXT_MLX, "workspace": str(workspace), "messages": [{"role": "user", "content": "Edit and check"}]}
                async def is_disconnected(self): return False
            async def exercise():
                response = await chat_api.chat(Request())
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                return response.headers["x-workflow-run-id"]
            original_append = runs.store.append
            def append_then_external_edit(run_id, kind, payload):
                result = original_append(run_id, kind, payload)
                if kind == "sse" and (payload.get("data") or {}).get("x_verification", {}).get("status") == "verified":
                    (workspace / "value.py").write_text("external = 'changed at final boundary'\n")
                return result
            with fixture.ChatEngine(turns=[fixture._verification_edit(), fixture.chat_action_deltas("run_shell", json.dumps({"command": command})),
                                           [fixture.chat_delta(content="UNSUPPORTED_SUCCESS")]]), \
                 patch("studio.api.chat.harness_gate", return_value=("safe", None, {})), \
                 patch.object(runs.store, "append", side_effect=append_then_external_edit):
                run_id = asyncio.run(exercise())
            self.assertNotIn("UNSUPPORTED_SUCCESS", "".join(chunks))
            self.assertIn("Unverified:", "".join(chunks))
            self.assertEqual(runs.store.get(run_id)["outcome"], "incomplete")
            statuses = [e["x_verification"]["status"] for chunk in chunks for e in fixture.sse(chunk) if "x_verification" in e]
            self.assertEqual(statuses[-1], "unverified")

    def test_resume_restores_context_and_reasks_permission(self):
        import asyncio
        import studio.api.chat as chat_api
        from studio.services import workflow
        with fixture.Env():
            workspace = fixture.TMP / "durable-resume"; fixture._verification_setup(workspace)
            (workspace / "value.py").write_text("x = 2\n")
            settings = fixture.app.load_settings(); settings["harness"]["durable_runs"] = True; fixture.app.save_settings(settings)
            identity = runs.workspace_fingerprint(workspace)
            record, _ = runs.store.create(str(workspace.resolve()), runs.policy_fingerprint(settings, str(workspace.resolve())), identity,
                                          [{"role": "user", "content": "Continue the original source change"}], metadata={"task_identity": None, "workflow_mode": "act"})
            runs.store.claim(record["id"])
            runs.store.dispatch(record["id"], "1-0", "edit_file", {"path": "value.py", "old_text": "1", "new_text": "2"}, call_id="old")
            runs.store.result(record["id"], "1-0", {"status": "edited", "path": str(workspace / "value.py")})
            record = runs.store.finish(record["id"], "incomplete")
            record, token = runs.store.prepare_resume(record["id"], record["seq"], identity, record["policy_digest"])
            chunks = []
            class Request:
                async def json(self):
                    return {"model_id": fixture.TEXT_MLX, "workspace": str(workspace), "permission_mode": "accept_edits",
                            "workflow_run_id": record["id"], "workflow_resume_token": token,
                            "messages": [{"role": "user", "content": "Untrusted replacement task"}]}
                async def is_disconnected(self): return False
            async def exercise():
                response = await chat_api.chat(Request())
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                    if "x_approval" in chunk:
                        prompt = fixture.sse(chunk)[0]["x_approval"]
                        self.assertEqual(prompt["name"], "edit_file")
                        chat_api.chat_session_reply(prompt["session_id"], {"id": prompt["id"], "decision": "deny"})
            with fixture.ChatEngine(turns=[fixture._verification_edit("2", "3")]) as engine:
                asyncio.run(exercise())
            self.assertIn("Continue the original", json.dumps(engine.seen[0]["messages"]))
            self.assertNotIn("Untrusted replacement", json.dumps(engine.seen[0]["messages"]))
            self.assertEqual((workspace / "value.py").read_text(), "x = 2\n")
            self.assertIn("Unverified:", "".join(chunks))
            final = runs.store.get(record["id"])
            self.assertEqual(final["outcome"], "needs_user")
            self.assertEqual(final["attempt"], 2)

    def test_elapsed_budget_stops_foreground_check_and_persists_failure(self):
        import shlex
        import time
        with fixture.Env() as env:
            workspace = fixture.TMP / "durable-deadline"; command = fixture._verification_setup(workspace)
            (workspace / "verify.py").write_text("import time\nprint('deadline-started',flush=True)\ntime.sleep(30)\nopen('must-not-exist','w').write('bad')\n")
            settings = fixture.app.load_settings(); settings["harness"]["durable_runs"] = True
            settings["harness"]["verification_workspaces"][str(workspace.resolve())]["elapsed_budget_seconds"] = 1
            fixture.app.save_settings(settings)
            started = time.monotonic()
            turns = [fixture._verification_edit(), fixture.chat_action_deltas("run_shell", json.dumps({"command": command}))]
            response, _, _, _ = fixture._verification_request(env, workspace, turns)
            self.assertLess(time.monotonic() - started, 4)
            self.assertFalse((workspace / "must-not-exist").exists())
            record = runs.store.get(response.headers["x-workflow-run-id"])
            self.assertEqual(record["outcome"], "incomplete", response.text)
            self.assertNotEqual(record["verification"]["status"], "verified")
            self.assertFalse(any(step["status"] == "dispatched" for step in record["steps"].values()))


class TaskChat(unittest.TestCase):
    """Plan/Act/Auto through the real /api/chat route with scripted model turns (no engine)."""

    def prepare(self, name, mode):
        from studio.services import workflow
        workspace = fixture.TMP / name
        command = fixture._verification_setup(workspace)
        settings = fixture.app.load_settings(); settings["harness"].update(workflow=True, durable_runs=True); fixture.app.save_settings(settings)
        task = workflow.create_task(str(workspace.resolve()), "Make value 2", "value.py holds x = 2", "Edit value.py, run the check", mode=mode)
        return workspace, command, task, workflow

    def post(self, env, workspace, body_extra, turns, approvals=None):
        from unittest.mock import patch
        import asyncio
        import studio.api.chat as api_chat
        events = []
        class Request:
            async def json(self_inner):
                return {"model_id": fixture.TEXT_MLX, "workspace": str(workspace.resolve()), "permission_mode": "accept_edits",
                        "messages": [{"role": "user", "content": "Update value.py and verify the change"}], **body_extra}
            async def is_disconnected(self_inner): return False
        async def exercise():
            response = await api_chat.chat(Request())
            self.run_id = (getattr(response, "headers", {}) or {}).get("x-workflow-run-id")
            async for chunk in response.body_iterator:
                events.append(chunk)
                if approvals and "x_approval" in chunk:
                    prompt = json.loads(chunk[6:])["x_approval"]
                    api_chat.chat_session_reply(prompt["session_id"], {"id": prompt["id"], "decision": approvals})
        with fixture.ChatEngine(turns=turns):
            asyncio.run(exercise())
        return "".join(events)

    def test_declined_approval_in_a_task_run_ends_needs_user_without_a_success_claim(self):
        with fixture.Env() as env:
            workspace, command, task, workflow = self.prepare("task-declined", "act")
            turns = [fixture._verification_edit(), fixture.chat_action_deltas("run_shell", json.dumps({"command": command})),
                     fixture.chat_action_deltas("reply", '{"text":"CLAIM_ALL_DONE"}')]
            joined = self.post(env, workspace, {"workflow_task_id": task["id"], "workflow_mode": "act"}, turns, approvals="deny")
            self.assertNotIn("CLAIM_ALL_DONE", joined)
            self.assertIn("Unverified:", joined)
            self.assertEqual(joined.count('"x_approval"'), 1)
            self.assertEqual(workflow.get_task(task["id"])["state"], "needs_user")
            self.assertEqual(runs.store.get(self.run_id)["outcome"], "needs_user")

    def test_auto_mode_needs_a_task_and_ends_verified_or_incomplete(self):
        from fastapi import HTTPException
        with fixture.Env() as env:
            workspace, command, task, workflow = self.prepare("task-auto", "auto")
            with self.assertRaises(HTTPException) as refused:
                self.post(env, workspace, {"workflow_mode": "auto"}, [[fixture.chat_delta(content="x")]])
            self.assertIn(refused.exception.status_code, (400, 409))
            self.assertIn("Auto mode requires a durable task", str(refused.exception.detail))
            turns = [fixture._verification_edit(), fixture.chat_action_deltas("run_shell", json.dumps({"command": command})),
                     [fixture.chat_delta(content="Auto finished")]]
            joined = self.post(env, workspace, {"workflow_task_id": task["id"]}, turns, approvals="allow")
            self.assertIn("Auto finished", joined)
            self.assertEqual(workflow.get_task(task["id"])["state"], "verified_complete")
            self.assertEqual(runs.store.get(self.run_id)["outcome"], "verified_complete")
            self.assertIn("x = 2", (workspace / "value.py").read_text())
        with fixture.Env() as env:
            workspace, command, task, workflow = self.prepare("task-auto-claim", "auto")
            turns = [fixture._verification_edit()] + [[fixture.chat_delta(content="UNSUPPORTED_SUCCESS")]] * 4
            joined = self.post(env, workspace, {"workflow_task_id": task["id"]}, turns, approvals="allow")
            self.assertNotIn("UNSUPPORTED_SUCCESS", joined)
            self.assertEqual(workflow.get_task(task["id"])["state"], "incomplete")
            self.assertEqual(runs.store.get(self.run_id)["outcome"], "incomplete")

    def test_applied_project_rules_are_shown_on_the_tool_step_and_are_content_free(self):
        from studio.services import instructions
        with fixture.Env() as env:
            workspace = fixture.TMP / "rules-shown"
            (workspace / "src").mkdir(parents=True); (workspace / "other").mkdir()
            (workspace / "AGENTS.md").write_text("ROOT-SECRET-RULE-TEXT\n"); (workspace / "src" / "CLAUDE.md").write_text("SRC-SECRET-RULE-TEXT\n")
            (workspace / "src" / "a.py").write_text("A = 1\n"); (workspace / "other" / "b.py").write_text("B = 1\n")
            settings = fixture.app.load_settings(); settings["harness"]["nested_instructions"] = True; fixture.app.save_settings(settings)
            turns = [fixture.chat_action_deltas("read_file", json.dumps({"path": "src/a.py"})),
                     fixture.chat_action_deltas("read_file", json.dumps({"path": "other/b.py"})), [fixture.chat_delta(content="Read both")]]
            joined = self.post(env, workspace, {}, turns)
            done = [json.loads(line[6:])["x_tool"] for line in joined.splitlines() if line.startswith("data: ") and '"x_tool"' in line]
            done = [t for t in done if t["status"] == "done"]
            self.assertEqual(len(done), 2, joined[:600])
            deep, shallow = done
            self.assertEqual([(f["name"], f["scope"]) for f in deep["rules"][0]["files"]], [("AGENTS.md", "."), ("src/CLAUDE.md", "src")])
            self.assertEqual(deep["rules"][0]["target"], "src/a.py")
            self.assertEqual([f["name"] for f in shallow["rules"][0]["files"]], ["AGENTS.md"], "a sibling's rules never apply")
            self.assertTrue(deep["rules_complete"])
            self.assertNotIn("SECRET-RULE-TEXT", json.dumps(done), "provenance carries names, scopes and digests, never rule content")
            summary = instructions.summarize(instructions.resolve_instructions(str(workspace), ["src/a.py"]))
            self.assertEqual(len(summary[0]["files"][0]["sha256"]), 12)
        with fixture.Env() as env:                                                   # feature off: no provenance, nothing changes
            workspace = fixture.TMP / "rules-off"; workspace.mkdir()
            (workspace / "AGENTS.md").write_text("x\n"); (workspace / "a.py").write_text("A = 1\n")
            joined = self.post(env, workspace, {}, [fixture.chat_action_deltas("read_file", json.dumps({"path": "a.py"})), [fixture.chat_delta(content="ok")]])
            self.assertNotIn('"rules"', joined)

    def test_plan_mode_run_cannot_edit_and_ends_planned(self):
        with fixture.Env() as env:
            workspace, command, task, workflow = self.prepare("task-plan", "plan")
            turns = [fixture._verification_edit(), [fixture.chat_delta(content="Here is the plan")]]
            joined = self.post(env, workspace, {"workflow_task_id": task["id"]}, turns, approvals="allow")
            self.assertEqual((workspace / "value.py").read_text(), "x = 1\n", "Plan mode must not edit project files")
            self.assertIn("Plan mode", joined)
            self.assertEqual(workflow.get_task(task["id"])["state"], "planned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
