"""Durable coding tasks, versioned planning artifacts, and mode transitions."""
import fcntl
import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from studio import config
from studio.security.fences import _harness_path_fence
from studio.security.workflow_policy import PolicySession, canonical_target
from studio.storage.atomic import write_text_atomic
from studio.storage.flags import flag

ARTIFACTS = ('intent.md', 'spec.md', 'plan.md')
ID_PATTERN = re.compile(r'^[a-f0-9]{32}$')
ACTIVE_TASKS = {}


def _root():
    root = config.STATE / 'data' / 'workflow' / 'tasks'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _id(task_id):
    if not isinstance(task_id, str) or not ID_PATTERN.fullmatch(task_id):
        raise ValueError('Invalid workflow task id.')
    return task_id


@contextmanager
def _locked(task_id):
    tid = _id(task_id)
    with (_root() / (tid + '.lock')).open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _read(task_id):
    path = _root() / (_id(task_id) + '.json')
    if not path.is_file() or path.is_symlink():
        raise ValueError('Workflow task not found.')
    if path.stat().st_size > 2_000_000:
        raise ValueError('Workflow task record is too large.')
    task = json.loads(path.read_text())
    if task.get('schema_version') != 1 or task.get('id') != task_id:
        raise ValueError('Unsupported or corrupt workflow task.')
    return task


def _is_active(task_id):
    with (_root() / (_id(task_id) + '.active')).open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False


def _write(task):
    task['updated_at'] = time.time()
    write_text_atomic(_root() / (task['id'] + '.json'), json.dumps(task, indent=2))


def workspace_path(workspace):
    if not isinstance(workspace, str) or not workspace:
        raise ValueError('A workspace is required.')
    path = _harness_path_fence(workspace, write=False)
    if not path.is_dir() or path in (Path('/'), config.HOME.resolve(), config.ROOT.resolve() if '.app' in str(config.ROOT) else Path('/')):
        raise ValueError('Select a specific existing project workspace.')
    return path


