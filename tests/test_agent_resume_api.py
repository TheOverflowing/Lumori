"""Resume authorization and checkpoint integrity through real isolated HTTP APIs.

Accounts, sessions, CSRF checks and SQLite are real. enqueue is replaced with a
recording callback so these tests never execute a generation or provider call.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.generation_agent import authorize_resume
from app.models import GenerateRequest
from app.store import uid
from test_account_isolation import another_client, isolated_app, register


@pytest.fixture
def rig(tmp_path, monkeypatch):
    app, wire = isolated_app(tmp_path)
    app.state.settings.generation_workflow = 'agent_v1'
    enqueued = []
    monkeypatch.setattr(app.state.pipeline, 'enqueue', enqueued.append)
    with TestClient(app) as alice:
        first = register(alice, 'resume-alice@example.test', 'Alice')
        with another_client(app, alice) as bob:
            second = register(bob, 'resume-bob@example.test', 'Bob')
            with another_client(app, alice) as anonymous:
                yield SimpleNamespace(app=app, store=app.state.store, wire=wire, alice=alice,
                                      bob=bob, anonymous=anonymous, alice_session=first,
                                      bob_session=second, enqueued=enqueued)
    assert wire.calls == 0, 'Resume API tests must never execute providers.'


def seed_failed(rig, *, agent_status='provider_error', job_status='failed'):
    response = rig.alice.post('/api/courses', json={'name': 'Private interrupted course'})
    assert response.status_code == 201, response.text
    course_id = response.json()['id']
    request = GenerateRequest(course_id=course_id, topic='Compare known scheduling policies',
        material='quiz', count=2, question_type='short_answer',
        difficulty_distribution={'easy': 1, 'medium': 1, 'hard': 0},
        request_key='resume-api-' + uid())
    job, created = rig.store.job(request.request_key, 'generate', request.model_dump(),
                                owner_id=rig.alice_session['user']['id'])
    assert created
    rig.store.execute('UPDATE jobs SET status=?,error=? WHERE id=?',
                      (job_status, 'Simulated upstream interruption', job['id']))
    evidence = {
        'schema_version': 'education-agent-v1',
        'configuration': {'max_calls': 12},
        'sources': [{'id': 'private-source', 'text': 'ALICE PRIVATE source remains unchanged.'}],
        'agent': {'version': 'education-agent-v1', 'status': agent_status,
                  'phase': 'independent_solution', 'call_count': 2, 'resume_count': 0,
                  'slots': [
                      {'slot_id': 'q1', 'difficulty': 'easy', 'status': 'passed',
                       'attempts': [{'question': {'stem': 'ALICE PRIVATE accepted question'},
                                     'review': {'status': 'passed'}}]},
                      {'slot_id': 'q2', 'difficulty': 'medium', 'status': 'pending',
                       'attempts': [{'solution': {'status': 'provider_error'}}]},
                  ]},
    }
    rig.store.save_job_evidence(job['id'], evidence)
    # The progress endpoint exposes total ledger calls separately from text calls.
    for capability in ('embedding', 'text', 'text'):
        rig.store.reserve_call(job['id'], capability, 'fixture-model', 100)
    return job['id'], deepcopy(evidence)


def checkpoint(rig, job_id):
    row = rig.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (job_id,))
    return json.loads(row['evidence'])


def assert_unchanged(rig, job_id, before, job_status='failed'):
    assert checkpoint(rig, job_id) == before
    assert rig.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'] == job_status
    assert rig.enqueued == []


@pytest.mark.parametrize('client_name,expected', [('bob', 404), ('anonymous', 401)])
@pytest.mark.parametrize('method,suffix', [('GET', ''), ('GET', '/evidence'), ('POST', '/resume')])
def test_progress_evidence_and_resume_enforce_job_ownership(rig, client_name, expected, method, suffix):
    job_id, before = seed_failed(rig)
    client = getattr(rig, client_name)
    response = client.request(method, '/api/jobs/' + job_id + suffix)
    assert response.status_code == expected, response.text
    assert 'ALICE PRIVATE' not in response.text
    assert 'private-source' not in response.text
    assert_unchanged(rig, job_id, before)


def test_owner_resume_requires_csrf_even_with_valid_session(rig):
    job_id, before = seed_failed(rig)
    with another_client(rig.app, rig.alice) as no_csrf:
        no_csrf.cookies.update(rig.alice.cookies)
        assert 'X-CSRF-Token' not in no_csrf.headers
        assert no_csrf.get('/api/jobs/' + job_id).status_code == 200
        response = no_csrf.post('/api/jobs/' + job_id + '/resume')
    assert response.status_code == 403
    assert response.json()['code'] == 'csrf_invalid'
    assert_unchanged(rig, job_id, before)


@pytest.mark.parametrize('agent_status', ['provider_error', 'running'])
def test_owner_resumes_same_job_once_and_preserves_passed_work(rig, agent_status):
    job_id, before = seed_failed(rig, agent_status=agent_status)
    progress = rig.alice.get('/api/jobs/' + job_id).json()['progress']
    assert progress == {'workflow': 'education-agent-v1', 'completed': 1, 'total': 2,
                        'phase': 'independent_solution', 'text_call_attempts': 2,
                        'resumable': True, 'calls': 3}
    jobs_before = rig.store.one('SELECT count(*) AS n FROM jobs')['n']
    response = rig.alice.post('/api/jobs/' + job_id + '/resume')
    assert response.status_code == 200, response.text
    assert response.json() == {'job_id': job_id, 'resumed': True}
    after = checkpoint(rig, job_id)
    assert after['agent']['slots'] == before['agent']['slots']
    assert after['sources'] == before['sources']
    assert after['configuration'] == before['configuration']
    assert after['agent']['call_count'] == 2
    assert after['agent']['resume_count'] == 1
    assert after['agent']['status'] == 'running'
    assert len(after['agent']['resume_events']) == 1
    job = rig.alice.get('/api/jobs/' + job_id).json()
    assert job['id'] == job_id and job['status'] == 'queued' and job['error'] is None
    assert job['progress']['completed'] == 1 and job['progress']['calls'] == 3
    assert rig.enqueued == [job_id]

    duplicate = rig.alice.post('/api/jobs/' + job_id + '/resume')
    assert duplicate.status_code == 409, duplicate.text
    assert checkpoint(rig, job_id) == after
    assert rig.enqueued == [job_id]
    assert rig.store.one('SELECT count(*) AS n FROM jobs')['n'] == jobs_before
    assert rig.store.one('SELECT count(*) AS n FROM calls WHERE job_id=?', (job_id,))['n'] == 3


def test_quality_failed_checkpoint_cannot_get_another_attempt_budget(rig):
    job_id, before = seed_failed(rig, agent_status='quality_failed')
    assert rig.alice.get('/api/jobs/' + job_id).json()['progress']['resumable'] is False
    response = rig.alice.post('/api/jobs/' + job_id + '/resume')
    assert response.status_code == 409, response.text
    assert_unchanged(rig, job_id, before)


def test_owner_can_extend_exhausted_candidate_loop_without_rewriting_history(rig):
    job_id, before = seed_failed(rig, agent_status='quality_failed')
    slot = before['agent']['slots'][1]
    slot.update(status='failed', attempts=[{'number': i, 'status': 'rejected',
                'feedback': {'issues': ['author_contract_invalid'], 'validation': {'rule': 'schema_validation'}}}
                for i in (1, 2)])
    rig.store.save_job_evidence(job_id, before)
    status = rig.alice.get('/api/jobs/' + job_id).json()['progress']
    assert status['resumable'] is True and status['resume_kind'] == 'repair'
    assert status['completed'] == 1
    assert rig.bob.post('/api/jobs/' + job_id + '/resume').status_code == 404
    response = rig.alice.post('/api/jobs/' + job_id + '/resume')
    assert response.status_code == 200, response.text
    after = checkpoint(rig, job_id)
    assert after['configuration'] == before['configuration']
    assert after['sources'] == before['sources']
    assert after['agent']['slots'][0] == before['agent']['slots'][0]
    failed = after['agent']['slots'][1]
    assert failed['attempts'] == slot['attempts']
    assert failed['status'] == 'pending'
    assert failed['repair_extensions'][0]['start_attempt'] == 3
    assert failed['repair_extensions'][0]['rounds'] == 3
    assert after['agent']['call_count'] == before['agent']['call_count']
    assert rig.alice.post('/api/jobs/' + job_id + '/resume').status_code == 409
    assert rig.enqueued == [job_id]


def test_resume_is_blocked_when_workflow_changes_to_legacy(rig):
    job_id, before = seed_failed(rig)
    rig.app.state.settings.generation_workflow = 'legacy_v3'
    response = rig.alice.post('/api/jobs/' + job_id + '/resume')
    assert response.status_code == 409, response.text
    assert_unchanged(rig, job_id, before)


@pytest.mark.parametrize('status', ['queued', 'running', 'succeeded'])
def test_nonfailed_job_cannot_be_resumed_even_with_provider_error_checkpoint(rig, status):
    job_id, before = seed_failed(rig, job_status=status)
    response = rig.alice.post('/api/jobs/' + job_id + '/resume')
    assert response.status_code == 409, response.text
    assert_unchanged(rig, job_id, before, job_status=status)


def test_simultaneous_owner_http_resumes_only_enqueue_once(rig):
    job_id, before = seed_failed(rig)
    barrier = Barrier(2)
    with another_client(rig.app, rig.alice) as first, another_client(rig.app, rig.alice) as second:
        for client in (first, second):
            client.cookies.update(rig.alice.cookies)
            client.headers['X-CSRF-Token'] = rig.alice_session['csrf_token']

        def send(client):
            barrier.wait(timeout=5)
            return client.post('/api/jobs/' + job_id + '/resume')

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(send, (first, second)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert rig.enqueued == [job_id]
    after = checkpoint(rig, job_id)
    assert after['agent']['resume_count'] == 1
    assert after['agent']['slots'] == before['agent']['slots']
    assert len(after['agent']['resume_events']) == 1
    assert rig.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'] == 'queued'


def test_resume_checkpoint_transition_is_atomic_across_database_callers(rig):
    # Real parallel connections exercise SQLite's BEGIN IMMEDIATE boundary,
    # beyond the event loop's serialization of the HTTP handlers above.
    job_id, before = seed_failed(rig)
    barrier = Barrier(2)

    def authorize(_):
        barrier.wait(timeout=5)
        try:
            authorize_resume(rig.store, job_id)
            return 'queued'
        except ValueError:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(authorize, (0, 1)))
    assert sorted(outcomes) == ['conflict', 'queued']
    after = checkpoint(rig, job_id)
    assert after['agent']['status'] == 'running'
    assert after['agent']['resume_count'] == 1
    assert len(after['agent']['resume_events']) == 1
    assert after['agent']['slots'] == before['agent']['slots']
    assert rig.store.one('SELECT status,error FROM jobs WHERE id=?', (job_id,)) == {'status': 'queued', 'error': None}
    assert rig.enqueued == []  # authorize_resume only changes state; the HTTP route enqueues.
