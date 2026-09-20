"""Resume guards use isolated persisted checkpoints; no model or search calls."""
import json

import pytest
from fastapi.testclient import TestClient

from app.generation_agent import authorize_resume
from app.models import GenerateRequest
from app.store import dumps
from test_workflow import rig, authenticate


def failed_task(rig, exploration_raw=None):
    client, app, wire = rig
    app.state.settings.generation_workflow = 'agent_v1'
    course = client.post('/api/courses', json={'name': 'Resume fixture'}).json()['id']
    store = app.state.store
    owner = store.one('SELECT user_id FROM course_owners WHERE course_id=?', (course,))['user_id']
    request = GenerateRequest(course_id=course, topic='Explain retrieval', request_key='exploration-resume-fixture', auto_explore=True)
    job, _ = store.job(request.request_key, 'generate', request.model_dump(), owner_id=owner)
    store.execute("UPDATE jobs SET status='failed',error='fixture interruption' WHERE id=?", (job['id'],))
    evidence = {'agent': {'status': 'provider_error', 'phase': 'retrieval', 'resume_count': 0,
                          'call_count': 0, 'slots': [{'status': 'pending'}]}, 'sources': []}
    store.save_job_evidence(job['id'], evidence)
    if exploration_raw is not None:
        store.execute('INSERT INTO exploration_runs VALUES(?,?)', (job['id'], exploration_raw))
    return job['id']


def unchanged_snapshot(store, job_id):
    return (store.one('SELECT * FROM jobs WHERE id=?', (job_id,)),
            store.one('SELECT * FROM job_evidence WHERE job_id=?', (job_id,)),
            store.one('SELECT * FROM exploration_runs WHERE job_id=?', (job_id,)))


@pytest.mark.parametrize('raw', ['broken JSON', 'null', '[]', 'true', '{}',
    dumps({'status': 'running', 'in_flight': 'searching_sources'}),
    dumps({'status': 'interrupted'}), dumps({'status': 'failed'}), dumps({'status': []})])
def test_incomplete_exploration_disables_resume_and_rejects_without_mutation(rig, monkeypatch, raw):
    client, app, wire = rig
    job_id = failed_task(rig, raw)
    store = app.state.store
    enqueued = []
    monkeypatch.setattr(app.state.pipeline, 'enqueue', enqueued.append)
    before = unchanged_snapshot(store, job_id)
    response = client.get('/api/jobs/' + job_id)
    assert response.status_code == 200
    assert response.json()['progress']['resumable'] is False
    response = client.post('/api/jobs/' + job_id + '/resume')
    assert response.status_code == 409 and '新建任务' in response.json()['detail']
    with pytest.raises(ValueError, match='新建任务'):
        authorize_resume(store, job_id)
    assert unchanged_snapshot(store, job_id) == before
    assert not enqueued and wire.calls == 0


@pytest.mark.parametrize('raw', [None, dumps({'status': 'complete', 'result': {'selected': [], 'trace': {}}})])
def test_no_acquisition_or_completed_acquisition_preserves_later_generation_resume(rig, monkeypatch, raw):
    client, app, wire = rig
    job_id = failed_task(rig, raw)
    enqueued = []
    monkeypatch.setattr(app.state.pipeline, 'enqueue', enqueued.append)
    response = client.get('/api/jobs/' + job_id)
    assert response.json()['progress']['resumable'] is True
    response = client.post('/api/jobs/' + job_id + '/resume')
    assert response.status_code == 200 and response.json()['resumed'] is True
    store = app.state.store
    assert store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'] == 'queued'
    state = json.loads(store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (job_id,))['evidence'])['agent']
    assert state['resume_count'] == 1 and state['status'] == 'running'
    assert enqueued == [job_id] and wire.calls == 0


def test_exploration_resume_keeps_job_ownership_boundary(rig, monkeypatch):
    client, app, wire = rig
    job_id = failed_task(rig, dumps({'status': 'interrupted'}))
    enqueued = []
    monkeypatch.setattr(app.state.pipeline, 'enqueue', enqueued.append)
    other = TestClient(app)
    other.portal = client.portal
    try:
        authenticate(other, 'other-resume-fixture@example.test')
        assert other.get('/api/jobs/' + job_id).status_code == 404
        assert other.post('/api/jobs/' + job_id + '/resume').status_code == 404
    finally:
        other.close()
        other.portal = None
    assert not enqueued and wire.calls == 0
