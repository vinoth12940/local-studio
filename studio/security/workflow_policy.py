"""Request-scoped workflow policy, bounded hooks, and test-file protection.

Configured hooks are trusted executables; their text/output never grants an
application permission. Opaque execution is refused in Plan. With test protection
enabled, only configured foreground verification may execute, with kernel file
write restrictions. Unsupported sandboxes fail closed.
"""
import contextvars
import fnmatch
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from studio import config
from studio.infra.workspace import _harness_root
from studio.security.execution import _readonly_workspace
from studio.security.fences import _harness_path_fence
from studio.security.redact import redact_obj
from studio.storage.atomic import write_text_atomic
from studio.storage.flags import flag
from studio.storage.settings import load_settings

_active_policy = contextvars.ContextVar('active_workflow_policy', default=None)
_inside_action = contextvars.ContextVar('inside_workflow_action', default=False)
_policy_execution = contextvars.ContextVar('workflow_execution', default=False)
FILE_TOOLS = frozenset(('write_file', 'edit_file', 'multi_edit_file'))
OPAQUE_TOOLS = frozenset(('run_shell', 'run_python', 'use_computer'))
DEFAULT_TEST_PATTERNS = ('tests/**', 'test/**', '**/tests/**', '**/test/**',
                         'test_*.py', '**/test_*.py', '*_test.py', '**/*_test.py',
                         '*.test.*', '**/*.test.*', '*.spec.*', '**/*.spec.*', 'macos/check_*')


def canonical_target(workspace, value, write=False):
    base = Path(workspace).resolve()
    raw = Path(str(value)).expanduser()
    if not raw.is_absolute():
        raw = base / raw
    # Reject intermediate symlinks too: a path resolved before a later rename
    # must not appear to acquire a new write authority.
    if any(part.is_symlink() for part in (raw, *raw.parents) if part != base.parent):
        raise ValueError('Workflow paths cannot traverse symlinks.')
    target = _harness_path_fence(str(raw), write=write)
    if not target.is_relative_to(base):
        raise ValueError('The tool target is outside the selected workflow workspace.')
    return target


def _matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) or
               (pattern.startswith('**/') and fnmatch.fnmatchcase(path, pattern[3:])) for pattern in patterns)


