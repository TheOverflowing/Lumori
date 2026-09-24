"""Task controls and scoped revision APIs over real isolated account sessions.

The queue is recorded, not consumed. These tests never call a model or mutate
the developer's workspace data; generation quality is tested separately.
"""
import json

import pytest

from app.config import Endpoint
from app.difficulty import build_difficulty_plan
from app.models import GenerateRequest, LearningAsset
from app.store import dumps, now, uid
from test_account_isolation import another_client
from test_agent_resume_api import rig, seed_failed


def seed_content(rig):
    course = rig.alice.post('/api/courses', json={'name': 'Revision private course'}).json()['id']
    request = GenerateRequest(course_id=course, topic='Apply the course retrieval rule',
        material='quiz', question_type='short_answer', count=2, difficulty='medium',
        include_explanations=False, request_key='base-generation-' + uid())
    original, _ = rig.store.job(request.request_key, 'generate', request.model_dump(),
                               owner_id=rig.alice_session['user']['id'])
    rig.store.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (original['id'],))
    document = uid()
    rig.store.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
        (document, course, 'private-course.md', uid(), 'ready', 1, now()))
    source = {'id': 'source-' + uid(), 'document_id': document,
              'document_name': 'private-course.md', 'page': 1,
              'text': 'ALICE PRIVATE evidence supports the retrieval rule.'}
    asset = LearningAsset.model_validate({'title': 'Private questions', 'evidence_sufficient': True,
        'questions': [
            {'slot_id': f'q{index}', 'difficulty': 'medium', 'kind': 'short_answer',
             'stem': f'ALICE PRIVATE question {index}: apply the retrieval rule.',
             'answer': 'Use the supplied evidence.', 'explanation': 'Evidence supports a grounded answer.',
             'difficulty_reason': 'Apply a known rule to a described case.',
             'difficulty_design': {'cognitive_process': 'apply', 'concepts': ['retrieval'],
                                   'expected_steps': ['Identify evidence.', 'Apply the rule.']},
             'citation_ids': [source['id']]}
            for index in (1, 2)]}).model_dump()
    config = request.model_dump() | {'difficulty_plan': build_difficulty_plan(request)}
    content = uid()
    rig.store.execute('INSERT INTO contents VALUES(?,?,?,?,?,?,?,?,?)',
        (content, course, original['id'], 1, 'approved', dumps(asset), dumps([source]), dumps(config), now()))
    rig.store.execute('INSERT INTO revisions VALUES(?,?,?,?)', (content, 1, dumps(asset), now()))
    return content, course, asset, source, config


def revision_request(**changes):
    return {'version': 1, 'instruction': 'Make the wording more concrete.', 'mode': 'rewrite',
            'request_key': 'question-revision-' + uid(), **changes}


@pytest.mark.parametrize('client_name,expected', [('bob', 404), ('anonymous', 401)])
@pytest.mark.parametrize('method,suffix', [('POST', '/cancel'), ('GET', '/partial-content')])
def test_task_controls_and_partial_view_respect_account_boundary(rig, client_name, expected, method, suffix):
    job_id, before = seed_failed(rig)
    response = getattr(rig, client_name).request(method, '/api/jobs/' + job_id + suffix)
    assert response.status_code == expected, response.text
    assert 'ALICE PRIVATE' not in response.text
    assert not rig.store.one('SELECT 1 FROM job_controls WHERE job_id=?', (job_id,))
    assert json.loads(rig.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (job_id,))['evidence']) == before


def test_queued_cancel_is_idempotent_and_does_not_enqueue_or_call_provider(rig):
    job_id, _ = seed_failed(rig, job_status='queued')
    assert rig.alice.get('/api/jobs/' + job_id).json()['cancellable'] is True
    response = rig.alice.post('/api/jobs/' + job_id + '/cancel', json={})
    assert response.status_code == 200, response.text
    assert response.json() == {'job_id': job_id, 'status': 'cancelled',
                               'cancel_requested': True, 'cancellable': False}
    assert rig.alice.post('/api/jobs/' + job_id + '/cancel').json() == response.json()
    job = rig.alice.get('/api/jobs/' + job_id).json()
    assert job['status'] == 'cancelled' and job['cancellable'] is False
    assert job['cancel_requested'] is True
    assert rig.enqueued == []


def test_running_cancel_requests_safe_boundary_and_resume_clears_request(rig):
    job_id, before = seed_failed(rig, job_status='running')
    response = rig.alice.post('/api/jobs/' + job_id + '/cancel')
    assert response.status_code == 200
    assert response.json()['status'] == 'running'
    assert response.json()['cancel_requested'] is True
    # The worker owns the final safe-boundary transition, not the HTTP handler.
    assert rig.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'] == 'running'
    assert json.loads(rig.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (job_id,))['evidence']) == before
    rig.app.state.pipeline._finish_cancelled(job_id)
    assert rig.alice.get('/api/jobs/' + job_id).json()['progress']['resumable'] is True
    assert rig.alice.post('/api/jobs/' + job_id + '/resume').status_code == 200
    current = rig.alice.get('/api/jobs/' + job_id).json()
    assert current['status'] == 'queued' and current['cancel_requested'] is False
    assert current['progress']['completed'] == 1
    assert rig.enqueued == [job_id]