def _artifact_path(task, name):
    if name not in (*ARTIFACTS, 'REVIEW.md'):
        raise ValueError('Unknown task artifact.')
    return canonical_target(task['workspace'], f'.localstudio/tasks/{task["id"]}/{name}', write=True)


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _artifact_text(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 100_000:
        raise ValueError('Task artifacts must be nonempty strings of at most 100 KB.')
    return value.rstrip() + '\n'


def _scope(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 64 or not all(isinstance(p, str) and p and not Path(p).is_absolute() and '..' not in Path(p).parts for p in value):
        raise ValueError('Task scope must contain relative path patterns inside the workspace.')
    return list(dict.fromkeys(value))


def _save_artifact(task, name, text):
    text = _artifact_text(text)
    path = _artifact_path(task, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    revision = task.get('artifacts', {}).get(name, {}).get('version', 0) + 1
    history = _root() / task['id'] / 'artifacts' / name
    history.mkdir(parents=True, exist_ok=True)
    write_text_atomic(history / (str(revision) + '.md'), text)
    write_text_atomic(path, text)
    task.setdefault('artifacts', {})[name] = {'version': revision, 'sha256': _hash(text), 'path': str(path)}


def create_task(workspace, intent, spec, plan, mode='plan', scope=None, budgets=None, review_required=False):
    if not flag('harness.workflow'):
        raise ValueError('Workflow tasks are disabled.')
    base = workspace_path(workspace)
    if mode not in ('plan', 'act', 'auto'):
        raise ValueError('Unknown workflow mode.')
    if type(review_required) is not bool:
        raise ValueError('review_required must be a boolean.')
    if review_required and not flag('harness.review'):
        raise ValueError('Reviewed completion is disabled; enable harness.review first.')
    budgets = budgets or {}
    if not isinstance(budgets, dict):
        raise ValueError('Task budgets must be an object.')
    maximum = {'max_steps': 120, 'max_seconds': 3600, 'max_children': 4}
    defaults = {'max_steps': 48, 'max_seconds': 900, 'max_children': 2}
    effective = {}
    for key, cap in maximum.items():
        value = budgets.get(key, defaults[key])
        if type(value) is not int or not 1 <= value <= cap:
            raise ValueError(f'{key} must be an integer in 1..{cap}.')
        effective[key] = value
    task = {'schema_version': 1, 'id': uuid.uuid4().hex, 'version': 1, 'workspace': str(base),
            'mode': mode, 'state': 'ready', 'scope': _scope(scope or ['**']), 'budgets': effective,
            'review_required': review_required, 'review': None, 'artifacts': {}, 'created_at': time.time(),
            'usage': {'steps': 0, 'seconds': 0.0}, 'active_started_at': None}
    # Validate every input before writing any artifacts.
    texts = {name: _artifact_text(value) for name, value in zip(ARTIFACTS, (intent, spec, plan))}
    with _locked(task['id']):
        for name, text in texts.items():
            _save_artifact(task, name, text)
        _write(task)
    return task


def artifact_status(task):
    statuses = {}
    for name, info in task['artifacts'].items():
        path = _artifact_path(task, name)
        try:
            current = path.read_text() if path.stat().st_size <= 100_000 else None
            statuses[name] = bool(current is not None and _hash(current) == info['sha256'])
        except OSError:
            statuses[name] = False
    return statuses


def get_task(task_id):
    with _locked(task_id):
        task = _read(task_id)
        if task['state'] == 'running' and not _is_active(task_id):
            _settle_usage(task)
            task['state'] = 'needs_user'
            task['interrupted'] = True
            _write(task)
        return {**task, 'artifact_status': artifact_status(task)}


def list_tasks(workspace=None):
    selected = str(workspace_path(workspace)) if workspace else None
    result = []
    for path in sorted(_root().glob('*.json'), reverse=True)[:500]:
        try:
            task = get_task(path.stem)
            if selected is None or task['workspace'] == selected:
                result.append(task)
        except (ValueError, OSError, json.JSONDecodeError):
            continue
    return sorted(result, key=lambda item: item['updated_at'], reverse=True)


def update_task(task_id, expected_version, changes):
    allowed = {'mode', 'state', 'scope', 'artifacts', 'adopt_artifacts', 'review_required'}
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise ValueError('Unsupported workflow task changes.')
    with _locked(task_id):
        task = _read(task_id)
        if type(expected_version) is not int or task['version'] != expected_version:
            raise ValueError('Task changed; reload it before updating.')
        if _is_active(task_id):
            if changes in ({'state': 'paused'}, {'state': 'cancelled'}):
                if task_id in ACTIVE_TASKS:
                    ACTIVE_TASKS[task_id].cancel.set()
            else:
                raise ValueError('Pause the active task before changing scope, mode, or artifacts.')
        if 'mode' in changes:
            if changes['mode'] not in ('plan', 'act', 'auto'):
                raise ValueError('Unknown workflow mode.')
            task['mode'] = changes['mode']
        if 'state' in changes:
            if changes['state'] not in ('ready', 'paused', 'cancelled'):
                raise ValueError('Only ready, paused, or cancelled can be selected explicitly.')
            task['state'] = changes['state']
            if task['state'] in ('paused', 'cancelled'):
                _settle_usage(task)
        if 'scope' in changes:
            task['scope'] = _scope(changes['scope'])
        if 'review_required' in changes:
            if type(changes['review_required']) is not bool or (changes['review_required'] and not flag('harness.review')):
                raise ValueError('Reviewed completion requires the enabled review feature.')
            task['review_required'] = changes['review_required']
        artifacts = changes.get('artifacts', {})
        if not isinstance(artifacts, dict) or set(artifacts) - set(ARTIFACTS):
            raise ValueError('Only intent.md, spec.md, and plan.md are editable planning artifacts.')
        texts = {name: _artifact_text(text) for name, text in artifacts.items()}
        if changes.get('adopt_artifacts'):
            if changes['adopt_artifacts'] is not True:
                raise ValueError('adopt_artifacts must be true.')
            for name in ARTIFACTS:
                path = _artifact_path(task, name)
                if not path.is_file() or path.stat().st_size > 100_000:
                    raise ValueError('An edited artifact is missing or too large.')
                texts.setdefault(name, _artifact_text(path.read_text()))
        # Editing the markdown REVIEW projection never changes authoritative findings.
        for name, text in texts.items():
            _save_artifact(task, name, text)
        task['review'] = None
        task['version'] += 1
        _write(task)
    if changes.get('state') in ('paused', 'cancelled') and flag('harness.worktrees'):
        from studio.services import worktrees
        try:
            worktrees.cancel_children(task_id)          # a paused or cancelled parent stops its isolated workers too
        except (ValueError, OSError):
            pass                                       # pausing must always succeed; stale workers are refused at integration
    return task


def validate_session(snapshot):
    task = get_task(snapshot['id'])
    if task['version'] != snapshot['version'] or task['mode'] != snapshot['mode']:
        raise ValueError('Task mode or scope changed; pause and start a fresh turn.')
    if task['state'] in ('paused', 'cancelled'):
        raise ValueError('Workflow task is ' + task['state'] + '.')
    if not all(task['artifact_status'].values()):
        raise ValueError('Planning or review artifacts changed; explicitly adopt/review the new version first.')
    if task_elapsed(task) >= task['budgets']['max_seconds']:
        raise ValueError('Task total elapsed budget exhausted.')
    return task


def task_elapsed(task):
    used = task.get('usage', {}).get('seconds', 0.0)
    started = task.get('active_started_at')
    if started is not None:
        delta = time.time() - started
        if delta < -1:
            raise ValueError('Clock moved backwards during the task; explicit reconciliation is required.')
        used += max(0, delta)
    return used


def _settle_usage(task):
    try:
        total = task_elapsed(task)
    except ValueError:
        # A regressed clock cannot create extra unattended execution budget.
        total = task['budgets']['max_seconds']
    task.setdefault('usage', {})['seconds'] = total
    task['active_started_at'] = None


def consume_task_step(snapshot):
    with _locked(snapshot['id']):
        task = _read(snapshot['id'])
        if task['version'] != snapshot['version'] or task['state'] != 'running':
            raise ValueError('Task is not running at the selected version.')
        if task_elapsed(task) >= task['budgets']['max_seconds'] or task['usage']['steps'] >= task['budgets']['max_steps']:
            raise ValueError('Task total execution budget exhausted.')
        task['usage']['steps'] += 1
        _write(task)


def start_session(body, workspace):
    task_id = body.pop('workflow_task_id', None)
    requested_mode = body.pop('workflow_mode', None)
    if (task_id or requested_mode is not None) and not flag('harness.workflow'):
        raise ValueError('Workflow modes are disabled.')
    if (task_id or requested_mode is not None) and not workspace:
        raise ValueError('Workflow modes require a selected workspace.')
    task = get_task(task_id) if task_id else None
    if task:
        if not workspace or task['workspace'] != str(Path(workspace).resolve()):
            raise ValueError('Task workspace does not match the selected chat workspace.')
        validate_session(task)
        if requested_mode is not None and requested_mode != task['mode']:
            raise ValueError('Change the task mode explicitly before starting a turn.')
    mode = task['mode'] if task else requested_mode or 'act'
    if mode == 'auto' and task is None:
        raise ValueError('Auto mode requires a durable task with an explicit scope and budgets.')
    if task and task['id'] in ACTIVE_TASKS:
        raise ValueError('This task already has an active turn.')
    session = PolicySession(workspace, mode=mode, task=task, validate_task=validate_session,
                            finish_task=complete_task, charge_task_step=consume_task_step)
    if task:
        session.max_seconds = max(0, min(session.max_seconds, task['budgets']['max_seconds'] - task_elapsed(task)))
    return session


def claim_session(session):
    if not session.task:
        return
    task_id = session.task['id']
    with _locked(task_id):
        validate_session_unlocked(session.task)
        lease = (_root() / (task_id + '.active')).open('a+')
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lease.close()
            raise ValueError('Task is already running.')
        session.task_lease = lease
        ACTIVE_TASKS[task_id] = session
        task = _read(task_id)
        task['state'] = 'running'
        task['active_started_at'] = time.time()
        _write(task)
        session.task_watch_stop = threading.Event()
        def watch():
            while not session.task_watch_stop.wait(.1):
                try:
                    record = _read(task_id)
                    if (record['version'] != session.task['version'] or record['state'] != 'running'
                            or task_elapsed(record) >= record['budgets']['max_seconds']):
                        session.cancel.set()
                        return
                except (ValueError, OSError, json.JSONDecodeError):
                    session.cancel.set()
                    return
        session.task_watcher = threading.Thread(target=watch, daemon=True)
        session.task_watcher.start()


def validate_session_unlocked(snapshot):
    task = _read(snapshot['id'])
    if task['version'] != snapshot['version'] or task['state'] in ('paused', 'cancelled') or not all(artifact_status(task).values()):
        raise ValueError('Task changed, paused, or has stale artifacts.')


def release_session(session, outcome=None):
    if not session.task:
        return
    task_id = session.task['id']
    if getattr(session, 'task_watch_stop', None) is not None:
        session.task_watch_stop.set()
    with _locked(task_id):
        if ACTIVE_TASKS.get(task_id) is session:
            ACTIVE_TASKS.pop(task_id)
        task = _read(task_id)
        if task['state'] == 'running':
            _settle_usage(task)
            task['state'] = outcome or ('cancelled' if session.cancel.is_set() else 'needs_user')
            _write(task)
        lease = getattr(session, 'task_lease', None)
        if lease is not None:
            fcntl.flock(lease, fcntl.LOCK_UN)
            lease.close()
            session.task_lease = None


def complete_task(snapshot, verification_status):
    task = validate_session(snapshot)
    if task['mode'] == 'plan':
        return 'planned'
    if verification_status.get('status') != 'verified':
        raise ValueError('Task completion requires successful current verification checks.')
    integrated = task.get('post_integration')
    if integrated:
        # A worker's patch changed the workspace after any earlier evidence: only checks that ran afterwards, against exactly
        # the current content, count. Without content fingerprints the evidence cannot be bound, so completion is refused.
        from studio.services.verification import VerificationGate
        from studio.storage.settings import load_settings
        recorded = (verification_status.get('fingerprint') or {}).get('digest')
        current = VerificationGate(True, task['workspace'], load_settings(), fingerprints=True).fingerprint() or {}
        if not recorded or recorded != current.get('digest') or verification_status.get('started_wall', 0) < integrated['at']:
            raise ValueError('A worker was integrated: rerun the configured checks against the integrated content '
                             '(requires durable runs or harness.verification_fingerprints) before completion.')
    if task.get('review_required'):
        from studio.services import reviews
        reviews.require_complete(task, verification_status)
    return 'verified_complete'
