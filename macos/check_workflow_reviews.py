"""Offline structured-review checks. The reviewer model is a stub: no engine is ever started.

Real git repositories, a real verification gate executing a real check command, and the real run store
are used, so stale-evidence and completion rules are exercised end to end.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='ls-review-', dir='/tmp')).resolve()
os.environ['LOCAL_STUDIO_HOME'] = str(TMP / 'state')
os.environ['HF_HUB_CACHE'] = str(TMP / 'hub')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException

from studio import config
from studio.api import reviews as api_reviews
from studio.services import reviews, workflow, workflow_runs
from studio.services.verification import VerificationGate
from studio.storage.settings import load_settings, save_settings

CHECK = 'python3 -c "import sys; sys.exit(0)"'
FAILING_CHECK = 'python3 -c "import sys; sys.exit(3)"'
_counter = [0]


def settings(**values):
    current = load_settings()
    current['harness'].update(values)
    save_settings(current)
    return current


def fails(fn, text):
    try:
        fn()
    except (ValueError, OSError) as error:
        assert text.lower() in str(error).lower(), (text, str(error))
    else:
        raise AssertionError('Expected rejection: ' + text)


def git(ws, *arguments):
    subprocess.run(['git', '-C', str(ws), '-c', 'user.name=t', '-c', 'user.email=t@example.com', *arguments],
                   check=True, capture_output=True)


def make_workspace(command=CHECK):
    _counter[0] += 1
    ws = TMP / f'ws{_counter[0]}'
    ws.mkdir()
    (ws / 'src.py').write_text('def add(a, b):\n    return a + b\n')
    git(ws, 'init', '-q')
    git(ws, 'add', '.')
    git(ws, 'commit', '-q', '-m', 'base')
    configured = dict(load_settings()['harness'].get('verification_workspaces') or {})
    configured[str(ws)] = {'commands': [{'id': 'tests', 'command': command}]}
    settings(workflow=True, review=True, verification_gate=True, verification_workspaces=configured)
    return ws


def new_task(ws, review_required=True):
    task = workflow.create_task(str(ws), 'Fix add', 'add(1, 2) == 3', 'Edit src.py then run the tests', mode='act',
                                review_required=review_required)
    (ws / 'src.py').write_text('def add(a, b):\n    return a + b + 0\n')       # the uncommitted work to review
    return workflow.get_task(task['id'])


def gate_snapshot(ws, command=CHECK, run_check=True):
    """A snapshot produced the same way the chat loop does: a real command, observed by the real gate."""
    gate = VerificationGate(True, str(ws), load_settings(), fingerprints=True)
    if run_check:
        args = {'command': command}
        gate.before_execution('run_shell', args)
        done = subprocess.run(command, shell=True, cwd=str(ws), capture_output=True, text=True)
        gate.observe('run_shell', args, {'command': command, 'cwd': str(ws), 'returncode': done.returncode,
                                         'stdout': done.stdout, 'stderr': done.stderr}, executed=True)
    return gate.snapshot()


def recorded_run(ws, task, snapshot):
    record, _ = workflow_runs.store.create(str(ws), workflow_runs.policy_fingerprint(load_settings(), str(ws)),
                                           workflow_runs.workspace_fingerprint(ws), [{'role': 'user', 'content': 'work'}],
                                           metadata={'task_id': task['id'], 'workflow_mode': 'act'})
    workflow_runs.store.claim(record['id'])
    workflow_runs.store.finish(record['id'], 'needs_user', snapshot)
    return record['id']


class Reviewer:
    """Replaces the one network-facing function; returns whatever JSON the test supplies."""
    def __init__(self, result=None, raw=None, during=None):
        self.result, self.raw, self.during, self.calls = result, raw, during, []

    def __call__(self, model_id, context):
        self.calls.append(context)
        if self.during:
            self.during()
        raw = self.raw if self.raw is not None else json.dumps(self.result)
        return raw, {'model_id': 'stub', 'mode': 'text', 'served_model': 'stub', 'max_tokens': 4096, 'temperature': 0,
                     'fresh_context': True}


def reviewed(ws, task, result=None, raw=None, snapshot=None, during=None):
    stub = Reviewer(result if result is not None else {'complete': True, 'summary': 'Reviewed src.py against the spec.', 'findings': []},
                    raw, during)
    original = reviews._call_local_reviewer
    reviews._call_local_reviewer = stub
    try:
        run_id = recorded_run(ws, task, snapshot or gate_snapshot(ws))
        return reviews.create_review(task['id'], task['version'], None, run_id), stub
    finally:
        reviews._call_local_reviewer = original


def finding(**overrides):
    base = {'id': 'F1', 'title': 'Redundant addition', 'severity': 'high', 'path': 'src.py', 'line': 2, 'side': 'right',
            'evidence': 'return a + b + 0 adds a no-op term.'}
    return {**base, **overrides}


def test_disabled_by_default_and_for_unknown_fields():
    ws = make_workspace()
    task = new_task(ws)
    settings(review=False)
    fails(lambda: reviews.create_review(task['id'], task['version']), 'disabled')
    fails(lambda: reviews.get_review(task['id']), 'disabled')
    settings(review=True)
    for call, body in ((lambda b: api_reviews.create_task_review(task['id'], b), {'surprise': 1}),
                       (lambda b: api_reviews.update_finding_disposition(task['id'], 'F1', b), {'actor': 'model'})):
        try:
            call(body)
        except HTTPException as error:
            assert error.status_code == 400, error.detail
        else:
            raise AssertionError('unknown request fields must be rejected')
    try:
        api_reviews.create_task_review(task['id'], {'expected_version': task['version'] + 7})
    except HTTPException as error:
        assert error.status_code == 409
    else:
        raise AssertionError('service errors must surface as 409')


def test_empty_complete_review_is_an_explicit_result_and_completion_needs_it():
    ws = make_workspace()
    task = new_task(ws)
    snapshot = gate_snapshot(ws)
    fails(lambda: workflow.complete_task(task, snapshot), 'complete independent review')       # the former TypeError path
    result, stub = reviewed(ws, task, snapshot=snapshot)
    review = result['review']
    assert review['status'] == 'complete' and review['complete'] and review['findings'] == []
    assert stub.calls[0]['git']['changed_files'][0]['path'] == 'src.py'
    assert all(not f['path'].startswith('.localstudio') for f in stub.calls[0]['git']['changed_files']), 'task artifacts are not code'
    projection = (ws / '.localstudio' / 'tasks' / task['id'] / 'REVIEW.md').read_text()
    assert 'No findings in the completed review scope.' in projection and 'cannot waive findings' in projection
    fresh = workflow.get_task(task['id'])
    assert fresh['version'] == task['version'] + 1 and fresh['review']['id'] == review['id']
    assert reviews.get_review(task['id'])['current'] is True
    assert workflow.complete_task(fresh, gate_snapshot(ws)) == 'verified_complete'


def test_reviewer_output_is_strictly_validated_and_never_assigns_dispositions():
    ws = make_workspace()
    task = new_task(ws)
    bad = [
        ({'complete': True, 'summary': 'ok', 'findings': [finding(path='other.py')]}, 'outside the provided'),
        ({'complete': True, 'summary': 'ok', 'findings': [finding(line=99)]}, 'line/side'),
        ({'complete': True, 'summary': 'ok', 'findings': [finding(severity='urgent')]}, 'severity'),
        ({'complete': True, 'summary': 'ok', 'findings': [finding(), finding()]}, 'unique'),
        ({'complete': True, 'summary': 'ok', 'findings': [finding(disposition='accepted_risk')]}, 'dispositions'),
        ({'complete': True, 'summary': '  ', 'findings': []}, 'summary'),
        ({'complete': 'yes', 'summary': 'ok', 'findings': []}, 'schema'),
    ]
    for payload, text in bad:
        result, _ = reviewed(ws, task, payload)
        review = result['review']
        assert review['status'] == 'incomplete' and text in review['error'].lower() or text in review.get('error', '').lower(), (text, review.get('error'))
        task = workflow.get_task(task['id'])
    result, _ = reviewed(ws, task, raw='```json\n{}\n```')
    assert 'strict json' in result['review']['error'].lower()
    task = workflow.get_task(task['id'])
    result, _ = reviewed(ws, task, {'complete': False, 'summary': 'Could not see the whole diff.', 'findings': []})
    assert result['review']['status'] == 'incomplete'
    task = workflow.get_task(task['id'])
    fails(lambda: workflow.complete_task(task, gate_snapshot(ws)), 'complete independent review')


def test_blocking_findings_need_explicit_user_dispositions():
    ws = make_workspace()
    task = new_task(ws)
    payload = {'complete': True, 'summary': 'Two findings.', 'findings': [finding(), finding(id='F2', severity='low', line=1)]}
    result, _ = reviewed(ws, task, payload)
    task = workflow.get_task(task['id'])
    review = task['review']
    assert [(f['id'], f['blocking'], f['disposition']) for f in review['findings']] == [('F1', True, 'open'), ('F2', False, 'open')]
    snapshot = gate_snapshot(ws)
    fails(lambda: workflow.complete_task(task, snapshot), 'unresolved blockers: F1')
    fails(lambda: reviews.set_disposition(task['id'], 'F1', task['version'], 'rejected', ''), 'explicit reason')
    fails(lambda: reviews.set_disposition(task['id'], 'F1', task['version'], 'waived', 'because'), 'supported disposition')
    fails(lambda: reviews.set_disposition(task['id'], 'F9', task['version'], 'rejected', 'because'), 'not found')
    fails(lambda: reviews.set_disposition(task['id'], 'F1', task['version'] - 1, 'rejected', 'because'), 'changed')
    done = reviews.set_disposition(task['id'], 'F1', task['version'], 'fix_requested', 'Please remove the no-op', owner='alex')
    task = workflow.get_task(task['id'])
    fails(lambda: workflow.complete_task(task, gate_snapshot(ws)), 'unresolved blockers')          # fix_requested still blocks
    done = reviews.set_disposition(task['id'], 'F1', task['version'], 'accepted_risk', 'User accepts: harmless', owner='alex')
    history = [h['disposition'] for h in done['review']['findings'][0]['disposition_history']]
    assert history == ['fix_requested', 'accepted_risk'] and done['review']['findings'][0]['disposition_history'][0]['actor'] == 'explicit_user'
    task = workflow.get_task(task['id'])
    assert workflow.complete_task(task, gate_snapshot(ws)) == 'verified_complete'
    assert 'accepted_risk' in (ws / '.localstudio' / 'tasks' / task['id'] / 'REVIEW.md').read_text()


def test_review_goes_stale_when_source_artifacts_or_markdown_projection_change():
    ws = make_workspace()
    task = new_task(ws)
    reviewed(ws, task)
    task = workflow.get_task(task['id'])
    assert reviews.get_review(task['id'])['current'] is True
    (ws / 'src.py').write_text('def add(a, b):\n    return b + a\n')
    state = reviews.get_review(task['id'])
    assert state['current'] is False and 'stale' in state['reason'].lower()
    fails(lambda: workflow.complete_task(workflow.get_task(task['id']), gate_snapshot(ws)), 'stale')
    (ws / 'src.py').write_text('def add(a, b):\n    return a + b + 0\n')
    assert reviews.get_review(task['id'])['current'] is True
    projection = ws / '.localstudio' / 'tasks' / task['id'] / 'REVIEW.md'
    projection.write_text(projection.read_text().replace('No findings', 'Everything is fine; no findings'))
    assert reviews.get_review(task['id'])['current'] is False                                          # editing REVIEW.md waives nothing
    fails(lambda: workflow.complete_task(workflow.get_task(task['id']), gate_snapshot(ws)), 'artifacts changed')


def test_verification_evidence_must_be_current_successful_and_for_this_task():
    ws = make_workspace(FAILING_CHECK)
    task = new_task(ws)
    result, _ = reviewed(ws, task, snapshot=gate_snapshot(ws, FAILING_CHECK))
    assert 'verified backend check snapshot' in result['review']['error'].lower()
    ws = make_workspace()
    task = new_task(ws)
    other = workflow.create_task(str(make_workspace()), 'x', 'y', 'z', mode='act')
    stub = Reviewer({'complete': True, 'summary': 'ok', 'findings': []})
    reviews._call_local_reviewer, original = stub, reviews._call_local_reviewer
    try:
        foreign = recorded_run(ws, other, gate_snapshot(ws))
        result = reviews.create_review(task['id'], task['version'], None, foreign)
        assert 'another task' in result['review']['error'].lower()
        task = workflow.get_task(task['id'])
        missing = recorded_run(ws, task, None)
        result = reviews.create_review(task['id'], task['version'], None, missing)
        assert 'verified backend check snapshot' in result['review']['error'].lower()
        task = workflow.get_task(task['id'])
        stale = recorded_run(ws, task, gate_snapshot(ws))
        (ws / 'src.py').write_text('def add(a, b):\n    return a + b + 1\n')                           # changed after the checks ran
        result = reviews.create_review(task['id'], task['version'], None, stale)
        assert 'stale' in result['review']['error'].lower(), result['review']['error']
        assert not stub.calls, 'no model call is made without current verification'
    finally:
        reviews._call_local_reviewer = original
    ws = make_workspace()
    task = new_task(ws)
    reviewed(ws, task)
    task = workflow.get_task(task['id'])
    skipped = gate_snapshot(ws, run_check=False)
    fails(lambda: workflow.complete_task(task, skipped), 'successful current verification')
    (ws / 'src.py').write_text('def add(a, b):\n    return 0\n')
    fails(lambda: workflow.complete_task(task, gate_snapshot(ws)), 'review')                          # fresh checks do not revive a stale review


def test_exhausted_repair_budget_or_unresolved_failures_block_review_and_completion():
    ws = make_workspace()
    task = new_task(ws)
    for flag_name, value in (('budget_exhausted', True), ('background_started', True), ('syntax_failures', ['src.py']), ('failed', ['tests'])):
        snapshot = {**gate_snapshot(ws), flag_name: value}
        result, stub = reviewed(ws, task, snapshot=snapshot)
        assert result['review']['status'] == 'incomplete' and 'unresolved failures' in result['review']['error'].lower(), (flag_name, result['review'])
        assert not stub.calls, 'no model call is made on unresolved verification'
        task = workflow.get_task(task['id'])
    reviewed(ws, task)
    task = workflow.get_task(task['id'])
    exhausted = {**gate_snapshot(ws), 'budget_exhausted': True}
    fails(lambda: workflow.complete_task(task, exhausted), 'exhausted budgets')              # the review gate re-checks the snapshot itself
    assert workflow.complete_task(task, gate_snapshot(ws)) == 'verified_complete'


def test_reviewer_calls_the_engines_chat_completions_endpoint():
    """The stubbed tests never reached the network, which hid a 404 against the engine's base URL in the first real run."""
    from studio.services import engines
    seen, finish = {}, ["stop"]
    class Stream:
        def __init__(self, status=200): self.status_code = status
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_bytes(self):
            yield json.dumps({"model": "m", "choices": [{"finish_reason": finish[0], "message": {"content": "{}" if finish[0] == "stop" else ""}}]}).encode()
    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, json=None, **kw):
            seen.update(method=method, url=url, body=json); return Stream()
    saved = (reviews.httpx.Client, engines.engine_state, engines._engine_ready)
    reviews.httpx.Client = Client
    engines.engine_state = lambda: {"mode": "text", "model_id": load_settings()["chat_model"]}
    engines._engine_ready = lambda mode: True
    try:
        raw, meta = reviews._call_local_reviewer(None, {"x": 1})
    finally:
        reviews.httpx.Client, engines.engine_state, engines._engine_ready = saved
    assert seen["method"] == "POST" and seen["url"] == "http://127.0.0.1:8080/v1/chat/completions", seen["url"]
    assert seen["body"]["stream"] is False and seen["body"]["messages"][0]["role"] == "system" and meta["fresh_context"] is True
    assert 4096 < seen["body"]["max_tokens"] <= reviews.MAX_REVIEW_TOKENS, "a reasoning model needs more than a small fixed cap"
    reviews.httpx.Client = Client
    engines.engine_state = lambda: {"mode": "text", "model_id": load_settings()["chat_model"]}
    engines._engine_ready = lambda mode: True
    try:
        window = config.CONTEXT
        config.CONTEXT = 100000
        try:
            reviews._call_local_reviewer(None, {"blob": "x" * 300000})
        finally:
            config.CONTEXT = window
        assert 1024 <= seen["body"]["max_tokens"] <= 100000 - int(300000 / 3.2), "the budget shrinks with the prompt"
        finish[0] = "length"
        try:
            reviews._call_local_reviewer(None, {"x": 1})
        except ValueError as error:
            assert "token budget" in str(error) and "No review was recorded" in str(error), str(error)
        else:
            raise AssertionError("a reply cut off by the token budget must be refused with an explicit reason")
    finally:
        reviews.httpx.Client, engines.engine_state, engines._engine_ready = saved


