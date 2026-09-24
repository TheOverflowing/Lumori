"""Preparation HTTP/state integration with real accounts and offline transports."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Endpoint, Settings
from app.generation_preparation import TTL_SECONDS, execution_request, fingerprint
from app.generation_agent import authorize_resume
from app.main import create_app
from app.models import GenerateRequest, IntentGenerateRequest
from app.pipeline import Pipeline
from app.providers import ApiProviders
from app.store import Conflict, dumps, now, uid
from test_account_isolation import another_client, register
from test_workflow import Wire, done
from test_generation_agent import SOURCE, agent_case  # noqa: F401
from test_harness_quality_gates import offline_harness  # noqa: F401
from test_supervisor_agent import planner_wire  # noqa: F401


class PreparationWire(Wire):
    """Dispatch through the real ApiProviders adapter, never a live endpoint."""
    def __init__(self):
        self.calls = 0
        self.preparation_calls = []
        self.preparation_mode = 'clarify'
        self.http_status = 503
        self.options = []

    def __call__(self, request):
        body = json.loads(request.content)
        if request.url.path.endswith('/chat/completions'):
            data = json.loads(body['messages'][1]['content'])
            if set(data) == {'topic', 'material', 'learner_profile', 'language'}:
                self.calls += 1
                self.preparation_calls.append(body)
                assert request.headers['authorization'] == 'Bearer test-key-not-real'
                assert 'JSON' in body['messages'][0]['content']
                if self.preparation_mode == 'http_error':
                    return httpx.Response(self.http_status, text='PRIVATE_UPSTREAM_BODY test-key-not-real')
                ready = self.preparation_mode == 'ready'
                decision = dict(status='ready' if ready else 'clarification_required',
                    question='' if ready else ('你想讨论哪一个具体操作？' if data['language']=='zh' else 'Which specific operation do you mean?'),
                    options=[] if ready else list(self.options), reason='A bounded fixture decision.', anchor_quote='' if ready else data['topic'][:60],
                    missing_information='' if ready else 'The intended operation.')
                if self.preparation_mode == 'invalid':
                    decision['rewritten_query'] = 'PRIVATE_MODEL_OUTPUT'
                return httpx.Response(200, json={'model':'fixture-model',
                    'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15,'cost':0.001},
                    'choices':[{'finish_reason':'stop','message':{'content':json.dumps(decision)}}]})
        return super().__call__(request)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, workers=1, generation_workflow='legacy_v3')
    for capability, path in [('text','/chat/completions'),('embedding','/embeddings')]:
        setattr(settings, capability, Endpoint('https://test.invalid/v1','test-key-not-real','fixture-model',path))
    wire = PreparationWire()
    app = create_app(settings, lambda s, db: ApiProviders(s, db, httpx.AsyncClient(transport=httpx.MockTransport(wire))))
    enqueued = []
    original_enqueue = app.state.pipeline.enqueue
    monkeypatch.setattr(app.state.pipeline, 'enqueue', enqueued.append)
    with TestClient(app) as alice:
        first = register(alice, 'prepare-alice@example.test', 'Alice')
        with another_client(app, alice) as bob:
            second = register(bob, 'prepare-bob@example.test', 'Bob')
            with another_client(app, alice) as anonymous:
                course = alice.post('/api/courses', json={'name':'Alice private course'}).json()['id']
                bob_course = bob.post('/api/courses', json={'name':'Bob private course'}).json()['id']
                yield SimpleNamespace(app=app, store=app.state.store, wire=wire, alice=alice, bob=bob,
                    anonymous=anonymous, alice_session=first, bob_session=second, course=course,
                    bob_course=bob_course, enqueued=enqueued, original_enqueue=original_enqueue)


def payload(rig, **updates):
    value = dict(course_id=rig.course, topic='Explain this operation and its cost.', material='quiz',
        difficulty='medium', learner_profile='An undergraduate student.', question_type='mcq',
        count=1, language='en', document_ids=[], request_key='preparation-' + uid())
    value.update(updates)
    return value


@pytest.mark.parametrize('material', ['quiz', 'assignment'])
def test_subagent_choice_fails_early_when_agent_workflow_is_disabled(rig, material):
    request = payload(rig, material=material, use_subagents=True)
    response = rig.alice.post('/api/generations', json=request)
    assert response.status_code == 400
    assert '逐题子代理' in response.json()['detail']
    assert rig.enqueued == []


def test_explicit_subagent_opt_out_is_preserved_for_restored_request(rig):
    response = rig.alice.post('/api/generations', json=payload(rig, use_subagents=False))
    assert response.status_code == 202, response.text
    saved = rig.alice.get('/api/jobs/' + response.json()['job_id'] + '/generation-request')
    assert saved.status_code == 200, saved.text
    assert saved.json()['request']['use_subagents'] is False


def prepare(rig, data=None, client=None):
    data = data or payload(rig)
    response = (client or rig.alice).post('/api/generations/prepare', json=data)
    assert response.status_code == 200, response.text
    return data, response.json()


def final_payload(data, decision, **updates):
    value = dict(data, preparation_id=decision['preparation_id'])
    if decision['status'] == 'clarification_required':
        value.update(clarification_action='answer', clarification_answer='Insertion into a dynamic array.')
    value.update(updates)
    return value


def expire(rig, preparation_id):
    saved = rig.store.one('SELECT result FROM jobs WHERE id=?', (preparation_id,))
    result = json.loads(saved['result'])
    result['expires_at'] = 1
    rig.store.execute('UPDATE jobs SET result=? WHERE id=?', (dumps(result), preparation_id))


def document(rig, course_id):
    did = uid()
    rig.store.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
        (did, course_id, 'private-note.md', uid(), 'ready', 1, now()))
    return did


def test_prepare_is_once_account_metered_and_hidden_from_job_list(rig):
    data, decision = prepare(rig)
    retry = rig.alice.post('/api/generations/prepare', json=data)
    assert retry.status_code == 200 and retry.json() == decision
    assert len(rig.wire.preparation_calls) == rig.wire.calls == 1
    assert rig.enqueued == []
    assert set(decision) == {'preparation_id','status','question','options','expires_at'}
    job = rig.store.one('SELECT * FROM jobs WHERE id=?', (decision['preparation_id'],))
    assert job['kind'] == 'prepare' and job['status'] == 'succeeded'
    assert json.loads(job['payload'])['topic'] == data['topic']
    assert decision['expires_at'] - int(datetime.fromisoformat(job['created_at']).timestamp()) == TTL_SECONDS == 900
    owner = rig.store.one('SELECT user_id FROM job_owners WHERE job_id=?', (job['id'],))
    assert owner['user_id'] == rig.alice_session['user']['id']
    assert rig.alice.get('/api/status').json()['calls_today'] == 1
    assert rig.bob.get('/api/status').json()['calls_today'] == 0
    assert rig.alice.get('/api/jobs').json() == []
    projected = json.loads(rig.wire.preparation_calls[0]['messages'][1]['content'])
    assert set(projected) == {'topic','material','learner_profile','language'}
    assert rig.course not in json.dumps(projected)


@pytest.mark.parametrize('path', ['/api/generations/prepare','/api/generations'])
def test_prepare_and_final_require_auth_csrf_and_same_origin(rig, path):
    data = payload(rig)
    assert rig.anonymous.post(path, json=data).status_code == 401
    with another_client(rig.app, rig.alice) as no_csrf:
        no_csrf.cookies.update(rig.alice.cookies)
        response = no_csrf.post(path, json=data)
        assert response.status_code == 403 and response.json()['code'] == 'csrf_invalid'
    response = rig.alice.post(path, json=data, headers={'origin':'https://untrusted.invalid'})
    assert response.status_code == 403
    assert rig.wire.calls == 0 and rig.enqueued == []


def test_prepare_rechecks_course_and_document_ownership_before_model(rig):
    foreign_doc = document(rig, rig.bob_course)
    another_course = rig.alice.post('/api/courses', json={'name':'Other Alice course'}).json()['id']
    other_doc = document(rig, another_course)
    for data, status in [(payload(rig, course_id=rig.bob_course),404),
                         (payload(rig, document_ids=[foreign_doc]),404),
                         (payload(rig, document_ids=[other_doc]),400)]:
        response = rig.alice.post('/api/generations/prepare', json=data)
        assert response.status_code == status, response.text
    assert rig.wire.calls == 0


def test_foreign_preparation_and_evidence_never_leak(rig):
    data, decision = prepare(rig)
    foreign = final_payload(data, decision, course_id=rig.bob_course, request_key='foreign-' + uid())
    response = rig.bob.post('/api/generations', json=foreign)
    assert response.status_code == 404 and response.json()['code'] == 'preparation_missing'
    for suffix in ('','/evidence'):
        assert rig.bob.get('/api/jobs/' + decision['preparation_id'] + suffix).status_code == 404
    assert decision['question'] not in response.text
    assert rig.enqueued == [] and rig.wire.calls == 1


def test_every_generation_parameter_is_bound_to_snapshot(rig):
    rig.wire.preparation_mode = 'ready'
    data, decision = prepare(rig)
    other_course = rig.alice.post('/api/courses', json={'name':'Other course'}).json()['id']
    did = document(rig, rig.course)
    changes = [dict(topic='A different learning goal'), dict(course_id=other_course),
        dict(material='lesson'), dict(count=2), dict(difficulty='hard'),
        dict(difficulty_distribution={'easy':1,'medium':0,'hard':0}),
        dict(question_type='short_answer'), dict(language='zh'), dict(query_fusion=True),
        dict(learner_profile='A beginner without prerequisites.'), dict(document_ids=[did])]
    for change in changes:
        response = rig.alice.post('/api/generations', json=final_payload(data, decision, **change))
        assert response.status_code == 409, (change, response.text)
        assert response.json()['code'] == 'preparation_changed'
    assert rig.enqueued == [] and rig.wire.calls == 1
    assert not rig.store.all('SELECT * FROM preparation_uses')


def test_fusion_choice_survives_preparation_answer_and_exact_retry(rig):
    data, decision = prepare(rig, payload(rig, query_fusion=True))
    final = final_payload(data, decision)
    response = rig.alice.post('/api/generations', json=final)
    assert response.status_code == 202, response.text
    job_id = response.json()['job_id']
    stored = json.loads(rig.store.one('SELECT payload FROM jobs WHERE id=?', (job_id,))['payload'])
    assert stored['query_fusion'] is True
    executable = execution_request(rig.store, GenerateRequest(**stored), job_id)
    assert executable.query_fusion is True
    assert executable.original_topic == data['topic']
    assert final['clarification_answer'] in executable.topic
    retry = rig.alice.post('/api/generations', json=final)
    assert retry.status_code == 202 and retry.json()['job_id'] == job_id


def test_document_order_is_semantically_canonical_in_fingerprint(rig):
    a, b = document(rig, rig.course), document(rig, rig.course)
    rig.wire.preparation_mode = 'ready'
    data, decision = prepare(rig, payload(rig, document_ids=[a,b]))
    changed = final_payload(data, decision, document_ids=[b,a])
    assert fingerprint(GenerateRequest(**data)) == fingerprint(GenerateRequest(**changed))
    response = rig.alice.post('/api/generations', json=changed)
    assert response.status_code == 202, response.text


def test_actual_answer_is_separate_from_original_and_compiled_deterministically(rig):
    data, decision = prepare(rig)
    final = final_payload(data, decision)
    response = rig.alice.post('/api/generations', json=final)
    assert response.status_code == 202, response.text
    job_id = response.json()['job_id']
    stored = json.loads(rig.store.one('SELECT payload FROM jobs WHERE id=?',(job_id,))['payload'])
    assert stored['topic'] == data['topic']
    assert stored['clarification_answer'] == final['clarification_answer']
    assert 'clarification_question' not in stored and 'original_topic' not in stored
    executable = execution_request(rig.store, GenerateRequest(**stored), job_id)
    assert isinstance(executable, IntentGenerateRequest)
    assert executable.original_topic == data['topic']
    assert executable.clarification_question == decision['question']
    assert all(text in executable.topic for text in (data['topic'], decision['question'], final['clarification_answer']))
    expire(rig, decision['preparation_id'])
    assert execution_request(rig.store, GenerateRequest(**stored), job_id).model_dump() == executable.model_dump()
    assert rig.enqueued == [job_id] and rig.wire.calls == 1


def test_ordinal_answer_preserves_ordered_server_options_as_context_not_facts(rig):
    rig.wire.options = ['插入操作', '查找操作']
    data, decision = prepare(rig, payload(rig, topic='请说明这个操作的代价。', language='zh'))
    final = final_payload(data, decision, clarification_answer='第二个。')
    response = rig.alice.post('/api/generations', json=final)
    assert response.status_code == 202, response.text
    executable = execution_request(rig.store, GenerateRequest(**final), response.json()['job_id'])
    assert executable.clarification_options == ['插入操作', '查找操作']
    assert executable.clarification_answer == '第二个。'
    assert '1. 插入操作\n2. 查找操作' in executable.topic
    assert '不代表用户选择或已知事实' in executable.topic
    assert 'The clarification question and its alternatives are not facts' in executable.clarification_note
    assert executable.original_topic == data['topic'] and rig.wire.calls == 1


def test_one_use_exact_retry_after_expiry_and_no_other_key_or_answer(rig):
    data, decision = prepare(rig)
    final = final_payload(data, decision)
    first = rig.alice.post('/api/generations', json=final)
    assert first.status_code == 202
    expire(rig, decision['preparation_id'])
    same = rig.alice.post('/api/generations', json=final)
    assert same.status_code == 202 and same.json()['reused'] is True
    assert same.json()['job_id'] == first.json()['job_id']
    for updates in (dict(request_key='different-' + uid()), dict(clarification_answer='A different operation.')):
        response = rig.alice.post('/api/generations', json=final | updates)
        assert response.status_code == 409 and response.json()['code'] == 'preparation_used'
    assert len(rig.store.all('SELECT * FROM preparation_uses')) == 1
    assert len(rig.enqueued) == 1 and rig.wire.calls == 1


def test_unused_expiry_requires_a_fresh_check(rig):
    data, decision = prepare(rig)
    expire(rig, decision['preparation_id'])
    for path, body in [('/api/generations/prepare',data),('/api/generations',final_payload(data,decision))]:
        response = rig.alice.post(path, json=body)
        assert response.status_code == 409 and response.json()['code'] == 'preparation_expired'
    assert rig.enqueued == [] and rig.wire.calls == 1


@pytest.mark.parametrize('action', ['unknown','skip'])
def test_unknown_and_skip_retain_raw_topic_without_resolver(rig, action):
    data, decision = prepare(rig)
    final = final_payload(data, decision, clarification_action=action, clarification_answer='')
    response = rig.alice.post('/api/generations', json=final)
    assert response.status_code == 202
    executable = execution_request(rig.store, GenerateRequest(**final), response.json()['job_id'])
    assert executable.topic == executable.original_topic == data['topic']
    assert executable.clarification_answer == '' and rig.wire.calls == 1


@pytest.mark.parametrize('mode', ['http_error','invalid'])
def test_failed_check_requires_explicit_skip_and_sanitizes_errors(rig, mode):
    rig.wire.preparation_mode = mode
    data, decision = prepare(rig)
    assert decision['status'] == 'unavailable' and decision['question'] == ''
    assert decision['failure_code'] == ('upstream_unavailable' if mode == 'http_error' else 'invalid_output')
    final = final_payload(data, decision)
    for update in ({}, {'clarification_action':'unknown'}):
        response = rig.alice.post('/api/generations', json=final | update)
        assert response.status_code == 409 and response.json()['code'] == 'preparation_incomplete'
    skipped = rig.alice.post('/api/generations', json=final | {'clarification_action':'skip'})
    assert skipped.status_code == 202
    assert rig.wire.calls == 1
    job = rig.alice.get('/api/jobs/' + decision['preparation_id'])
    assert 'PRIVATE_' not in job.text and 'test-key-not-real' not in job.text
    call = rig.store.one('SELECT * FROM calls WHERE job_id=?',(decision['preparation_id'],))
    assert call['http_status'] == (503 if mode=='http_error' else 200)


@pytest.mark.parametrize('status,code', [
    (400,'configuration'), (401,'authentication'), (402,'payment_required'),
    (403,'authentication'), (404,'configuration'), (422,'configuration'),
    (429,'rate_limited'), (500,'upstream_unavailable'),
    (502,'upstream_unavailable'), (503,'upstream_unavailable'),
    (504,'upstream_unavailable'), (418,'unknown'),
])
def test_preparation_classifies_service_failures_without_exposing_response_or_retrying(rig, status, code):
    rig.wire.preparation_mode = 'http_error'
    rig.wire.http_status = status
    data, decision = prepare(rig)
    assert decision['failure_code'] == code
    assert rig.wire.calls == 1 and rig.enqueued == []
    duplicate = rig.alice.post('/api/generations/prepare', json=data).json()
    assert duplicate == decision and rig.wire.calls == 1
    job = rig.alice.get('/api/jobs/' + decision['preparation_id'])
    assert 'PRIVATE_UPSTREAM_BODY' not in job.text and 'test-key-not-real' not in job.text
    assert '生成也可能' in job.json()['error'] or '直接生成也可能' in job.json()['error']


def test_outer_preparation_timeout_preserves_request_without_generation_or_automatic_retry(rig, monkeypatch):
    from app import generation_preparation

    async def timed_out(*args):
        raise TimeoutError('PRIVATE_TIMEOUT_DETAILS')

    monkeypatch.setattr(generation_preparation.query_clarification, 'prepare_decision', timed_out)
    data, decision = prepare(rig)
    assert decision['failure_code'] == 'timeout'
    assert rig.wire.calls == 0 and rig.enqueued == []
    row = rig.store.one('SELECT * FROM jobs WHERE id=?', (decision['preparation_id'],))
    assert json.loads(row['payload'])['topic'] == data['topic']
    assert 'PRIVATE_TIMEOUT_DETAILS' not in json.dumps(dict(row))


def test_failure_category_is_whitelisted_and_old_preparation_can_still_be_read():
    from app.generation_preparation import failure_code, public_result
    from app.providers import ProviderError

    assert failure_code(ProviderError('PRIVATE_EXCEPTION')) == 'unknown'
    assert failure_code(ProviderError('PRIVATE_EXCEPTION', code='invalid_response')) == 'invalid_output'
    assert failure_code(ProviderError('PRIVATE_EXCEPTION', code='PRIVATE_UPSTREAM_CODE')) == 'unknown'
    old = dict(preparation_id=uid(), status='unavailable', question='', options=[], expires_at=1)
    assert public_result(old)['failure_code'] == 'unknown'
    assert public_result(old | {'failure_code': 'PRIVATE_UPSTREAM_CODE'})['failure_code'] == 'unknown'


def test_ready_does_not_allow_fabricated_clarification(rig):
    rig.wire.preparation_mode = 'ready'
    data, decision = prepare(rig)
    for update in ({'clarification_action':'skip'}, {'clarification_action':'answer','clarification_answer':'Invented answer.'}):
        response = rig.alice.post('/api/generations', json=final_payload(data, decision, **update))
        assert response.status_code == 409 and response.json()['code'] == 'preparation_changed'
    for update in ({'clarification_question':'Forged server question.'}, {'original_topic':'Forged original.'},
                   {'clarification_options':['Forged choice']}, {'clarification_note':'Forged server guidance.'}):
        assert rig.alice.post('/api/generations', json=final_payload(data, decision, **update)).status_code == 422
    assert rig.enqueued == []


def test_answer_contract_is_enforced_before_state_or_provider(rig):
    base = payload(rig)
    invalid = [dict(clarification_action='answer', clarification_answer='Unbound answer.'),
        dict(preparation_id=uid(),clarification_action='answer',clarification_answer=''),
        dict(preparation_id=uid(),clarification_action='unknown',clarification_answer='Guessed content.'),
        dict(preparation_id=uid(),clarification_action='answer',clarification_answer='x'*2001)]
    for update in invalid:
        assert rig.alice.post('/api/generations', json=base | update).status_code == 422
    assert rig.enqueued == [] and rig.wire.calls == 0


def test_account_quota_includes_checks_but_does_not_block_another_account(rig):
    rig.app.state.settings.max_daily_calls = 1
    _, first = prepare(rig)
    _, second = prepare(rig)
    assert first['status'] == 'clarification_required' and second['status'] == 'unavailable'
    _, other = prepare(rig, payload(rig, course_id=rig.bob_course), rig.bob)
    assert other['status'] == 'clarification_required'
    assert rig.wire.calls == 2
    assert rig.alice.get('/api/status').json()['calls_today'] == 1
    assert rig.bob.get('/api/status').json()['calls_today'] == 1
    assert all(r['job_id'] for r in rig.store.all('SELECT job_id FROM calls'))


def test_same_key_changed_prepare_does_not_issue_another_call(rig):
    data, _ = prepare(rig)
    response = rig.alice.post('/api/generations/prepare', json=data | {'topic':'Different request.'})
    assert response.status_code == 409 and response.json()['code'] == 'preparation_changed'
    assert rig.wire.calls == 1


def test_scope_is_rechecked_after_preparation(rig):
    data, decision = prepare(rig)
    rig.store.execute('UPDATE course_owners SET user_id=? WHERE course_id=?',
                      (rig.bob_session['user']['id'],rig.course))
    response = rig.alice.post('/api/generations', json=final_payload(data, decision))
    assert response.status_code == 404
    assert rig.enqueued == [] and not rig.store.all('SELECT * FROM preparation_uses')


def test_single_use_claim_is_atomic_across_sqlite_callers(rig):
    data, decision = prepare(rig)
    barrier = Barrier(2)
    owner = rig.alice_session['user']['id']

    def claim(_):
        final = GenerateRequest(**final_payload(data, decision, request_key='race-' + uid()))
        barrier.wait(timeout=5)
        try:
            rig.store.job(final.request_key, 'generate', final.model_dump(), owner_id=owner,
                          preparation_id=decision['preparation_id'])
            return 'created'
        except Conflict:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, (0,1)))
    assert sorted(outcomes) == ['conflict','created']
    assert len(rig.store.all('SELECT * FROM preparation_uses')) == 1
    assert rig.store.one("SELECT count(*) AS n FROM jobs WHERE kind='generate'")['n'] == 1


def test_restart_marks_pending_checks_interrupted_without_enqueuing_them(rig):
    pending = []
    for status in ('queued','running'):
        req = GenerateRequest(**payload(rig))
        job, _ = rig.store.job('prepare:' + req.request_key, 'prepare', req.model_dump(),
                               owner_id=rig.alice_session['user']['id'])
        rig.store.execute('UPDATE jobs SET status=? WHERE id=?',(status,job['id']))
        pending.append((req,job['id']))
    settings = copy.copy(rig.app.state.settings)
    settings.workers = 0

    class NoCalls:
        async def close(self):
            pass

    pipeline = Pipeline(settings,rig.store,NoCalls(),enforce_account_ownership=True)

    async def restart():
        await pipeline.start()
        assert pipeline.queue.empty()
        await pipeline.stop()

    asyncio.run(restart())
    for req, jid in pending:
        assert rig.store.one('SELECT status FROM jobs WHERE id=?',(jid,))['status'] == 'failed'
        response = rig.alice.post('/api/generations/prepare',json=req.model_dump())
        assert response.status_code == 409 and response.json()['code'] == 'preparation_incomplete'
    assert rig.wire.calls == 0


def test_full_legacy_generation_uses_real_answer_without_extra_resolver(rig, monkeypatch):
    monkeypatch.setattr(rig.app.state.pipeline,'enqueue',rig.original_enqueue)
    uploaded = rig.alice.post('/api/documents',data={'course_id':rig.course},
        files={'file':('lecture.md','检索为生成提供课程参考资料。'.encode())})
    assert uploaded.status_code == 201
    did = uploaded.json()['id']
    assert done(rig.alice,rig.alice.post('/api/documents/'+did+'/index'))['status'] == 'succeeded'
    data, decision = prepare(rig,payload(rig,material='lesson',document_ids=[did]))
    final = final_payload(data,decision)
    before = rig.wire.calls
    completed = done(rig.alice,rig.alice.post('/api/generations',json=final))
    assert completed['status'] == 'succeeded',completed
    assert rig.wire.calls == before + 2  # One embedding and one lesson generation, no resolver.
    content = rig.alice.get('/api/contents/'+completed['result']['content_id']).json()
    config = content['config']
    assert config['original_topic'] == data['topic']
    assert config['clarification_answer'] == final['clarification_answer']
    assert final['clarification_answer'] in config['topic']
    original = json.loads(rig.store.one('SELECT payload FROM jobs WHERE id=?',(completed['id'],))['payload'])
    assert original['topic'] == data['topic']


def bind_agent_preparation(case):
    """Seed an accepted preparation to exercise real agent execution/resume.

    Preparation's HTTP/model path is covered above. Here only the completed
    decision is a fixture; request reconstruction, ownership and agent are real.
    """
    original = GenerateRequest(**json.loads(case.job()['payload']))
    owner = 'preparation-agent-owner'
    case.store.execute('INSERT INTO users VALUES(?,?,?,?,?)',
        (owner,'agent-preparation@example.test','test-only-no-login','Fixture owner',now()))
    case.store.execute('INSERT INTO course_owners VALUES(?,?)',(original.course_id,owner))
    case.store.execute('INSERT INTO job_owners VALUES(?,?)',(case.job_id,owner))
    case.store.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
        (SOURCE['document_id'],original.course_id,SOURCE['document_name'],'fixture-source-hash','ready',1,now()))
    case.pipeline.enforce_account_ownership = True
    prep, _ = case.store.job('prepare:agent-regression','prepare',original.model_dump(),owner_id=owner)
    decision = dict(preparation_id=prep['id'],status='clarification_required',
        question='Which part of this learning goal should the questions emphasize?',
        options=['The role of retrieved evidence','Recovery after an embedding model changes'],
        expires_at=int(datetime.now().timestamp())+TTL_SECONDS,
        request_fingerprint=fingerprint(original),prompt_version='offline-fixture-only')
    case.store.execute("UPDATE jobs SET status='succeeded',result=? WHERE id=?",(dumps(decision),prep['id']))
    final = GenerateRequest(**(original.model_dump() | {'preparation_id':prep['id'],
        'clarification_action':'answer','clarification_answer':'The second option; retain course isolation.'}))
    case.store.execute('UPDATE jobs SET payload=? WHERE id=?',(dumps(final.model_dump()),case.job_id))
    case.store.execute('INSERT INTO preparation_uses VALUES(?,?)',(prep['id'],case.job_id))
    return final, decision


def test_supervisor_and_author_receive_prepared_context_but_asset_title_stays_original(
        agent_case, offline_harness, planner_wire):
    case = agent_case(count=1,settings_options={'agent_runtime':'deepseek_harness','agent_orchestration':'supervisor_v1'})
    original, decision = bind_agent_preparation(case)
    expected = execution_request(case.store,original,case.job_id)
    assert case.run()['status'] == 'succeeded',case.job()
    assert case.asset()['title'] == original.topic
    assert 'Original learning goal:' not in case.asset()['title']
    assert offline_harness == ['plan','author','solve','review']
    contracts = [c for c in case.wire.contracts if c['task'] in ('agent_plan','agent_author')]
    assert [c['task'] for c in contracts] == ['agent_plan','agent_author']
    for contract in contracts:
        supplied = contract['request']
        assert supplied['topic'] == expected.topic
        assert supplied['original_topic'] == original.topic
        assert supplied['clarification_answer'] == original.clarification_answer
        assert supplied['clarification_question'] == decision['question']
        assert supplied['clarification_options'] == decision['options']
        assert 'not facts' in supplied['clarification_note']
    assert case.evidence()['configuration']['topic'] == expected.topic
    assert json.loads(case.job()['payload'])['topic'] == original.topic


def test_prepared_agent_resume_after_expiry_rebuilds_identical_execution_and_keeps_passed_work(agent_case):
    case = agent_case(count=2,wire_options={'fail_phase':('agent_solve',2)})
    original, decision = bind_agent_preparation(case)
    before = execution_request(case.store,original,case.job_id).model_dump()
    assert case.run()['status'] == 'failed',case.job()
    evidence = case.evidence()
    assert evidence['agent']['status'] == 'provider_error'
    passed = copy.deepcopy(evidence['agent']['slots'][0])
    config = copy.deepcopy(evidence['configuration'])
    saved = json.loads(case.store.one('SELECT result FROM jobs WHERE id=?',(decision['preparation_id'],))['result'])
    saved['expires_at'] = 1
    case.store.execute('UPDATE jobs SET result=? WHERE id=?',(dumps(saved),decision['preparation_id']))
    assert execution_request(case.store,original,case.job_id).model_dump() == before
    authorize_resume(case.store,case.job_id)
    assert case.run()['status'] == 'succeeded',case.job()
    after = case.evidence()
    assert after['configuration'] == config
    assert after['agent']['slots'][0] == passed
    assert after['agent']['resume_count'] == 1
    assert case.wire.stage_counts[('agent_solve',2)] == 2
    assert case.wire.stage_counts[('agent_author',1)] == 1
    assert len(case.retrieval_calls) == 1
    assert case.asset()['title'] == original.topic
    assert all(c['request']['topic'] == before['topic'] for c in case.wire.contracts if c['task']=='agent_author')


def legacy_checkpoint_without_preparation_fields(case):
    """Represent the persisted schema from before clarification was introduced."""
    fields = ('preparation_id','clarification_action','clarification_answer')
    stored_request = json.loads(case.job()['payload'])
    evidence = case.evidence()
    for values in (stored_request,evidence['request'],evidence['configuration']):
        for name in fields:
            values.pop(name,None)
    case.store.execute('UPDATE jobs SET payload=? WHERE id=?',(dumps(stored_request),case.job_id))
    case.store.save_job_evidence(case.job_id,evidence)
    return stored_request, evidence


def test_pre_feature_agent_checkpoint_resumes_without_new_default_key_mismatch(agent_case):
    case = agent_case(count=2,wire_options={'fail_phase':('agent_solve',2)})
    assert case.run()['status'] == 'failed'
    stored_request, evidence = legacy_checkpoint_without_preparation_fields(case)
    request_again = GenerateRequest(**stored_request)
    assert not {'preparation_id','clarification_action','clarification_answer'} & request_again.model_dump().keys()
    previous_config = copy.deepcopy(evidence['configuration'])
    previous_first = copy.deepcopy(evidence['agent']['slots'][0])
    authorize_resume(case.store,case.job_id)
    assert case.run()['status'] == 'succeeded',case.job()
    assert case.evidence()['configuration'] == previous_config
    assert case.evidence()['agent']['slots'][0] == previous_first
    assert len(case.retrieval_calls) == 1
    assert case.wire.stage_counts[('agent_author',1)] == 1
    assert case.wire.stage_counts[('agent_solve',2)] == 2


def test_legacy_compatibility_does_not_ignore_actual_changed_request_configuration(agent_case):
    case = agent_case(count=2,wire_options={'fail_phase':('agent_solve',2)})
    assert case.run()['status'] == 'failed'
    stored_request, _ = legacy_checkpoint_without_preparation_fields(case)
    stored_request['topic'] = 'A genuinely different educational task.'
    case.store.execute('UPDATE jobs SET payload=? WHERE id=?',(dumps(stored_request),case.job_id))
    calls_before = case.text_call_count()
    authorize_resume(case.store,case.job_id)
    assert case.run()['status'] == 'failed'
    assert '任务配置已改变' in case.job()['error']
    assert case.text_call_count() == calls_before
    assert not case.contents()
