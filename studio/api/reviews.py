"""Explicit local-user controls for independent workflow reviews."""
from fastapi import APIRouter, Body, HTTPException

from studio.services import reviews

router = APIRouter()


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ValueError, OSError) as error:
        raise HTTPException(409, str(error)) from error


@router.get('/api/workflow/tasks/{task_id}/review')
def get_task_review(task_id: str):
    return _call(reviews.get_review, task_id)


@router.post('/api/workflow/tasks/{task_id}/review')
def create_task_review(task_id: str, body: dict = Body(default={})):
    if set(body) - {'expected_version', 'model_id', 'verification_run_id'}:
        raise HTTPException(400, 'Unknown review request fields.')
    return _call(reviews.create_review, task_id, body.get('expected_version'), body.get('model_id'), body.get('verification_run_id'))


@router.patch('/api/workflow/tasks/{task_id}/review/findings/{finding_id}')
def update_finding_disposition(task_id: str, finding_id: str, body: dict = Body(default={})):
    if set(body) - {'expected_version', 'disposition', 'reason', 'owner'}:
        raise HTTPException(400, 'Unknown finding disposition fields.')
    return _call(reviews.set_disposition, task_id, finding_id, body.get('expected_version'), body.get('disposition'),
                 body.get('reason'), body.get('owner'))