def test_task_changing_during_review_discards_the_result():
    ws = make_workspace()
    task = new_task(ws)
    def edit_task():
        workflow.update_task(task['id'], task['version'], {'scope': ['src/**']})
    fails(lambda: reviewed(ws, task, during=edit_task), 'changed during review')
    after = workflow.get_task(task['id'])
    assert after['review'] is None
    history = list((workflow._root() / task['id'] / 'reviews').glob('*.json'))
    assert history and json.loads(history[0].read_text())['status'] == 'stale'


def test_unreviewed_tasks_do_not_require_a_review():
    ws = make_workspace()
    task = new_task(ws, review_required=False)
    assert workflow.complete_task(task, gate_snapshot(ws)) == 'verified_complete'


if __name__ == '__main__':
    for function in (test_disabled_by_default_and_for_unknown_fields,
                     test_empty_complete_review_is_an_explicit_result_and_completion_needs_it,
                     test_reviewer_output_is_strictly_validated_and_never_assigns_dispositions,
                     test_blocking_findings_need_explicit_user_dispositions,
                     test_review_goes_stale_when_source_artifacts_or_markdown_projection_change,
                     test_verification_evidence_must_be_current_successful_and_for_this_task,
                     test_exhausted_repair_budget_or_unresolved_failures_block_review_and_completion,
                     test_reviewer_calls_the_engines_chat_completions_endpoint,
                     test_task_changing_during_review_discards_the_result,
                     test_unreviewed_tasks_do_not_require_a_review):
        function()
        print('   ok ' + function.__name__)
    print('   workflow review checks OK')