def test_cancel_requires_csrf_and_rejects_completed_jobs(rig):
    job_id, _ = seed_failed(rig, job_status='queued')
    with another_client(rig.app, rig.alice) as client:
        client.cookies.update(rig.alice.cookies)
        assert client.post('/api/jobs/' + job_id + '/cancel').status_code == 403
    rig.store.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (job_id,))
    assert rig.alice.post('/api/jobs/' + job_id + '/cancel').status_code == 409
    assert rig.alice.get('/api/jobs/' + job_id).json()['cancellable'] is False


def test_synchronous_preparation_does_not_advertise_unsupported_cancellation(rig):
    job_id, _ = seed_failed(rig, job_status='queued')
    rig.store.execute("UPDATE jobs SET kind='prepare' WHERE id=?", (job_id,))
    assert rig.alice.get('/api/jobs/' + job_id).json()['cancellable'] is False
    response = rig.alice.post('/api/jobs/' + job_id + '/cancel')
    assert response.status_code == 400, response.text
    assert rig.store.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'] == 'queued'
    assert not rig.store.one('SELECT 1 FROM job_controls WHERE job_id=?', (job_id,))


def test_partial_content_contains_only_passed_questions_and_never_publishes(rig):
    content_id, _, asset, source, config = seed_content(rig)
    job_id, evidence = seed_failed(rig)
    evidence['sources'] = [source]
    evidence['configuration'] = config
    evidence['agent']['slots'][0]['accepted_asset'] = {**asset, 'questions': [asset['questions'][0]]}
    evidence['agent']['slots'][1]['accepted_asset'] = {**asset, 'questions': [asset['questions'][1]]}
    evidence['agent']['slots'][1]['attempts'][0]['private_candidate'] = 'REJECTED PRIVATE CANDIDATE'
    rig.store.save_job_evidence(job_id, evidence)
    before = rig.store.all('SELECT * FROM contents')
    assert rig.alice.get('/api/jobs/' + job_id).json()['partial_available'] is True
    response = rig.alice.get('/api/jobs/' + job_id + '/partial-content')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['partial'] is True and data['completed'] == 1 and data['total'] == 2
    assert [q['stem'] for q in data['asset']['questions']] == [asset['questions'][0]['stem']]
    assert 'REJECTED PRIVATE CANDIDATE' not in response.text
    assert asset['questions'][1]['stem'] not in response.text
    assert rig.store.all('SELECT * FROM contents') == before
    assert rig.alice.get('/api/contents/' + content_id).json()['status'] == 'approved'


def test_partial_without_an_accepted_asset_is_not_advertised(rig):
    job_id, _ = seed_failed(rig)
    assert rig.alice.get('/api/jobs/' + job_id).json()['partial_available'] is False
    assert rig.alice.get('/api/jobs/' + job_id + '/partial-content').status_code == 404


@pytest.mark.parametrize('client_name,expected', [('bob', 404), ('anonymous', 401)])
def test_question_revision_preserves_account_isolation(rig, client_name, expected):
    content, _, _, _, _ = seed_content(rig)
    before = rig.store.all('SELECT * FROM jobs')
    response = getattr(rig, client_name).post('/api/contents/' + content + '/questions/q1/revise',
                                            json=revision_request())
    assert response.status_code == expected, response.text
    assert 'ALICE PRIVATE' not in response.text
    assert rig.store.all('SELECT * FROM jobs') == before


def test_revision_is_frozen_idempotent_course_scoped_and_needs_no_embedding_config(rig):
    content, course, asset, source, _ = seed_content(rig)
    rig.app.state.settings.embedding = Endpoint()
    payload = revision_request()
    path = '/api/contents/' + content + '/questions/q2/revise'
    first = rig.alice.post(path, json=payload)
    assert first.status_code == 202, first.text
    duplicate = rig.alice.post(path, json=payload)
    assert duplicate.status_code == 202, duplicate.text
    assert first.json()['job_id'] == duplicate.json()['job_id']
    assert duplicate.json()['reused'] is True
    job_id = first.json()['job_id']
    assert rig.enqueued == [job_id]
    frozen = json.loads(rig.store.one('SELECT payload FROM jobs WHERE id=?', (job_id,))['payload'])
    assert frozen['course_id'] == course and frozen['content_id'] == content
    assert source['text'] in dumps(frozen) and asset['questions'][1]['stem'] in dumps(frozen)
    job = rig.alice.get('/api/jobs/' + job_id).json()
    assert job['kind'] == 'revise_question' and job['course_id'] == course and job['content_id'] == content
    other = rig.alice.post('/api/courses', json={'name': 'Separate course'}).json()['id']
    assert job_id in [item['id'] for item in rig.alice.get('/api/jobs', params={'course_id': course}).json()]
    assert job_id not in [item['id'] for item in rig.alice.get('/api/jobs', params={'course_id': other}).json()]
    assert rig.alice.get('/api/jobs/' + job_id + '/partial-content').status_code == 400
    current = rig.alice.get('/api/contents/' + content).json()
    assert current['asset'] == asset and current['version'] == 1 and current['status'] == 'approved'


