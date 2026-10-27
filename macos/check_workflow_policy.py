"""Offline workflow policy checks with real isolated execution and artifacts."""
import json
import os
import shlex
import sys
import tempfile
import time
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='ls-policy-', dir='/tmp')).resolve()
os.environ['LOCAL_STUDIO_HOME'] = str(TMP / 'state')
os.environ['HF_HUB_CACHE'] = str(TMP / 'hub')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio import config
from studio.infra.workspace import _in_workspace
from studio.infra.processes import _execution_cancel
from studio.security import workflow_policy as policy
from studio.security.execution import SANDBOX_EXEC, seatbelt_profile
from studio.services import harness, workflow
from studio.storage.settings import load_settings, save_settings


def settings(**values):
    current = load_settings()
    current['harness'].update(values)
    save_settings(current)
    return current


def fails(fn, text):
    try:
        fn()
    except (ValueError, OSError) as error:
        assert text.lower() in str(error).lower(), (text, error)
    else:
        raise AssertionError('Expected rejection: ' + text)


def act(ws, name, args, session=None, on_output=None):
    token = policy._active_policy.set(session)
    try:
        return _in_workspace(str(ws), harness._chat_action_tool, name, args, on_output)
    finally:
        policy._active_policy.reset(token)


def test_protection_enforces_every_native_and_opaque_path():
    ws = TMP / 'protected'; (ws / 'tests').mkdir(parents=True)
    target = ws / 'tests' / 'test_value.py'; target.write_text('x = 1\n')
    settings(protect_tests=True)
    for name, args in [('write_file', {'path': 'tests/test_value.py', 'content': 'x=2'}),
                       ('edit_file', {'path': 'tests/test_value.py', 'old_text': '1', 'new_text': '2'}),
                       ('multi_edit_file', {'path': 'tests/test_value.py', 'edits': [{'old_text': '1', 'new_text': '2'}]}),
                       ('run_python', {'code': "open('tests/test_value.py','w').write('bad')"}),
                       ('run_shell', {'command': 'pytest'}), ('mcp__fake__edit', {})]:
        result = act(ws, name, args)
        assert result.get('policy_blocked') and result.get('error'), (name, result)
        assert target.read_text() == 'x = 1\n'
    outside = TMP / 'outside.py'
    result = act(ws, 'write_file', {'path': str(outside), 'content': 'bad'})
    assert result.get('policy_blocked') and not outside.exists()
    (ws / 'link').symlink_to(ws / 'tests', target_is_directory=True)
    assert act(ws, 'write_file', {'path': 'link/test_value.py', 'content': 'bad'}).get('policy_blocked')
    assert act(ws, 'write_file', {'path': 'module.py', 'content': 'x = 1\n'}).get('status') == 'written'
    session = policy.PolicySession(str(ws))
    for tool in ('use_computer', 'delegate_write'):
        fails(lambda: session.before_tool(tool, {}), 'opaque')
    settings(workspace_policies={str(ws): {'test_exceptions': {'tests/test_value.py': 'User authorized updating this fixture'}}})
    assert act(ws, 'edit_file', {'path': 'tests/test_value.py', 'old_text': '1', 'new_text': '2'}).get('status') == 'edited'
    assert target.read_text() == 'x = 2\n'
    settings(workspace_policies={str(ws): {'protected_tests': 'tests'}})
    fails(lambda: policy.PolicySession(str(ws)), 'bounded list')
    settings(workspace_policies={})


def test_plain_chat_without_a_workspace_is_not_governed_by_a_coding_policy():
    """With protect_tests or hooks on, a chat that has no workspace used to be treated as if the checkout (or home) were one: every
    write elsewhere was refused as 'outside the workspace' and a file called test_plan.py was refused as a protected test."""
    settings(protect_tests=True, hooks=True, workspace_policies={})
    try:
        target = TMP / 'no-workspace' / 'test_plan.py'; target.parent.mkdir()
        token = policy._active_policy.set(None)
        try:
            result = harness._chat_action_tool('write_file', {'path': str(target), 'content': 'x = 1\n'}, None)
            with policy.action_scope('write_file', {'path': str(target)}) as session:
                assert not session.enabled and session.workspace is None
        finally:
            policy._active_policy.reset(token)
        assert result.get('status') == 'written' and not result.get('policy_blocked'), result
        assert target.read_text() == 'x = 1\n'
        ws = TMP / 'still-protected'; (ws / 'tests').mkdir(parents=True); (ws / 'tests' / 'test_a.py').write_text('x = 1\n')
        assert act(ws, 'write_file', {'path': 'tests/test_a.py', 'content': 'bad'}).get('policy_blocked'), 'a real workspace stays protected'
    finally:
        settings(protect_tests=False, hooks=False, workspace_policies={})


