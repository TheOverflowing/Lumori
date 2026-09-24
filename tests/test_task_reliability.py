"""Task-control regression tests: isolated databases and no live model calls."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.config import Endpoint, Settings
from app.pipeline import Pipeline
from app.providers import ApiProviders
from app.store import Conflict, Store, dumps, now
from app.task_control import JobCancelled
from test_figure_pipeline import rig as figure_rig, upload, await_job  # noqa: F401


@pytest.fixture
def case(tmp_path):
    store = Store(tmp_path)
    settings = Settings(data_dir=tmp_path, workers=1)
    async def close(): pass
    pipeline = Pipeline(settings, store, SimpleNamespace(close=close))
    return store, pipeline


def new_job(store, key='fixture-job', owner_id=None):
    return store.job(key, 'generate', {'course_id': 'course', 'topic': 'A valid fixture topic',
                                    'request_key': key}, owner_id=owner_id)[0]


def user(store, name):
    store.execute('INSERT INTO users VALUES(?,?,?,?,?)',
                  (name, name+'@example.test', 'unused-fixture-hash', name, now()))


def test_queued_cancellation_is_idempotent_and_persistent(case):
    store, pipeline = case
    job = new_job(store)
    assert store.job_control(job['id']) == {'cancel_requested': False, 'cancellable': True}
    value = pipeline.request_cancel(job['id'])
    assert value == {'status': 'cancelled', 'cancel_requested': True, 'cancellable': False}
    assert pipeline.request_cancel(job['id']) == value
    with pytest.raises(JobCancelled): Store(store.path.parent).check_cancelled(job['id'])
    assert store.one('SELECT error FROM jobs WHERE id=?', (job['id'],))['error'] is None


def test_running_request_retains_evidence_until_safe_boundary(case):
    store, pipeline = case
    job = new_job(store)
    evidence = {'schema_version': 'fixture', 'slots': [{'id': 'q1', 'status': 'passed'}]}
    store.save_job_evidence(job['id'], evidence)
    store.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
    assert pipeline.request_cancel(job['id'])['status'] == 'running'
    assert json.loads(store.one('SELECT evidence FROM job_evidence')['evidence']) == evidence
    with pytest.raises(JobCancelled): store.check_cancelled(job['id'])
    store.clear_cancel_request(job['id'])
    store.check_cancelled(job['id'])


def test_shutdown_does_not_turn_requested_cancellation_into_failure(case, monkeypatch):
    store, pipeline = case
    job = new_job(store)
    async def scenario():
        entered = asyncio.Event()
        async def generated(request, job_id):
            store.save_job_evidence(job_id, {'agent': {'status': 'running', 'slots': [{'status': 'passed'}]}})
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(pipeline, 'generate', generated)
        await pipeline.start()
        await asyncio.wait_for(entered.wait(), 1)
        pipeline.request_cancel(job['id'])
        await pipeline.stop()
        await asyncio.wait_for(pipeline.queue.join(), 1)
    asyncio.run(scenario())
    row = store.one('SELECT status,error FROM jobs WHERE id=?', (job['id'],))
    assert row == {'status': 'cancelled', 'error': None}
    assert json.loads(store.one('SELECT evidence FROM job_evidence')['evidence'])['agent'] == {
        'status': 'cancelled', 'slots': [{'status': 'passed'}]}


@pytest.mark.parametrize('at_scope', [False, True])
def test_harness_relay_returns_explicit_cancellation_without_upstream_calls(case, at_scope):
    from app.harness_relay import create_relay_app
    store, pipeline = case
    job = new_job(store)
    store.request_cancel(job['id'])
    async def scenario():
        pipeline.settings.text = Endpoint('https://fixture.invalid', 'unused', 'fixture', '/chat/completions')
        def reject(request): pytest.fail('A cancelled harness reached upstream')
        providers = ApiProviders(pipeline.settings, store, httpx.AsyncClient(transport=httpx.MockTransport(reject)))
        token = 'fixture-relay-cancellation-token'
        before_call = (lambda: store.check_cancelled(job['id'])) if at_scope else None
        relay = create_relay_app(providers, job['id'], token, before_call=before_call)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(relay), base_url='http://fixture.test') as client:
                response = await client.post('/v1/chat/completions',
                    json={'model': 'fixture', 'messages': [{'role': 'user', 'content': 'Fixture question'}]},
                    headers={'Authorization': 'Bearer '+token})
                assert response.status_code == 409
                assert 'task_cancelled' in response.text
        finally: await providers.close()
    asyncio.run(scenario())
    assert store.one('SELECT count(*) n FROM calls')['n'] == 0


@pytest.mark.parametrize('status', ['succeeded', 'failed', 'insufficient_evidence'])
def test_completed_jobs_cannot_be_relabelled_cancelled(case, status):
    store, pipeline = case
    job = new_job(store)
    store.execute('UPDATE jobs SET status=? WHERE id=?', (status, job['id']))
    with pytest.raises(Conflict): pipeline.request_cancel(job['id'])
    assert store.job_control(job['id']) == {'cancel_requested': False, 'cancellable': False}


def test_cancelled_job_cannot_reserve_or_send_any_new_provider_request(case):
    store, pipeline = case
    job = new_job(store)
    pipeline.request_cancel(job['id'])
    with pytest.raises(JobCancelled): store.reserve_call(job['id'], 'search', 'fixture', 100)
    async def scenario():
        settings = pipeline.settings
        settings.text = Endpoint('https://fixture.invalid', 'unused', 'fixture', '/chat/completions')
        def reject(request): pytest.fail('A cancelled task reached transport')
        providers = ApiProviders(settings, store, httpx.AsyncClient(transport=httpx.MockTransport(reject)))
        try:
            with pytest.raises(JobCancelled): await providers.call('text', {}, job['id'])
        finally: await providers.close()
    asyncio.run(scenario())
    assert store.one('SELECT count(*) n FROM calls')['n'] == 0


def test_cancel_during_inflight_call_preserves_cost_and_blocks_next_call(case):
    store, pipeline = case
    job = new_job(store)
    store.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def wire(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={'usage': {'total_tokens': 8}, 'result': 'complete'})
        pipeline.settings.text = Endpoint('https://fixture.invalid', 'unused', 'fixture', '/chat/completions')
        providers = ApiProviders(pipeline.settings, store, httpx.AsyncClient(transport=httpx.MockTransport(wire)))
        try:
            current = asyncio.create_task(providers.call('text', {}, job['id']))
            await entered.wait()
            pipeline.request_cancel(job['id'])
            release.set()
            assert (await current)['result'] == 'complete'
            with pytest.raises(JobCancelled): await providers.call('text', {}, job['id'])
        finally: await providers.close()
    asyncio.run(scenario())
    calls = store.calls_for_job(job['id'])
    assert len(calls) == 1 and calls[0]['status'] == 'succeeded'
    assert calls[0]['usage'] == {'total_tokens': 8}


def test_account_limit_is_transactional_and_idempotent_with_research_exemption(case):
    store, pipeline = case
    user(store, 'alice'); user(store, 'bob')
    store.max_pending_jobs_per_account = 2
    first = new_job(store, 'first', 'alice')
    second = new_job(store, 'second', 'alice')
    store.execute("UPDATE jobs SET status='running' WHERE id=?", (second['id'],))
    assert new_job(store, 'first', 'alice')['id'] == first['id']
    with pytest.raises(Conflict, match='上限'): new_job(store, 'third', 'alice')
    new_job(store, 'first', 'bob')
    for number in range(4): new_job(store, 'research-'+str(number))
    pipeline.request_cancel(first['id'])
    new_job(store, 'third', 'alice')


def test_worker_survives_transient_claim_failure_and_queue_join_finishes(case, monkeypatch):
    store, pipeline = case
    job = new_job(store)
    original = store.execute
    failures = []
    def flaky(sql, args=()):
        if sql.startswith("UPDATE jobs SET status='running'") and not failures:
            failures.append(True)
            raise sqlite3.OperationalError('fixture database busy')
        return original(sql, args)
    monkeypatch.setattr(store, 'execute', flaky)
    async def generated(request, job_id): return {'content_id': 'fixture-content'}
    monkeypatch.setattr(pipeline, 'generate', generated)
    async def scenario():
        await pipeline.start()
        try:
            await asyncio.wait_for(pipeline.queue.join(), 2)
            assert pipeline.readiness()['ready']
            assert pipeline.worker_failures == 1
            assert store.one('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] == 'succeeded'
        finally: await pipeline.stop()
    asyncio.run(scenario())


def test_db_outage_after_claim_does_not_leave_permanently_running_job(case, monkeypatch):
    store, pipeline = case
    job = new_job(store)
    original = store.one
    outage = {'remaining': 3}
    def unavailable(sql, args=()):
        if sql in ('SELECT * FROM jobs WHERE id=?', 'SELECT status FROM jobs WHERE id=?') and outage['remaining']:
            outage['remaining'] -= 1
            raise sqlite3.OperationalError('fixture temporary outage')
        return original(sql, args)
    monkeypatch.setattr(store, 'one', unavailable)
    async def scenario():
        await pipeline.start()
        try:
            await asyncio.wait_for(pipeline.queue.join(), 2)
            assert pipeline.readiness()['ready']
            assert not pipeline.unsettled_jobs
        finally: await pipeline.stop()
    asyncio.run(scenario())
    assert store.one('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] == 'failed'


def test_worker_marks_cooperative_cancel_separately_from_failure(case, monkeypatch):
    store, pipeline = case
    job = new_job(store)
    async def generated(request, job_id):
        store.save_job_evidence(job_id, {'completed': ['q1']})
        store.request_cancel(job_id)
        store.check_cancelled(job_id)
    monkeypatch.setattr(pipeline, 'generate', generated)
    asyncio.run(pipeline.run(job['id']))
    row = store.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
    assert row['status'] == 'cancelled' and row['error'] is None
    assert json.loads(store.one('SELECT evidence FROM job_evidence')['evidence']) == {'completed': ['q1']}


def test_readiness_checks_database_and_live_workers(case, monkeypatch):
    store, pipeline = case
    assert not pipeline.readiness()['ready']
    async def scenario():
        await pipeline.start()
        try:
            assert pipeline.readiness()['ready']
            original = store.one
            monkeypatch.setattr(store, 'one', lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('offline')))
            assert not pipeline.readiness()['database']
            assert not pipeline.readiness()['ready']
            monkeypatch.setattr(store, 'one', original)
            pipeline.workers[0].cancel()
            await asyncio.gather(*pipeline.workers, return_exceptions=True)
            assert not pipeline.readiness()['ready']
        finally: await pipeline.stop()
        assert not pipeline.readiness()['ready']
    asyncio.run(scenario())


def test_startup_keeps_persisted_cancellation_and_does_not_run_job(case, monkeypatch):
    store, pipeline = case
    job = new_job(store)
    store.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
    store.request_cancel(job['id'])
    async def reject(*args): pytest.fail('Cancelled work was relaunched')
    monkeypatch.setattr(pipeline, 'generate', reject)
    async def scenario():
        await pipeline.start()
        try: await asyncio.wait_for(pipeline.queue.join(), 1)
        finally: await pipeline.stop()
    asyncio.run(scenario())
    row = store.one('SELECT status,error FROM jobs WHERE id=?', (job['id'],))
    assert row == {'status': 'cancelled', 'error': None}


def test_revision_authorization_requires_owned_course(case):
    store, pipeline = case
    user(store, 'alice')
    job, _ = store.job('revision', 'revise_question', {'course_id': 'unowned'}, owner_id='alice')
    with pytest.raises(ValueError, match='所属账号'): pipeline.authorize_job(job, {'course_id': 'unowned'})


def test_revisions_are_dispatched_to_dedicated_executor(case, monkeypatch):
    import sys
    store, pipeline = case
    job, _ = store.job('revision', 'revise_question', {'course_id': 'fixture', 'slot_id': 'q2'})
    async def execute_revision(current, payload, job_id):
        assert current is pipeline and job_id == job['id'] and payload['slot_id'] == 'q2'
        return {'content_id': 'fixture', 'version': 2}
    monkeypatch.setitem(sys.modules, 'app.question_revision', SimpleNamespace(execute_revision=execute_revision))
    asyncio.run(pipeline.run(job['id']))
    assert store.one('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] == 'succeeded'


def test_successful_commit_wins_over_late_cancellation(case, monkeypatch):
    store, pipeline = case
    job = new_job(store)
    async def generated(request, job_id):
        # Simulates an already committed result: a later cancellation must not
        # leave an accessible content row labelled as an incomplete task.
        store.request_cancel(job_id)
        return {'content_id': 'already-committed'}
    monkeypatch.setattr(pipeline, 'generate', generated)
    asyncio.run(pipeline.run(job['id']))
    assert store.one('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] == 'succeeded'


def test_full_account_queue_defers_images_without_losing_successful_parse(figure_rig):
    client, app, wire = figure_rig
    app.state.store.max_pending_jobs_per_account = 1
    _, document_id, job = upload(client)
    assert job['status'] == 'succeeded'
    assert job['result']['figures_deferred'] is True
    assert 'figures_job' not in job['result']
    assert app.state.store.one('SELECT status FROM documents WHERE id=?', (document_id,))['status'] == 'parsed'
    assert app.state.store.document_chunks(document_id)
    assert wire.vision == wire.embeddings == 0


def test_cancel_during_parse_preserves_previously_committed_document(figure_rig, monkeypatch):
    from app import document_backends
    client, app, wire = figure_rig
    _, document_id, _ = upload(client, images=False)
    before = app.state.store.document_chunks(document_id)
    original = document_backends.parse
    async def cancelled(*args):
        result = await original(*args)
        job = app.state.store.one("SELECT id FROM jobs WHERE status='running' AND kind='parse'")
        app.state.pipeline.request_cancel(job['id'])
        return result
    monkeypatch.setattr(document_backends, 'parse', cancelled)
    response = client.post('/api/documents/'+document_id+'/parse', json={'tier': 'standard', 'images': False})
    job = await_job(client, response.json()['job_id'])
    assert job['status'] == 'cancelled' and job['error'] is None
    assert app.state.store.document_chunks(document_id) == before
    assert app.state.store.one('SELECT status FROM documents WHERE id=?', (document_id,))['status'] == 'parsed'