@pytest.mark.parametrize('choice', [{}, {'difficulty': 'medium'}])
def test_completed_revision_replay_recovers_lost_response_without_new_work(rig, choice):
    content, _, _, _, _ = seed_content(rig)
    payload = revision_request(**choice)
    path = '/api/contents/' + content + '/questions/q2/revise'
    first = rig.alice.post(path, json=payload)
    assert first.status_code == 202, first.text
    job_id = first.json()['job_id']
    # Model publication is covered by the revision worker tests. Here the API
    # receives the same persisted state after a response was lost in transit.
    rig.store.execute('UPDATE contents SET version=2 WHERE id=?', (content,))
    rig.store.execute("UPDATE jobs SET status='succeeded',result=? WHERE id=?",
        (dumps({'content_id': content, 'version': 2}), job_id))
    before_jobs = rig.store.all('SELECT * FROM jobs')
    before_content = rig.store.one('SELECT * FROM contents WHERE id=?', (content,))
    # Retrieval of an already submitted job must not require a live provider.
    rig.app.state.settings.text = Endpoint()
    replay = rig.alice.post(path, json=payload)
    assert replay.status_code == 202, replay.text
    assert replay.json() == {'job_id': job_id, 'status': 'succeeded', 'reused': True}
    assert rig.store.all('SELECT * FROM jobs') == before_jobs
    assert rig.store.one('SELECT * FROM contents WHERE id=?', (content,)) == before_content
    assert rig.enqueued == [job_id]
    assert rig.bob.post(path, json=payload).status_code == 404


@pytest.mark.parametrize('changes,target_slot', [
    ({'instruction': 'Use a completely different situation.'}, 'q1'),
    ({'version': 2}, 'q1'), ({'mode': 'explanation'}, 'q1'),
    ({'difficulty': 'medium'}, 'q1'), ({}, 'q2'),
])
def test_completed_revision_key_cannot_replay_changed_inputs(rig, changes, target_slot):
    content, _, _, _, _ = seed_content(rig)
    payload = revision_request()
    path = '/api/contents/' + content + '/questions/q1/revise'
    first = rig.alice.post(path, json=payload)
    assert first.status_code == 202, first.text
    job_id = first.json()['job_id']
    rig.store.execute('UPDATE contents SET version=2 WHERE id=?', (content,))
    rig.store.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (job_id,))
    before = rig.store.all('SELECT * FROM jobs')
    replay = rig.alice.post(path.replace('/q1/', '/' + target_slot + '/'), json=payload | changes)
    assert replay.status_code == 409, replay.text
    assert rig.store.all('SELECT * FROM jobs') == before
    assert rig.enqueued == [job_id]


def test_revision_replay_rejects_other_content_and_job_kind(rig):
    content, course, _, _, _ = seed_content(rig)
    other_content, _, _, _, _ = seed_content(rig)
    payload = revision_request()
    path = '/api/contents/' + content + '/questions/q1/revise'
    first = rig.alice.post(path, json=payload)
    assert first.status_code == 202, first.text
    assert rig.alice.post(path.replace(content, other_content), json=payload).status_code == 409
    other_key = 'existing-generation-' + uid()
    rig.store.job(other_key, 'generate', {'course_id': course}, owner_id=rig.alice_session['user']['id'])
    assert rig.alice.post(path, json=payload | {'request_key': other_key}).status_code == 409
    assert rig.enqueued == [first.json()['job_id']]


@pytest.mark.parametrize('changes,expected', [({'version': 2}, 409), ({'instruction': ''}, 422),
    ({'mode': 'replace_everything'}, 422), ({'difficulty': 'impossible'}, 422),
    ({'sources': [{'text': 'Injected source'}]}, 422)])
def test_revision_rejects_stale_or_untrusted_input(rig, changes, expected):
    content, _, _, _, _ = seed_content(rig)
    response = rig.alice.post('/api/contents/' + content + '/questions/q1/revise',
                             json=revision_request(**changes))
    assert response.status_code == expected, response.text
    assert rig.enqueued == []