def test_configured_verifier_is_really_readonly():
    ws = TMP / 'readonly'; ws.mkdir()
    (ws / 'source.py').write_text('original')
    code = """from pathlib import Path
import os,json
blocked=[]
for kind,fn in [('existing',lambda:Path('source.py').write_text('bad')),('new',lambda:Path('new.py').write_text('bad')),('rename',lambda:os.rename('source.py','renamed.py')),('link',lambda:os.link('source.py','hard.py'))]:
    try:fn()
    except PermissionError:blocked.append(kind)
print(json.dumps(blocked))
"""
    command = shlex.join([sys.executable, '-I', '-c', code])
    settings(protect_tests=True, workspace_policies={}, verification_workspaces={str(ws): {'commands': [{'id': 'readonly', 'command': command}]}})
    profile = seatbelt_profile(ws, TMP / 'scratch', readonly=True)
    assert f'(allow file-write* (subpath {json.dumps(str(ws))})' not in profile
    result = act(ws, 'run_shell', {'command': command})
    if Path(SANDBOX_EXEC).is_file():
        assert result.get('returncode') == 0, result
        assert json.loads(result['stdout']) == ['existing', 'new', 'rename', 'link'], result
    else:
        assert 'unavailable' in result.get('error', ''), result
    assert (ws / 'source.py').read_text() == 'original'
    assert sorted(p.name for p in ws.iterdir()) == ['source.py']
    # Models habitually write `cd <workspace> && <check>`: that is the same command in the same directory, so it is accepted;
    # anything else around the configured command is not.
    prefixed = act(ws, 'run_shell', {'command': f'cd {ws} && {command}'})
    if Path(SANDBOX_EXEC).is_file():
        assert prefixed.get('returncode') == 0 and json.loads(prefixed['stdout']) == ['existing', 'new', 'rename', 'link'], prefixed
    for bad in (f'cd /tmp && {command}', f'cd {ws} && {command}; echo extra', f'cd {ws}; {command}', f'echo hi && cd {ws} && {command}'):
        assert act(ws, 'run_shell', {'command': bad}).get('policy_blocked'), bad
    assert sorted(p.name for p in ws.iterdir()) == ['source.py']


def test_hooks_deny_timeout_and_preserve_executed_results():
    ws = TMP / 'hooks'; ws.mkdir()
    def configure(phase, code):
        settings(protect_tests=False, hooks=True, workspace_policies={str(ws): {'hooks': {
            phase: [{'id': 'test-hook', 'argv': [sys.executable, '-I', '-c', code],
                     'timeout_seconds': 1, 'tools': ['write_file']}]
        }}})
    configure('PreToolUse', "print('{\"allow\":false,\"reason\":\"fixture denied\"}')")
    result = act(ws, 'write_file', {'path': 'no.py', 'content': 'x=1'})
    assert 'fixture denied' in result.get('error', '') and not (ws / 'no.py').exists(), result
    configure('PreToolUse', "print('{\"allow\":\"false\"}')")
    assert act(ws, 'write_file', {'path': 'no.py', 'content': 'x=1'}).get('policy_blocked')
    configure('PreToolUse', 'import time;time.sleep(3)')
    before = time.monotonic()
    result = act(ws, 'write_file', {'path': 'no.py', 'content': 'x=1'})
    assert 'timed out' in result.get('error', '') and time.monotonic()-before < 3 and not (ws / 'no.py').exists(), result
    configure('PostToolUse', "print('{\"allow\":false,\"reason\":\"post denied\"}')")
    result = act(ws, 'write_file', {'path': 'written.py', 'content': 'x=1'})
    assert result.get('post_hook_failed') and result.get('operation_executed') and (ws / 'written.py').read_text() == 'x=1', result
    records = [json.loads(p.read_text()) for p in (config.STATE / 'data/workflow/audit').glob('*.json')]
    assert any(e['kind'] == 'hook_error' and 'timed out' in e['payload']['error'] for r in records for e in r['events'])
    assert all([e['sequence'] for e in r['events']] == list(range(1,len(r['events'])+1)) for r in records)
    settings(hooks=False, protect_tests=False, workspace_policies={})


