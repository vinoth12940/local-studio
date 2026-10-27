"""Offline isolated-worker checks. Only throwaway Git repositories under /tmp are used; no model engine is touched.

The invariants: a worker edits its own checkout, never the parent's workspace; children cannot exceed the parent's scope or
budgets; integration applies only the exact patch a person reviewed, never over uncommitted work or a moved base, and never
commits; cancellation and cleanup keep the work recoverable.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ls-wt-", dir="/tmp")).resolve()
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")
os.environ["HF_HUB_CACHE"] = str(TMP / "hub")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from studio.main import app
from studio.services import workflow, workflow_runs, worktrees
from studio.services.verification import VerificationGate
from studio.storage.settings import load_settings, save_settings

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


def git(path, *args):
    done = subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args], capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout


def make_repo(scope=("src/**", "docs/**"), **budgets):
    _count[0] += 1
    repo = TMP / f"repo{_count[0]}"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "src" / "a.py").write_text("A = 1\n")
    (repo / "src" / "b.py").write_text("B = 1\n")
    (repo / "docs" / "readme.md").write_text("# Docs\n")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    settings(workflow=True, worktrees=True)
    task = workflow.create_task(str(repo), "Parallel work", "Both halves done", "Split by directory", mode="act",
                                scope=list(scope), budgets=budgets or None)
    return repo, task


def tree(worker):
    return Path(worker["path"])


def test_flags_and_repository_requirements():
    repo, task = make_repo()
    settings(worktrees=False)
    fails(lambda: worktrees.create_worker(task["id"], "w", ["src/**"]), "worktrees")
    settings(worktrees=True)
    plain = TMP / "plain"; plain.mkdir()
    plain_task = workflow.create_task(str(plain), "x", "y", "z", mode="act")
    fails(lambda: worktrees.create_worker(plain_task["id"], "w", ["**"]), "git repository")
    empty = TMP / "empty"; empty.mkdir(); git(empty, "init", "-q")
    empty_task = workflow.create_task(str(empty), "x", "y", "z", mode="act")
    fails(lambda: worktrees.create_worker(empty_task["id"], "w", ["**"]), "at least one commit")
    sub_task = workflow.create_task(str(repo / "src"), "x", "y", "z", mode="act")
    fails(lambda: worktrees.create_worker(sub_task["id"], "w", ["**"]), "repository root")
    for owner in ("", "x" * 65, "a\nb"):
        fails(lambda: worktrees.create_worker(task["id"], owner, ["src/**"]), "owner")
    for bad in ([], ["../x"], ["/etc/*"], "src/**"):
        fails(lambda: worktrees.create_worker(task["id"], "w", bad), "scope")


def test_worker_gets_an_isolated_checkout_and_the_parent_is_untouched():
    repo, task = make_repo()
    worker = worktrees.create_worker(task["id"], "frontend", ["src/**"], {"max_steps": 10, "max_seconds": 120})
    assert worker["state"] == "active" and worker["branch"].startswith("localstudio/") and worker["base_commit"] == git(repo, "rev-parse", "HEAD").strip()
    assert tree(worker).is_dir() and (tree(worker) / "src" / "a.py").read_text() == "A = 1\n"
    fingerprint = workflow_runs.workspace_fingerprint(tree(worker))
    assert fingerprint.get("digest") and not fingerprint.get("error"), fingerprint          # a worker chat must be able to start a durable run there
    (tree(worker) / "src" / "a.py").write_text("A = 2\n")
    assert (repo / "src" / "a.py").read_text() == "A = 1\n", "the parent's file must not change"
    assert [l for l in git(repo, "status", "--porcelain").splitlines() if ".localstudio" not in l] == [], "the parent checkout stays clean"
    assert worker["branch"] in git(repo, "branch", "--list")
    listing = worktrees.list_workers(task["id"])
    assert [w["id"] for w in listing["workers"]] == [worker["id"]] and listing["workers"][0]["health"] == "ok"


def test_children_cannot_exceed_the_parent_scope_budgets_count_or_ownership():
    repo, task = make_repo(max_steps=20, max_children=3)
    fails(lambda: worktrees.create_worker(task["id"], "w", ["**"]), "inside the parent task's scope")
    fails(lambda: worktrees.create_worker(task["id"], "w", ["tests/**"]), "inside the parent task's scope")
    fails(lambda: worktrees.create_worker(task["id"], "w", ["src/**"], {"max_steps": 21}), "remaining")
    fails(lambda: worktrees.create_worker(task["id"], "w", ["src/**"], {"max_steps": 5, "max_tools": 1}), "may only set")
    first = worktrees.create_worker(task["id"], "alice", ["src/**"], {"max_steps": 12})
    fails(lambda: worktrees.create_worker(task["id"], "bob", ["src/a.py"]), "overlaps worker")
    fails(lambda: worktrees.create_worker(task["id"], "bob", ["docs/**"], {"max_steps": 12}), "remaining step budget")
    second = worktrees.create_worker(task["id"], "bob", ["docs/**"], {"max_steps": 8})
    fails(lambda: worktrees.create_worker(task["id"], "carol", ["docs/x.md"]), "overlaps")
    other_repo, other_task = make_repo(max_children=1)
    worktrees.create_worker(other_task["id"], "only", ["src/**"])
    fails(lambda: worktrees.create_worker(other_task["id"], "extra", ["docs/**"]), "at most 1 active workers")
    paused_repo, paused_task = make_repo()
    workflow.update_task(paused_task["id"], paused_task["version"], {"state": "paused"})
    fails(lambda: worktrees.create_worker(paused_task["id"], "w", ["src/**"]), "paused")


def test_integration_applies_only_the_reviewed_patch_without_committing():
    repo, task = make_repo()
    worker = worktrees.create_worker(task["id"], "writer", ["src/**"])
    before_head = git(repo, "rev-parse", "HEAD").strip()
    empty = worktrees.prepare_integration(worker["id"])
    assert empty["can_integrate"] is False and "made no changes" in empty["problems"][0]
    (tree(worker) / "src" / "a.py").write_text("A = 2\n")
    (tree(worker) / "src" / "new.py").write_text("NEW = True\n")
    reviewed = worktrees.prepare_integration(worker["id"])
    assert reviewed["can_integrate"] and reviewed["files"] == ["src/a.py", "src/new.py"] and "A = 2" in reviewed["patch_preview"]
    assert (repo / "src" / "a.py").read_text() == "A = 1\n", "preparing a review must not touch the parent"
    (tree(worker) / "src" / "a.py").write_text("A = 3\n")                                  # changed after the review
    fails(lambda: worktrees.integrate(worker["id"], reviewed["digest"]), "differ from the patch you reviewed")
    assert (repo / "src" / "a.py").read_text() == "A = 1\n"
    current = worktrees.prepare_integration(worker["id"])
    fingerprint = workflow_runs.workspace_fingerprint(repo)["digest"]
    result = worktrees.integrate(worker["id"], current["digest"])
    assert result["committed"] is False and result["applied_files"] == ["src/a.py", "src/new.py"] and "rerun the configured checks" in result["next"]
    assert (repo / "src" / "a.py").read_text() == "A = 3\n" and (repo / "src" / "new.py").exists()
    assert git(repo, "rev-parse", "HEAD").strip() == before_head and " M src/a.py" in git(repo, "status", "--porcelain")
    assert workflow_runs.workspace_fingerprint(repo)["digest"] != fingerprint, "integration must invalidate earlier verification evidence"
    assert result["worker"]["state"] == "integrated" and result["worker"]["requires_after_integration"]
    fails(lambda: worktrees.integrate(worker["id"], current["digest"]), "integrated")
    fails(lambda: worktrees.prepare_integration(worker["id"]), "integrated")


def test_user_changes_stale_bases_scope_and_symlinks_block_integration():
    repo, task = make_repo()
    worker = worktrees.create_worker(task["id"], "w1", ["src/a.py", "src/b.py"])
    (tree(worker) / "src" / "a.py").write_text("A = 'worker'\n")
    (repo / "src" / "a.py").write_text("A = 'user, uncommitted'\n")
    blocked = worktrees.prepare_integration(worker["id"])
    assert not blocked["can_integrate"] and any("Uncommitted changes in the main workspace" in p for p in blocked["problems"])
    fails(lambda: worktrees.integrate(worker["id"], blocked["digest"]), "uncommitted changes")
    assert (repo / "src" / "a.py").read_text() == "A = 'user, uncommitted'\n", "the user's work must survive a refused integration"
    git(repo, "checkout", "--", "src/a.py")
    (repo / "src" / "b.py").write_text("B = 'main moved'\n"); git(repo, "commit", "-aq", "-m", "main moves")
    (tree(worker) / "src" / "b.py").write_text("B = 'worker'\n")
    stale = worktrees.prepare_integration(worker["id"])
    assert stale["stale_base"] and any("Stale base" in p and "src/b.py" in p for p in stale["problems"]), stale["problems"]
    (tree(worker) / "src" / "b.py").write_text("B = 1\n")                                  # the worker drops the clashing file
    (repo / "docs" / "readme.md").write_text("# Docs v2\n"); git(repo, "commit", "-aq", "-m", "unrelated")
    movable = worktrees.prepare_integration(worker["id"])
    assert movable["stale_base"] and movable["can_integrate"], "a moved base is fine when nothing overlaps and the patch applies"
    (tree(worker) / "docs").mkdir(exist_ok=True); (tree(worker) / "docs" / "readme.md").write_text("# worker docs\n")
    out = worktrees.prepare_integration(worker["id"])
    assert any("outside the worker's scope" in p and "docs/readme.md" in p for p in out["problems"]) and not out["can_integrate"]
    fails(lambda: worktrees.integrate(worker["id"], out["digest"]), "outside the worker's scope")
    repo2, task2 = make_repo()
    linker = worktrees.create_worker(task2["id"], "linker", ["src/**"])
    os.symlink("/etc/passwd", tree(linker) / "src" / "evil")
    unsafe = worktrees.prepare_integration(linker["id"])
    assert not unsafe["can_integrate"] and any("Symlinks" in p for p in unsafe["problems"]), unsafe["problems"]
    assert not (repo2 / "src" / "evil").exists()


def test_disjoint_workers_integrate_independently():
    repo, task = make_repo()
    one = worktrees.create_worker(task["id"], "one", ["src/**"])
    two = worktrees.create_worker(task["id"], "two", ["docs/**"])
    (tree(one) / "src" / "a.py").write_text("A = 'one'\n")
    (tree(two) / "docs" / "readme.md").write_text("# two\n")
    worktrees.integrate(one["id"], worktrees.prepare_integration(one["id"])["digest"])
    still = worktrees.prepare_integration(two["id"])
    assert still["can_integrate"] and still["files"] == ["docs/readme.md"]
    worktrees.integrate(two["id"], still["digest"])
    assert (repo / "src" / "a.py").read_text() == "A = 'one'\n" and (repo / "docs" / "readme.md").read_text() == "# two\n"


def test_parent_cancellation_stops_workers_and_their_runs_and_keeps_the_work():
    repo, task = make_repo()
    worker = worktrees.create_worker(task["id"], "w", ["src/**"])
    (tree(worker) / "src" / "a.py").write_text("A = 'unfinished'\n")
    run, _ = workflow_runs.store.create(worker["path"], "policy", workflow_runs.workspace_fingerprint(tree(worker)), [{"role": "user", "content": "go"}])
    workflow.update_task(task["id"], task["version"], {"state": "paused"})
    after = worktrees._read(worker["id"])
    assert after["state"] == "cancelled" and workflow_runs.store.get(run["id"], recover=False)["cancel_requested"] is True
    assert "A = 'unfinished'" in (worktrees._root() / f"{worker['id']}.patch").read_text(), "cancelled work stays recoverable"
    fails(lambda: worktrees.prepare_integration(worker["id"]), "cancelled")
    fails(lambda: worktrees.integrate(worker["id"], "0" * 64), "cancelled")
    other, task2 = make_repo()
    live = worktrees.create_worker(task2["id"], "w", ["src/**"])
    (tree(live) / "src" / "a.py").write_text("A = 'late'\n")
    reviewed = worktrees.prepare_integration(live["id"])
    workflow.update_task(task2["id"], task2["version"], {"state": "cancelled"})
    fails(lambda: worktrees.integrate(live["id"], reviewed["digest"]), "cancelled")


def test_cleanup_is_recoverable_and_orphans_are_reported():
    repo, task = make_repo()
    worker = worktrees.create_worker(task["id"], "w", ["src/**"])
    (tree(worker) / "src" / "a.py").write_text("A = 'keep me'\n")
    fails(lambda: worktrees.cleanup(worker["id"]), "integrate or abandon")
    worktrees.abandon(worker["id"])
    fails(lambda: worktrees.abandon(worker["id"]), "already abandoned")
    (tree(worker) / "src" / "scratch.txt").write_text("uncommitted after abandon")
    fails(lambda: worktrees.cleanup(worker["id"]), "uncommitted changes")
    done = worktrees.cleanup(worker["id"], force=True)
    assert done["cleaned"] and not tree(worker).exists() and worker["branch"] in git(repo, "branch", "--list"), "the branch is kept by default"
    assert "keep me" in (worktrees._root() / f"{worker['id']}.patch").read_text()
    assert str(tree(worker)) not in git(repo, "worktree", "list"), "Git no longer lists the removed checkout"
    second = worktrees.create_worker(task["id"], "w2", ["docs/**"])
    worktrees.abandon(second["id"])
    gone = worktrees.cleanup(second["id"], delete_branch=True)
    assert gone["branch_deleted"] and second["branch"] not in git(repo, "branch", "--list")
    third = worktrees.create_worker(task["id"], "w3", ["docs/**"])
    import shutil
    shutil.rmtree(tree(third))
    stray = worktrees._root() / ("f" * 32); stray.mkdir()
    listing = worktrees.list_workers(task["id"])
    health = {w["id"]: w["health"] for w in listing["workers"]}
    assert health[third["id"]] == "missing" and health[worker["id"]] == "ok" and "f" * 32 in listing["orphaned_directories"]
    fails(lambda: worktrees.prepare_integration(third["id"]), "missing")


def gate_snapshot(repo, fingerprints=True):
    """A real gate observing a real passing check, as the chat loop produces."""
    check = 'python3 -c "import sys; sys.exit(0)"'
    configured = dict(load_settings()["harness"].get("verification_workspaces") or {})
    configured[str(repo)] = {"commands": [{"id": "tests", "command": check}]}
    settings(verification_workspaces=configured)
    gate = VerificationGate(True, str(repo), load_settings(), fingerprints=fingerprints)
    args = {"command": check}
    gate.before_execution("run_shell", args)
    done = subprocess.run(check, shell=True, cwd=str(repo), capture_output=True, text=True)
    gate.observe("run_shell", args, {"command": check, "cwd": str(repo), "returncode": done.returncode, "stdout": "", "stderr": ""}, executed=True)
    return gate.snapshot()


def test_completion_after_integration_needs_fresh_checks_on_the_integrated_content():
    repo, task = make_repo()
    before = gate_snapshot(repo)
    assert workflow.complete_task(workflow.get_task(task["id"]), before) == "verified_complete", "no integration, no extra rule"
    worker = worktrees.create_worker(task["id"], "w", ["src/**"])
    (tree(worker) / "src" / "a.py").write_text("A = 'integrated'\n")
    worktrees.integrate(worker["id"], worktrees.prepare_integration(worker["id"])["digest"])
    parent = workflow.get_task(task["id"])
    assert parent["post_integration"]["worker_id"] == worker["id"] and parent["post_integration"]["files"] == ["src/a.py"]
    fails(lambda: workflow.complete_task(parent, before), "worker was integrated")                 # evidence from before the patch
    fails(lambda: workflow.complete_task(parent, gate_snapshot(repo, fingerprints=False)), "worker was integrated")   # cannot be bound to content
    assert workflow.complete_task(parent, gate_snapshot(repo)) == "verified_complete"
    (repo / "src" / "a.py").write_text("A = 'edited after the checks'\n")
    stale = gate_snapshot(repo)
    (repo / "src" / "a.py").write_text("A = 'edited again after the checks'\n")
    fails(lambda: workflow.complete_task(parent, stale), "worker was integrated")                    # content changed since those checks


def test_worker_runs_are_linked_and_their_steps_charged_to_the_parent_once():
    repo, task = make_repo(max_steps=20)
    worker = worktrees.create_worker(task["id"], "w", ["src/**"], {"max_steps": 2, "max_seconds": 120})
    run, _ = workflow_runs.store.create(worker["path"], "policy", workflow_runs.workspace_fingerprint(tree(worker)), [{"role": "user", "content": "go"}])
    workflow_runs.store.claim(run["id"])
    for index in range(3):
        workflow_runs.store.dispatch(run["id"], f"1-{index}", "read_file", {"path": "src/a.py"}, "read_only")
        workflow_runs.store.result(run["id"], f"1-{index}", {"content": "A = 1"})
    listed = worktrees.list_workers(task["id"])["workers"][0]
    assert listed["runs"] == [{"id": run["id"], "phase": "running", "outcome": None, "steps": 3}], "the run is linked to its worker"
    (tree(worker) / "src" / "a.py").write_text("A = 'x'\n")
    assert worktrees.prepare_integration(worker["id"])["can_integrate"], "an unfinished run is not charged yet"
    assert workflow.get_task(task["id"])["usage"]["steps"] == 0
    workflow_runs.store.finish(run["id"], "complete")
    blocked = worktrees.prepare_integration(worker["id"])
    assert any("3 tool steps, over its allocation of 2" in p for p in blocked["problems"]) and not blocked["can_integrate"]
    worktrees.prepare_integration(worker["id"])                                                       # settling is idempotent
    assert workflow.get_task(task["id"])["usage"]["steps"] == 3 and worktrees._read(worker["id"])["steps_used"] == 3
    fails(lambda: worktrees.integrate(worker["id"], blocked["digest"]), "over its allocation")


def test_http_contract_rejects_unknown_fields_and_surfaces_conflicts():
    repo, task = make_repo()
    client = TestClient(app, base_url="http://127.0.0.1:7860")
    assert client.post("/api/workflow/worktrees", json={"task_id": task["id"], "owner": "w", "scope": ["src/**"], "yolo": 1}).status_code == 400
    assert client.post("/api/workflow/worktrees", json={"task_id": task["id"], "owner": "w", "scope": ["**"]}).status_code == 409
    created = client.post("/api/workflow/worktrees", json={"task_id": task["id"], "owner": "w", "scope": ["src/**"]})
    assert created.status_code == 200
    worker = created.json()
    assert client.post(f"/api/workflow/worktrees/{worker['id']}/integrate", json={"expected_digest": "x", "force": True}).status_code == 400
    assert client.post(f"/api/workflow/worktrees/{worker['id']}/integrate", json={"expected_digest": "x"}).status_code == 409
    assert client.get(f"/api/workflow/worktrees/{worker['id']}/integration").json()["can_integrate"] is False
    assert [w["id"] for w in client.get("/api/workflow/worktrees", params={"task_id": task["id"]}).json()["workers"]] == [worker["id"]]
    assert client.get("/api/workflow/worktrees/not-an-id/integration").status_code == 409


TESTS = (test_flags_and_repository_requirements, test_worker_gets_an_isolated_checkout_and_the_parent_is_untouched,
         test_children_cannot_exceed_the_parent_scope_budgets_count_or_ownership,
         test_integration_applies_only_the_reviewed_patch_without_committing,
         test_user_changes_stale_bases_scope_and_symlinks_block_integration, test_disjoint_workers_integrate_independently,
         test_parent_cancellation_stops_workers_and_their_runs_and_keeps_the_work,
         test_cleanup_is_recoverable_and_orphans_are_reported, test_completion_after_integration_needs_fresh_checks_on_the_integrated_content,
         test_worker_runs_are_linked_and_their_steps_charged_to_the_parent_once, test_http_contract_rejects_unknown_fields_and_surfaces_conflicts)

if __name__ == "__main__":
    for function in TESTS:
        function()
        print("   ok " + function.__name__)
    print("   workflow worktree checks OK")
