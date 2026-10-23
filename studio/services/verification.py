"""Opt-in, turn-local completion gate using user-configured execution evidence.

Configuration is snapshotted from STATE settings, never from model tool arguments.
This tracks tools observed in one chat turn; it is not a filesystem snapshot or a
durable workflow ledger.
"""

import hashlib
import json
import time
from pathlib import Path

from studio.services.workflow_runs import workspace_fingerprint

class VerificationGate:
    MAX_COMPLETION_RETRIES = 2

    def __init__(self, enabled, workspace, settings, fingerprints=False, clock=None):
        self.enabled = bool(enabled and workspace)
        self.workspace = str(Path(workspace).resolve()) if workspace else None
        self.commands = {}
        self.config_error = "No trusted verification commands are configured for this workspace."
        harness = settings.get("harness") or {}
        workspaces = harness.get("verification_workspaces") or {}
        configured = workspaces.get(self.workspace) if isinstance(workspaces, dict) else None
        commands = configured.get("commands") if isinstance(configured, dict) else None
        self.options = json.loads(json.dumps(configured)) if isinstance(configured, dict) else {}
        self.fingerprints = bool(self.enabled and fingerprints)
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.started_wall = time.time()
        attempts = self.options.get("max_repair_attempts", self.MAX_COMPLETION_RETRIES)
        budget = self.options.get("elapsed_budget_seconds", 180)
        self.max_repair_attempts = attempts if type(attempts) is int and 0 <= attempts <= 12 else self.MAX_COMPLETION_RETRIES
        self.elapsed_budget = budget if type(budget) in (int, float) and 1 <= budget <= 3600 else 180
        self.failure_attempts = 0
        self.check_outcomes = []
        self._prepared_fingerprint = None
        self._fingerprint = None
        self.policy_digest = hashlib.sha256(json.dumps(self.options, sort_keys=True).encode()).hexdigest()
        if isinstance(commands, list) and 0 < len(commands) <= 16:
            for item in commands:
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or not item["id"].strip() or item["id"] in self.commands
                        or not isinstance(item.get("command"), str) or not item["command"].strip()
                        or len(item["command"]) > 4000 or "\x00" in item["command"]):
                    self.commands.clear()
                    self.config_error = "Trusted verification configuration is invalid."
                    break
                self.commands[item["id"]] = item["command"]
        self.revision = 0
        self.checked = False
        self.passed = {}
        self.failed = set()
        self.syntax_failures = set()
        self.background = False
        self.declined = False
        self.approval_timeout = False
        self.completion_retries = 0

    def _requested_ids(self, name, args):
        if name != "run_shell" or args.get("background"):
            return []
        try:
            requested_cwd = Path(args.get("cwd") or self.workspace).expanduser()
            if not requested_cwd.is_absolute():
                requested_cwd = Path(self.workspace) / requested_cwd
            if str(requested_cwd.resolve()) != self.workspace:
                return []
        except (TypeError, ValueError, OSError):
            return []
        return [key for key, command in self.commands.items() if command == args.get("command")]

    def _configured_ids(self, name, args, result):
        try:
            if result.get("command") != args.get("command") or not result.get("cwd") or str(Path(result["cwd"]).resolve()) != self.workspace:
                return []
        except (TypeError, ValueError, OSError):
            return []
        return self._requested_ids(name, args)

    def fingerprint(self):
        return workspace_fingerprint(self.workspace, self.options.get("fingerprint_paths"),
                                     excludes=self.options.get("fingerprint_excludes")) if self.fingerprints else None

    def _sync_fingerprint(self, fingerprint):
        if self.passed and self._fingerprint and (not fingerprint.get("digest") or fingerprint.get("digest") != self._fingerprint.get("digest")):
            self.revision += 1
            self.passed.clear()
            for outcome in self.check_outcomes:
                if outcome["status"] == "passed":
                    outcome["status"] = "stale"
        self._fingerprint = fingerprint

    def before_execution(self, name, args):
        if self.fingerprints and self._requested_ids(name, args):
            self._prepared_fingerprint = self.fingerprint()
            self._sync_fingerprint(self._prepared_fingerprint)

    def remaining_seconds(self):
        return max(0, self.elapsed_budget - (self.clock() - self.started))

    def exhausted(self):
        return self.enabled and (self.remaining_seconds() <= 0 or self.failure_attempts > self.max_repair_attempts)

    def snapshot(self):
        return {**self.status(), "commands": self.commands, "policy_digest": self.policy_digest,
                "check_outcomes": self.check_outcomes[-100:], "fingerprint": self._fingerprint,
                "elapsed_seconds": self.clock() - self.started, "elapsed_budget_seconds": self.elapsed_budget,
                "started_wall": self.started_wall,
                "failure_attempts": self.failure_attempts, "completion_retries": self.completion_retries}

    def restore_budget(self, snapshot):
        if not isinstance(snapshot, dict):
            return
        elapsed = max(0, float(snapshot.get("elapsed_seconds", 0)))
        started_wall = snapshot.get("started_wall")
        if type(started_wall) in (int, float):
            elapsed = max(elapsed, time.time() - started_wall)
            self.started_wall = started_wall
        self.started = self.clock() - elapsed
        self.failure_attempts = max(0, int(snapshot.get("failure_attempts", 0)))
        self.completion_retries = max(0, int(snapshot.get("completion_retries", 0)))
        self.background = bool(snapshot.get("background_started"))
        self.syntax_failures = set(snapshot.get("syntax_failures") or [])
        self.check_outcomes = json.loads(json.dumps(snapshot.get("check_outcomes") or []))[-100:]
        for outcome in self.check_outcomes:
            if outcome.get("status") == "passed":
                outcome["status"] = "stale"

    def observe(self, name, args, result, executed=False):
        """Only backend execution results count. Call before result redaction."""
        if not self.enabled:
            return
        args = args if isinstance(args, dict) else {}
        requested_ids = self._requested_ids(name, args)
        if name == "run_shell" and args.get("command") in self.commands.values():
            self.checked = True
        refused_action = name in ("run_shell", "run_python", "write_file", "edit_file", "multi_edit_file") or name.startswith("mcp__")
        if result.get("declined") or (not executed and result.get("error") and refused_action):
            self.declined = True
            self.checked = True
            self.approval_timeout = bool(result.get("timed_out"))
        if not executed:
            for key in requested_ids:
                self.check_outcomes.append({"id": key, "command": self.commands[key], "status": "skipped", "returncode": None,
                                            "revision": self.revision, "evidence": result.get("error"), "policy_digest": self.policy_digest})
            return
        check_ids = self._configured_ids(name, args, result)
        if check_ids:
            self.checked = True
            after = self.fingerprint() if self.fingerprints else None
            before = self._prepared_fingerprint
            stable = not self.fingerprints or bool(before and before.get("digest") and after.get("digest") == before["digest"])
            if self.fingerprints:
                self._sync_fingerprint(after)
            any_failed = False
            for key in check_ids:
                # bool is an int in Python; only a real exit status is evidence.
                passed = type(result.get("returncode")) is int and result["returncode"] == 0 and not result.get("error") and stable
                outcome = {"id": key, "command": self.commands[key], "status": "passed" if passed else "stale" if not stable else "failed",
                           "returncode": result.get("returncode"), "revision": self.revision,
                           "fingerprint_before": before, "fingerprint_after": after, "policy_digest": self.policy_digest,
                           "checker_identity": hashlib.sha256((self.commands[key] + ((before or {}).get("digest") or "")).encode()).hexdigest(),
                           "evidence": {k: str(result.get(k) or "")[-12000:] for k in ("stdout", "stderr", "error")}}
                self.check_outcomes.append(outcome)
                if passed:
                    self.passed[key] = self.revision
                    self.failed.discard(key)
                else:
                    self.passed.pop(key, None)
                    self.failed.add(key)
                    any_failed = True
            if any_failed:
                self.failure_attempts += 1
            self._prepared_fingerprint = None
            return
        if requested_ids:
            self.failure_attempts += 1
            for key in requested_ids:
                timed_out = "timed out" in str(result.get("error", "")).lower()
                self.check_outcomes.append({"id": key, "command": self.commands[key], "status": "timed_out" if timed_out else "failed",
                                            "returncode": result.get("returncode"), "revision": self.revision,
                                            "fingerprint_before": self._prepared_fingerprint, "policy_digest": self.policy_digest,
                                            "evidence": result.get("error")})
            self._prepared_fingerprint = None
        file_edit = name in ("write_file", "edit_file", "multi_edit_file")
        # The approval allowlist includes test runners that execute project code.
        # It is not proof of no writes; every unconfigured shell invalidates evidence.
        possible_mutation = file_edit or name in ("run_python", "run_shell") or name.startswith("mcp__")
        if possible_mutation:
            # Even a failed process can have written files before exiting.
            self.revision += 1
            self.passed.clear()
            self.failed.clear()
            if name == "run_shell" and args.get("background"):
                self.background = True
        if file_edit and result.get("path"):
            if result.get("syntax_ok") is False:
                self.syntax_failures.add(result["path"])
            elif result.get("syntax_ok") is True:
                self.syntax_failures.discard(result["path"])

    def status(self):
        if self.fingerprints and self.passed:
            self._sync_fingerprint(self.fingerprint())
        required = self.enabled and bool(self.revision or self.checked)
        missing = [key for key in self.commands if self.passed.get(key) != self.revision]
        verified = required and bool(self.commands) and not missing and not self.syntax_failures and not self.background and not self.declined and not self.exhausted()
        return {"status": "verified" if verified else "unverified" if required else "not_required",
                "revision": self.revision, "missing": missing, "failed": sorted(self.failed),
                "syntax_failures": sorted(self.syntax_failures), "background_started": self.background,
                "budget_exhausted": bool(self.exhausted())}

    def completion(self):
        """Return (allowed, retry, trusted explanation) for every final-answer path."""
        status = self.status()
        if status["status"] != "unverified":
            return True, False, ""
        if self.exhausted():
            reason = "The verification repair attempt or elapsed-time budget was exhausted."
        elif not self.commands:
            reason = self.config_error
        elif self.background:
            reason = "A background process was started; this turn cannot certify changes while it may still write files."
        elif self.approval_timeout:
            reason = "Approval for a required action timed out."
        elif self.declined:
            reason = "A required action was declined or blocked."
        else:
            reason = "Verification is pending for the latest observed changes."
        detail = []
        if status["missing"]:
            detail.append("Missing or failing checks: " + ", ".join(status["missing"]) + ".")
        if status["syntax_failures"]:
            detail.append("Unresolved edit diagnostics: " + ", ".join(status["syntax_failures"]) + ".")
        retry = bool(self.commands and not self.background and not self.declined and not self.exhausted()
                     and self.completion_retries < self.max_repair_attempts)
        self.completion_retries += 1
        return False, retry, "Unverified: " + " ".join([reason] + detail)

    def guidance(self, explanation):
        return (explanation + " Completion is withheld by the backend verification gate. "
                "Repair the issue and run every missing configured command with run_shell, foreground, "
                f"in workspace {self.workspace}. Existing execution approval still applies. "
                "Do not replace a configured command with an echo or another command. "
                f"Trusted checks: {self.commands!r}")