def test_hooks_run_in_declared_order_stop_at_first_denial_and_never_widen_authority():
    ws = TMP / 'hook-order'; (ws / 'tests').mkdir(parents=True)
    allow = "print('{\"allow\":true}')"
    deny = "print('{\"allow\":false,\"reason\":\"second says no\"}')"
    def configure(second):
        hooks = [{'id': name, 'argv': [sys.executable, '-I', '-c', code], 'timeout_seconds': 3, 'tools': ['write_file', 'read_file']}
                 for name, code in (('first', allow), ('second', second), ('third', allow))]
        settings(hooks=True, protect_tests=False, workspace_policies={str(ws): {'hooks': {'PreToolUse': hooks}}})
    ids = lambda session: [e['payload']['id'] for e in session.events if e['kind'] == 'hook']
    configure(allow)
    session = policy.PolicySession(str(ws))
    assert act(ws, 'write_file', {'path': 'ok.py', 'content': 'x=1'}, session).get('status') == 'written'
    assert ids(session) == ['first', 'second', 'third'], 'hooks run in declared order, once each'
    configure(deny)
    session = policy.PolicySession(str(ws))
    result = act(ws, 'write_file', {'path': 'no.py', 'content': 'x=1'}, session)
    assert 'second says no' in result.get('error', '') and not (ws / 'no.py').exists(), result
    assert ids(session) == ['first', 'second'], 'the first denial stops the chain: the third hook never runs'
    # A hook that says "allow" cannot lift a fence, a path boundary or test protection: those are evaluated first.
    configure(allow)
    settings(protect_tests=True, hooks=True, workspace_policies={str(ws): {'hooks': {'PreToolUse': [
        {'id': 'permissive', 'argv': [sys.executable, '-I', '-c', allow], 'timeout_seconds': 3, 'tools': ['*']}]}}})
    session = policy.PolicySession(str(ws))
    for path in ('tests/test_value.py', str(TMP / 'outside.py'), '../escape.py'):
        result = act(ws, 'write_file', {'path': path, 'content': 'bad'}, session)
        assert result.get('policy_blocked') and result.get('error'), (path, result)
    assert not (ws / 'tests' / 'test_value.py').exists() and not (TMP / 'outside.py').exists()
    assert ids(session) == [], 'denied by policy before any hook was consulted'
    # Executable hooks never run in Plan mode, even for read-only tools.
    settings(protect_tests=False, hooks=True, workspace_policies={str(ws): {'hooks': {'PreToolUse': [
        {'id': 'plan-hook', 'argv': [sys.executable, '-I', '-c', allow], 'timeout_seconds': 3, 'tools': ['read_file']}]}}})
    plan = policy.PolicySession(str(ws), mode='plan')
    fails(lambda: plan.before_tool('read_file', {'path': 'ok.py'}), 'disabled in Plan mode')
    assert ids(plan) == []
    # With the feature off the configured hooks are inert and behavior is unchanged.
    settings(hooks=False, protect_tests=False)
    quiet = policy.PolicySession(str(ws))
    assert not quiet.enabled and act(ws, 'write_file', {'path': 'plain.py', 'content': 'x=1'}, quiet).get('status') == 'written'
    settings(hooks=False, protect_tests=False, workspace_policies={})


def test_modes_versioning_scope_and_artifact_changes():
    ws = TMP / 'tasks'; ws.mkdir()
    settings(workflow=True, hooks=False, protect_tests=False)
    task = workflow.create_task(str(ws), 'Fix value', 'value must equal 2', 'Inspect, edit, verify', scope=['src/**'])
    assert all(workflow.get_task(task['id'])['artifact_status'].values())
    session = workflow.start_session({'workflow_task_id': task['id']}, str(ws))
    workflow.claim_session(session)
    for name, args in [('write_file', {'path':'src/value.py','content':'x=1'}), ('run_shell', {'command':'echo no'}),
                       ('use_computer', {}), ('run_python', {'code':'print(1)'}), ('mcp__fake__edit', {}),
                       ('delegate_write', {}), ('manage_process', {'action':'stop'})]:
        fails(lambda:session.before_tool(name,args), 'Plan mode')
    session.before_tool('read_file', {'path':'src/value.py'})
    hint = session.guidance()
    assert 'PLAN MODE' in hint and 'plan' in hint.lower() and 'cannot edit files' in hint, hint
    fails(lambda:session.before_tool('write_file',{'path':'src/value.py','content':'x'}), 'write your plan as the final answer')
    fails(lambda:workflow.update_task(task['id'],task['version'],{'mode':'act'}), 'Pause')
    workflow.release_session(session, 'planned')
    task=workflow.update_task(task['id'],task['version'],{'mode':'act','state':'ready'})
    fails(lambda:workflow.update_task(task['id'],1,{'mode':'plan'}), 'changed')
    session=workflow.start_session({'workflow_task_id':task['id']},str(ws))
    workflow.claim_session(session)
    fails(lambda:session.before_tool('write_file', {'path':'other.py'}), 'outside')
    session.before_tool('write_file', {'path':'src/value.py'})
    path=Path(task['artifacts']['plan.md']['path']);path.write_text('User changed scope\n')
    fails(lambda:session.before_tool('read_file', {'path':'src/value.py'}), 'artifacts changed')
    workflow.release_session(session, 'needs_user')
    task=workflow.update_task(task['id'],task['version'],{'adopt_artifacts':True,'state':'ready'})
    assert task['artifacts']['plan.md']['version']==2 and all(workflow.get_task(task['id'])['artifact_status'].values())
    session=workflow.start_session({'workflow_task_id':task['id']},str(ws))
    fails(lambda:session.finalize({'status':'unverified'}),'requires successful')
    assert session.finalize({'status':'verified'})=='verified_complete'
    session.max_seconds=1;session.started=time.monotonic()-2
    fails(lambda:session.before_tool('read_file',{}),'budget')
    # Interrupted task state requires explicit user continuation; opening history cannot execute anything.
    task['state']='running'; workflow._write(task)
    recovered=workflow.get_task(task['id'])
    assert recovered['state']=='needs_user' and recovered['interrupted']
    settings(workflow=False)
    fails(lambda:workflow.start_session({'workflow_mode':'plan'},str(ws)),'disabled')
    assert not policy.PolicySession(str(ws)).enabled


