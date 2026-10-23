"""Explicit local user controls for coding tasks and project planning artifacts."""
from fastapi import APIRouter, Body, HTTPException

from studio.services import workflow
from studio.storage.flags import flag
from studio.storage.settings import load_settings, save_settings

router = APIRouter()
FEATURES = ('workflow', 'durable_runs', 'verification_gate', 'verification_fingerprints',
            'nested_instructions', 'skills', 'hooks', 'protect_tests', 'review', 'triggers', 'worktrees')


@router.get('/api/workflow/status')
def workflow_status():
    return {'enabled': flag('harness.workflow'), 'features': {name: flag('harness.' + name) for name in FEATURES},
            'modes': ['plan', 'act', 'auto']}


@router.post('/api/workflow/settings')
def workflow_settings(body: dict = Body(default={})):
    if set(body) - set(FEATURES) or any(type(value) is not bool for value in body.values()):
        raise HTTPException(400, 'Workflow settings must contain known boolean feature switches.')
    settings = load_settings()
    settings.setdefault('harness', {}).update(body)
    save_settings(settings)
    return workflow_status()


@router.get('/api/workflow/checks')
def workflow_checks(workspace: str):
    try:
        path = str(workflow.workspace_path(workspace))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return load_settings().get('harness', {}).get('verification_workspaces', {}).get(path, {'commands': []})


@router.post('/api/workflow/checks')
def save_workflow_checks(body: dict = Body(default={})):
    if set(body) - {'workspace', 'commands', 'max_repair_attempts', 'elapsed_budget_seconds'}:
        raise HTTPException(400, 'Unknown verification configuration fields.')
    try:
        path = str(workflow.workspace_path(body.get('workspace')))
        commands = body.get('commands')
        if not isinstance(commands, list) or not 1 <= len(commands) <= 16:
            raise ValueError('Configure 1–16 required checks.')
        ids = set()
        for item in commands:
            if not isinstance(item, dict) or set(item) != {'id', 'command'} or not all(isinstance(v, str) and v.strip() for v in item.values()) or len(item['command']) > 4000 or '\x00' in item['command'] or item['id'] in ids:
                raise ValueError('Each check requires a distinct id and a nonempty command of at most 4000 characters.')
            ids.add(item['id'])
        limits = {'max_repair_attempts': (0, 12, 2), 'elapsed_budget_seconds': (1, 3600, 300)}
        settings = load_settings()
        selected = settings['harness'].setdefault('verification_workspaces', {}).setdefault(path, {})
        selected['commands'] = commands
        for key, (low, high, default) in limits.items():
            value = body.get(key, selected.get(key, default))
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{key} must be in {low}..{high}.')
            selected[key] = value
        save_settings(settings)
        return selected
    except (ValueError, TypeError) as error:
        raise HTTPException(400, str(error)) from error


def _enabled():
    if not flag('harness.workflow'):
        raise HTTPException(409, 'Workflow tasks are disabled.')


def _call(function, *args, **kwargs):
    _enabled()
    try:
        return function(*args, **kwargs)
    except (ValueError, OSError) as error:
        raise HTTPException(409, str(error)) from error


@router.get('/api/workflow/tasks')
def list_workflow_tasks(workspace: str | None = None):
    return _call(workflow.list_tasks, workspace)


@router.post('/api/workflow/tasks')
def create_workflow_task(body: dict = Body(default={})):
    allowed = {'workspace', 'intent', 'spec', 'plan', 'mode', 'scope', 'budgets', 'review_required'}
    if set(body) - allowed:
        raise HTTPException(400, 'Unknown workflow task fields.')
    return _call(workflow.create_task, body.get('workspace'), body.get('intent'), body.get('spec'), body.get('plan'),
                 body.get('mode', 'plan'), body.get('scope'), body.get('budgets'), body.get('review_required', False))


@router.get('/api/workflow/tasks/{task_id}')
def get_workflow_task(task_id: str):
    return _call(workflow.get_task, task_id)


@router.patch('/api/workflow/tasks/{task_id}')
def update_workflow_task(task_id: str, body: dict = Body(default={})):
    if set(body) - {'expected_version', 'changes'}:
        raise HTTPException(400, 'Unknown task update fields.')
    return _call(workflow.update_task, task_id, body.get('expected_version'), body.get('changes'))