class PolicySession:
    def __init__(self, workspace, mode='act', task=None, audit=None, settings=None, validate_task=None, finish_task=None, charge_task_step=None):
        self.workspace = str(Path(workspace).resolve()) if workspace else None
        self.mode = mode
        if mode not in ('plan', 'act', 'auto'):
            raise ValueError('Workflow mode must be plan, act, or auto.')
        self.task = json.loads(json.dumps(task)) if task else None
        settings = settings if settings is not None else load_settings()
        harness = settings.get('harness', {})
        self.protected = bool(self.workspace and flag('harness.protect_tests'))
        self.hooks_enabled = bool(self.workspace and flag('harness.hooks'))
        self.enabled = bool(self.workspace and (self.task or self.mode != 'act' or self.protected or self.hooks_enabled))
        policies = harness.get('workspace_policies', {})
        self.options = json.loads(json.dumps(policies.get(self.workspace, {}))) if self.enabled and isinstance(policies, dict) else {}
        if not isinstance(self.options, dict):
            raise ValueError('Workspace policy must be an object.')
        patterns = self.options.get('protected_tests', DEFAULT_TEST_PATTERNS)
        if not isinstance(patterns, (list, tuple)) or not 1 <= len(patterns) <= 64:
            raise ValueError('protected_tests must be a bounded list of path patterns.')
        self.patterns = tuple(patterns)
        self.exceptions = self.options.get('test_exceptions', {})
        if not all(isinstance(p, str) and p and not p.startswith('/') and '..' not in Path(p).parts for p in self.patterns):
            raise ValueError('Invalid protected test path patterns.')
        if not isinstance(self.exceptions, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in self.exceptions.items()):
            raise ValueError('Test exceptions require exact relative paths and a recorded reason.')
        configured = harness.get('verification_workspaces', {}).get(self.workspace, {}) if self.workspace else {}
        self.commands = tuple(item.get('command') for item in configured.get('commands', []) if isinstance(item, dict) and isinstance(item.get('command'), str))
        self.hooks = self._validate_hooks(self.options.get('hooks', {})) if self.hooks_enabled else {}
        self.started = time.monotonic()
        limits = (self.task or {}).get('budgets', {})
        self.max_steps = max(1, min(int(limits.get('max_steps', 48)), 120))
        self.max_seconds = max(1, min(int(limits.get('max_seconds', 900)), 3600))
        self.steps = 0
        self.cancel = threading.Event()
        self.audit_id = uuid.uuid4().hex
        self.events = []
        self.audit_callback = audit
        self.validate_task = validate_task
        self.finish_task = finish_task
        self.charge_task_step = charge_task_step
        self.lock = threading.RLock()
        self.digest = hashlib.sha256(json.dumps({'mode': mode, 'task': self.task, 'options': self.options,
                                                'protected': self.protected, 'hooks_enabled': self.hooks_enabled,
                                                'commands': self.commands}, sort_keys=True).encode()).hexdigest()

    def _validate_hooks(self, hooks):
        if not isinstance(hooks, dict) or set(hooks) - {'PreToolUse', 'PostToolUse'}:
            raise ValueError('Hooks must use PreToolUse or PostToolUse.')
        answer = {}
        for phase, items in hooks.items():
            if not isinstance(items, list) or len(items) > 16:
                raise ValueError('At most 16 hooks are allowed per phase.')
            answer[phase] = []
            ids = set()
            for item in items:
                argv = item.get('argv') if isinstance(item, dict) else None
                hid = item.get('id') if isinstance(item, dict) else None
                if not isinstance(hid, str) or not hid or hid in ids or not isinstance(argv, list) or not 1 <= len(argv) <= 32 or not all(isinstance(a, str) and a and len(a) <= 4000 and '\x00' not in a for a in argv):
                    raise ValueError('A hook requires a distinct id and bounded argv array.')
                if not Path(argv[0]).is_absolute():
                    raise ValueError('Hook executables must use absolute paths.')
                _harness_path_fence(argv[0], write=False)
                tools = item.get('tools', ['*'])
                if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                    raise ValueError('Hook tools must be a list of tool name patterns.')
                answer[phase].append({'id': hid, 'argv': list(argv), 'tools': tools,
                                      'timeout': max(1, min(int(item.get('timeout_seconds', 5)), 10))})
                ids.add(hid)
        return answer

    def record(self, kind, payload):
        if not self.enabled:
            return
        with self.lock:
            payload = redact_obj(payload) if flag('security.redact') else payload
            event = {'sequence': len(self.events) + 1, 'kind': kind, 'at': time.time(), 'policy_sha256': self.digest,
                     'payload': payload}
            if len(self.events) >= 1000:
                raise ValueError('Workflow policy audit budget exhausted.')
            self.events.append(event)
            path = config.STATE / 'data' / 'workflow' / 'audit' / (self.audit_id + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, json.dumps({'version': 1, 'workspace': self.workspace,
                                              'task_id': (self.task or {}).get('id'), 'events': self.events}))
            if self.audit_callback:
                self.audit_callback('policy_' + kind, payload)

    def _check(self, name, args, charge=True):
        if not self.enabled:
            return
        if self.cancel.is_set():
            raise ValueError('Workflow was cancelled.')
        if time.monotonic() - self.started >= self.max_seconds or self.steps >= self.max_steps:
            raise ValueError('Workflow execution budget exhausted.')
        if charge:
            self.steps += 1
        if self.task:
            if not self.validate_task:
                raise ValueError('A durable task requires its task validation callback.')
            self.validate_task(self.task)
            if charge and self.charge_task_step:
                self.charge_task_step(self.task)
        opaque = name in OPAQUE_TOOLS or name.startswith('mcp__') or name == 'delegate_write'
        if self.mode == 'plan' and (name in FILE_TOOLS or opaque or name in ('memory_store', 'manage_process')):
            raise ValueError('Plan mode permits native inspection and designated planning-artifact API writes only.')
        if name in FILE_TOOLS:
            target = canonical_target(self.workspace, args.get('path', ''), write=True)
            rel = target.relative_to(Path(self.workspace)).as_posix()
            if self.task and not _matches(rel, self.task.get('scope', ['**'])):
                raise ValueError('The file is outside the selected task scope.')
            if self.protected and _matches(rel, self.patterns) and rel not in self.exceptions:
                raise ValueError('Protected test file: an explicit settings exception with a reason is required.')
        if self.protected and opaque:
            check = name == 'run_shell' and not args.get('background') and args.get('command') in self.commands
            if not check:
                raise ValueError('Test protection refuses opaque shell/Python/MCP/computer/delegated writes. Use native file tools or a configured read-only verification command.')
            if args.get('cwd') and canonical_target(self.workspace, args['cwd']) != Path(self.workspace):
                raise ValueError('Protected verification must run in the selected workspace.')
        if self.task and self.task.get('scope', ['**']) != ['**'] and opaque and not (self.protected and name == 'run_shell' and args.get('command') in self.commands):
            raise ValueError('A path-scoped task cannot grant opaque execution outside its assigned files.')

    def _run_hooks(self, phase, name, args, result=None):
        for hook in self.hooks.get(phase, []):
            if not _matches(name, hook['tools']):
                continue
            # Hooks are executable code. Plan mode does not run them; refusing is
            # explicit rather than pretending that a hook cannot have side effects.
            if self.mode == 'plan':
                raise ValueError('Executable hooks are disabled in Plan mode.')
            from studio.infra.processes import _run_streamed_subprocess
            event = {'version': 1, 'phase': phase, 'tool': name, 'args': args, 'result': result,
                     'workspace': self.workspace, 'policy_sha256': self.digest}
            wire = json.dumps(event, ensure_ascii=False)
            if len(wire) > 20000:
                raise ValueError('Hook event exceeds its input budget.')
            env = {'PATH': os.defpath, 'LANG': 'en_US.UTF-8', 'PYTHONIOENCODING': 'utf-8',
                   'LOCAL_STUDIO_HOOK_EVENT': wire}
            token = _readonly_workspace.set(True)
            try:
                rc, stdout, stderr = _run_streamed_subprocess(hook['argv'], Path(self.workspace), env,
                                                             timeout=hook['timeout'])
                outcome = json.loads(stdout) if stdout.strip() else {}
                if not isinstance(outcome, dict):
                    raise ValueError('Hook output must be a JSON object.')
                if ('allow' in outcome and type(outcome['allow']) is not bool) or ('reason' in outcome and not isinstance(outcome['reason'], str)):
                    raise ValueError('Hook allow must be boolean and reason must be text.')
                self.record('hook', {'id': hook['id'], 'phase': phase, 'tool': name, 'returncode': rc,
                                     'allow': outcome.get('allow', True), 'stderr': stderr[:2000]})
                if rc != 0 or outcome.get('allow') is False:
                    raise ValueError('Hook ' + hook['id'] + ' refused: ' + str(outcome.get('reason') or stderr or f'exit {rc}')[:1000])
            except (OSError, ValueError) as error:
                self.record('hook_error', {'id': hook['id'], 'phase': phase, 'error': str(error)[:1000]})
                raise ValueError('Hook failed closed: ' + str(error)[:1000]) from error
            finally:
                _readonly_workspace.reset(token)

    def before_tool(self, name, args):
        with self.lock:
            try:
                self._check(name, args)
                self._run_hooks('PreToolUse', name, args)
            except ValueError as error:
                self.record('denied', {'tool': name, 'reason': str(error)})
                raise
            self.record('allowed', {'tool': name})

    def preview(self, name, args):
        """Check immutable constraints before showing an approval dialog."""
        with self.lock:
            count = self.steps
            try:
                self._check(name, args, charge=False)
            except ValueError as error:
                self.record('denied_preview', {'tool': name, 'reason': str(error)})
                raise
            finally:
                self.steps = count

    def after_tool(self, name, args, result):
        if self.enabled:
            self._run_hooks('PostToolUse', name, args, result)
            self.record('result', {'tool': name, 'error': result.get('error'), 'returncode': result.get('returncode')})

    def guidance(self):
        return (f'Workflow mode: {self.mode}; task: {(self.task or {}).get("id", "none")}. '
                f'Budget: {self.max_steps} tools / {self.max_seconds}s. '
                'Tool policy is enforced by the backend; approval cannot override Plan mode, assigned scope, or test protection.')

    def finalize(self, verification_status):
        if self.task:
            if not self.finish_task:
                raise ValueError('A durable task requires its completion callback.')
            return self.finish_task(self.task, verification_status)
        return 'verified_complete' if verification_status.get('status') == 'verified' else 'complete'


@contextmanager
def action_scope(name, args):
    """Enforce at the execution boundary, including editor and delegate callers."""
    policy = _active_policy.get()
    own_policy = False
    if policy is None:
        policy = PolicySession(str(_harness_root()))
        own_policy = True
    if _inside_action.get() or not policy.enabled:
        yield policy
        return
    policy.before_tool(name, args)
    token = _inside_action.set(True)
    ptoken = _active_policy.set(policy) if own_policy else None
    readonly = _readonly_workspace.set(bool(policy.protected and name == 'run_shell'))
    try:
        yield policy
    finally:
        _readonly_workspace.reset(readonly)
        if ptoken is not None:
            _active_policy.reset(ptoken)
        _inside_action.reset(token)