def test_revision_requires_csrf_valid_slot_and_text_configuration(rig):
    content, _, _, _, _ = seed_content(rig)
    path = '/api/contents/' + content + '/questions/q1/revise'
    with another_client(rig.app, rig.alice) as client:
        client.cookies.update(rig.alice.cookies)
        assert client.post(path, json=revision_request()).status_code == 403
    invalid = rig.alice.post(path.replace('/q1/', '/q9/'), json=revision_request())
    assert invalid.status_code == 400, invalid.text
    rig.app.state.settings.text = Endpoint()
    assert rig.alice.post(path, json=revision_request()).status_code == 503
    assert rig.enqueued == []


def test_revision_checkpoint_can_resume_within_same_owner_boundary(rig):
    job_id, _ = seed_failed(rig)
    rig.store.execute("UPDATE jobs SET kind='revise_question' WHERE id=?", (job_id,))
    # Selective revision always uses its own checkpointed agent, even when new
    # complete sets are configured to use the legacy generation workflow.
    rig.app.state.settings.generation_workflow = 'legacy_v3'
    assert rig.bob.post('/api/jobs/' + job_id + '/resume').status_code == 404
    response = rig.alice.post('/api/jobs/' + job_id + '/resume')
    assert response.status_code == 200, response.text
    assert response.json() == {'job_id': job_id, 'resumed': True}
    assert rig.enqueued == [job_id]
    assert rig.alice.get('/api/jobs/' + job_id).json()['kind'] == 'revise_question'


def test_review_preserves_revised_slot_order_without_certifying_whole_set(rig):
    content, _, asset, _, config = seed_content(rig)
    first = asset['questions'][0]
    first['difficulty'] = 'hard'
    first['difficulty_design'].update(cognitive_process='evaluate',
        expected_steps=['Identify the constraints.', 'Compare evidence.', 'Justify a recommendation.'])
    config['difficulty_plan'][0]['difficulty'] = 'hard'
    config['question_revision'] = {'index': 0, 'slot_id': 'q1', 'base_version': 1,
                                   'version': 2, 'review_scope': 'selected_question_only'}
    rig.store.execute("UPDATE contents SET version=2,status='draft',asset=?,config=? WHERE id=?",
                      (dumps(asset), dumps(config), content))
    rig.store.execute('INSERT INTO revisions VALUES(?,?,?,?)', (content, 2, dumps(asset), now()))
    current = rig.alice.get('/api/contents/' + content).json()
    assert current['difficulty_assessment']['status'] == 'needs_review'
    saved = rig.alice.post('/api/contents/' + content + '/review',
                          json={'version': 2, 'action': 'save', 'asset': asset})
    assert saved.status_code == 200, saved.text
    assert saved.json()['version'] == 3
    current = rig.alice.get('/api/contents/' + content).json()
    assert [q['difficulty'] for q in current['asset']['questions']] == ['hard', 'medium']
    assert current['difficulty_assessment']['status'] == 'needs_review'
    approved = rig.alice.post('/api/contents/' + content + '/review',
                             json={'version': 3, 'action': 'approve'})
    assert approved.status_code == 200, approved.text
    # A manual edit may not silently change the untouched slot's difficulty plan.
    asset['questions'][1]['difficulty'] = 'easy'
    rejected = rig.alice.post('/api/contents/' + content + '/review',
                             json={'version': 3, 'action': 'save', 'asset': asset})
    assert rejected.status_code == 400, rejected.text
    assert rig.alice.get('/api/contents/' + content).json()['version'] == 3


def test_health_reports_worker_failure_while_liveness_stays_available(rig):
    assert rig.anonymous.get('/healthz').status_code == 200
    async def stop_workers():
        import asyncio
        for worker in rig.app.state.pipeline.workers:
            worker.cancel()
        await asyncio.gather(*rig.app.state.pipeline.workers, return_exceptions=True)
    rig.alice.portal.call(stop_workers)
    response = rig.anonymous.get('/healthz')
    assert response.status_code == 503
    assert response.json()['status'] == 'unavailable'
    assert response.json()['ready'] is False and response.json()['workers'] is False
    assert 'queued' not in response.text and 'active' not in response.text
    assert rig.anonymous.get('/livez').json() == {'status': 'ok'}
    assert 'test-key' not in response.text


def test_health_reports_database_unavailable_without_leaking_error(rig, monkeypatch):
    original = rig.store.one
    def read(sql, args=()):
        if sql == 'SELECT 1 AS ok':
            raise RuntimeError('ALICE PRIVATE database credential')
        return original(sql, args)
    monkeypatch.setattr(rig.store, 'one', read)
    response = rig.anonymous.get('/healthz')
    assert response.status_code == 503 and response.json()['database'] is False
    assert 'PRIVATE' not in response.text and 'credential' not in response.text