def test_durable_task_budgets_leases_and_cancel():
    ws=TMP/'task-budgets';ws.mkdir()
    settings(workflow=True,hooks=False,protect_tests=False)
    task=workflow.create_task(str(ws),'Intent','Spec','Plan',mode='act',budgets={'max_steps':2})
    session=workflow.start_session({'workflow_task_id':task['id']},str(ws))
    workflow.claim_session(session)
    session.preview('read_file',{'path':'value.py'})
    assert workflow.get_task(task['id'])['usage']['steps']==0, 'Approval preview charged the task'
    session.before_tool('read_file',{'path':'value.py'})
    child="from studio.services import workflow; s=workflow.start_session({'workflow_task_id':"+repr(task['id'])+"},"+repr(str(ws))+");workflow.claim_session(s)"
    result=subprocess.run([sys.executable,'-c',child],capture_output=True,text=True,env=os.environ.copy())
    assert result.returncode and ('already running' in result.stderr or 'active turn' in result.stderr),result.stderr
    workflow.release_session(session,'needs_user')
    session=workflow.start_session({'workflow_task_id':task['id']},str(ws));workflow.claim_session(session)
    session.before_tool('read_file',{'path':'value.py'})
    fails(lambda:session.before_tool('read_file',{'path':'value.py'}),'total execution budget')
    assert workflow.get_task(task['id'])['usage']['steps']==2
    workflow.release_session(session,'needs_user')
    # Pausing a task stops the actual foreground process, not only the next round.
    task=workflow.create_task(str(ws),'Cancel','No marker','Stop process',mode='act')
    session=workflow.start_session({'workflow_task_id':task['id']},str(ws));workflow.claim_session(session)
    marker=ws/'cancel-marker'
    code="import pathlib,time;print('ready',flush=True);time.sleep(4);pathlib.Path('cancel-marker').write_text('bad')"
    ready=threading.Event()
    def execute():
        token=_execution_cancel.set(session.cancel)
        try:return act(ws,'run_python',{'code':code},session,lambda chunk:ready.set() if 'ready' in chunk else None)
        finally:_execution_cancel.reset(token)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(execute)
        assert ready.wait(2),'The scratch subprocess did not start'
        workflow.update_task(task['id'],task['version'],{'state':'paused'})
        result=future.result(timeout=3)
    assert 'stopped' in result.get('error','').lower() and not marker.exists(),result
    workflow.release_session(session)
    assert workflow.get_task(task['id'])['state']=='paused'
    settings(workflow=False)


if __name__ == '__main__':
    for function in (test_protection_enforces_every_native_and_opaque_path,
                     test_plain_chat_without_a_workspace_is_not_governed_by_a_coding_policy,
                     test_configured_verifier_is_really_readonly,
                     test_hooks_deny_timeout_and_preserve_executed_results,
                     test_hooks_run_in_declared_order_stop_at_first_denial_and_never_widen_authority,
                     test_modes_versioning_scope_and_artifact_changes,
                     test_durable_task_budgets_leases_and_cancel):
        function()
        print('   ok ' + function.__name__)
    print('   workflow policy checks OK')
