"""Real durable milestones over isolated SQLite/HTTP; models are all fixtures.

No elapsed-time interpolation, model-quality assertions or live API requests.
"""
import asyncio
import json
import sqlite3
from threading import Event

import pytest

from app import job_progress
from app.generation_agent import authorize_resume
from app.pipeline import Pipeline
from app.store import Store, dumps, now
from test_generation_agent import agent_case  # noqa: F401
from test_harness_quality_gates import offline_harness  # noqa: F401
from test_supervisor_agent import planner_wire, supervisor  # noqa: F401
from test_workflow import rig as web_rig, done, seed, request, generated  # noqa: F401
from test_account_isolation import another_client, register


def timeline(rig):
    return job_progress.snapshot(rig.store, rig.job())


def stage_states(value):
    return {stage['id']: stage['status'] for stage in value['stages']}


@pytest.mark.parametrize('phase,stage,activity', [
    ('embed', 'retrieval', 'embedding'), ('generate', 'writing', 'writing'),
])
def test_live_http_reads_real_stage_while_provider_is_paused(web_rig, monkeypatch, phase, stage, activity):
    client, app, wire = web_rig
    course, _ = seed(client)
    entered, release = Event(), Event()
    original = getattr(app.state.pipeline.providers, phase)

    async def paused(*args, **kwargs):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.005)
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.state.pipeline.providers, phase, paused)
    response = client.post('/api/generations', json=request(course) | {'material': 'lesson'})
    try:
        assert response.status_code == 202
        assert entered.wait(3)
        row = client.get('/api/jobs/' + response.json()['job_id']).json()
        value = row['timeline']
        assert row['status'] == 'running'
        assert value['recorded'] and value['version'] == 'job-progress-v1'
        assert value['active_stage'] == stage and value['activity'] == activity
        assert [s['kind'] for s in value['stages']] == ['retrieval', 'writing', 'saving']
        assert stage_states(value)[stage] == 'active'
        assert value['completed'] is None and value['total'] is None and value['unit'] is None
        assert 'reference_chunks' not in dumps(value) and 'percentage' not in value
        # Time grows during the real wait; milestones and event history do not.
        again = client.get('/api/jobs/' + row['id']).json()['timeline']
        assert stage_states(again) == stage_states(value)
        assert again['events'] == value['events']
        assert again['elapsed_ms'] >= value['elapsed_ms']
    finally:
        release.set()
    final = done(client, response)
    assert final['status'] == 'succeeded'
    assert set(stage_states(final['timeline']).values()) == {'completed'}
    assert final['timeline']['activity'] is None
    assert final['result']['content_id']
    assert app.state.store.one("SELECT count(*) AS n FROM calls WHERE job_id=? AND capability='text'", (final['id'],))['n'] == 1


def test_legacy_quiz_reports_real_review_and_repair_without_invented_question_counts(web_rig, monkeypatch):
    client, app, wire = web_rig
    course, _ = seed(client)
    snapshots = []
    original = app.state.pipeline.providers.generate

    async def record(messages, job_id):
        row = app.state.store.one('SELECT * FROM jobs WHERE id=?', (job_id,))
        snapshots.append(job_progress.snapshot(app.state.store, row))
        return await original(messages, job_id)

    monkeypatch.setattr(app.state.pipeline.providers, 'generate', record)
    success = done(client, client.post('/api/generations', json=request(course)))
    assert success['status'] == 'succeeded'
    assert [value['activity'] for value in snapshots] == ['writing', 'reviewing']
    assert all(value['completed'] is None for value in snapshots)
    snapshots.clear()
    wire.mode = 'bad_citation'
    failed = done(client, client.post('/api/generations', json=request(course, 'repair-job')))
    assert failed['status'] == 'failed'
    assert [value['activity'] for value in snapshots] == ['writing', 'repairing', 'repairing']
    assert stage_states(failed['timeline']) == {'retrieval': 'completed', 'writing': 'failed', 'saving': 'pending'}
    assert failed['timeline']['activity'] is None


@pytest.mark.parametrize('mode,status,stage', [('insufficient', 'insufficient_evidence', 'writing'),
                                             ('http_error', 'failed', 'retrieval')])
def test_terminal_status_stops_current_stage_and_leaves_future_work_pending(web_rig, mode, status, stage):
    client, app, wire = web_rig
    course, _ = seed(client)
    wire.mode = mode
    final = done(client, client.post('/api/generations', json=request(course)))
    assert final['status'] == status
    assert stage_states(final['timeline'])[stage] == ('blocked' if mode == 'insufficient' else 'failed')
    assert stage_states(final['timeline'])['saving'] == 'pending'
    assert final['timeline']['activity'] is None
    assert app.state.store.one('SELECT count(*) AS n FROM contents')['n'] == 0


def test_agent_only_counts_passed_questions_and_finishes_saving(agent_case, monkeypatch):
    rig = agent_case(count=2)
    snapshots = []
    original = rig.providers.generate

    async def record(messages, job_id):
        snapshots.append(timeline(rig))
        return await original(messages, job_id)

    monkeypatch.setattr(rig.providers, 'generate', record)
    assert rig.run()['status'] == 'succeeded'
    assert [value['activity'] for value in snapshots] == ['writing', 'solving', 'reviewing'] * 2
    assert [value['completed'] for value in snapshots] == [0, 0, 0, 1, 1, 1]
    assert all(value['total'] == 2 and value['unit'] == 'questions' for value in snapshots)
    final = timeline(rig)
    assert final['completed'] == final['total'] == 2
    assert stage_states(final) == {'retrieval': 'completed', 'writing': 'completed', 'saving': 'completed'}
    assert rig.text_call_count() == 6
    # A new Store instance can recover the same durable display state.
    reloaded = Store(rig.settings.data_dir)
    assert job_progress.snapshot(reloaded, rig.job()) == final


def test_supervisor_plan_is_a_real_separate_stage(agent_case, offline_harness, planner_wire, monkeypatch):
    rig = supervisor(agent_case, count=1)
    snapshots = []
    original = rig.providers.generate

    async def record(messages, job_id):
        snapshots.append(timeline(rig))
        return await original(messages, job_id)

    monkeypatch.setattr(rig.providers, 'generate', record)
    assert rig.run()['status'] == 'succeeded'
    assert snapshots[0]['active_stage'] == 'planning' and snapshots[0]['activity'] == 'planning'
    assert stage_states(snapshots[0]) == {'retrieval': 'completed', 'planning': 'active', 'writing': 'pending', 'saving': 'pending'}
    assert [stage['id'] for stage in timeline(rig)['stages']] == ['retrieval', 'planning', 'writing', 'saving']
    assert rig.text_call_count() == 4


def test_resume_retains_accepted_counts_and_never_replays_retrieval(agent_case):
    rig = agent_case(count=2, wire_options={'fail_phase': ('agent_solve', 2)})
    assert rig.run()['status'] == 'failed'
    failed = timeline(rig)
    assert failed['completed'] == 1 and stage_states(failed)['writing'] == 'failed'
    authorize_resume(rig.store, rig.job_id)
    pending = timeline(rig)
    assert pending['completed'] == 1
    assert pending['active_stage'] is None and pending['activity'] is None
    assert stage_states(pending) == {'retrieval': 'completed', 'writing': 'pending', 'saving': 'pending'}
    assert rig.run()['status'] == 'succeeded'
    assert timeline(rig)['completed'] == 2
    assert len(rig.retrieval_calls) == 1
    assert rig.wire.author_counts[1] == rig.wire.author_counts[2] == 1


def test_process_restart_persists_interruption_without_advancing_future_stages(agent_case):
    rig = agent_case(count=1)
    job_progress.begin(rig.store, rig.job_id, ['retrieval', 'writing', 'saving'], total=1)
    job_progress.update(rig.store, rig.job_id, 'retrieval', activity='embedding')
    rig.store.execute("UPDATE jobs SET status='running' WHERE id=?", (rig.job_id,))

    async def restart():
        # No queued work or provider invocation; recovery only terminates old work.
        await rig.pipeline.start()
        await rig.pipeline.stop()

    asyncio.run(restart())
    assert rig.job()['status'] == 'failed'
    persisted = json.loads(rig.store.one('SELECT timeline FROM job_timelines WHERE job_id=?', (rig.job_id,))['timeline'])
    assert stage_states(persisted) == {'retrieval': 'failed', 'writing': 'pending', 'saving': 'pending'}
    assert persisted['activity'] is None
    assert rig.text_call_count() == 0


@pytest.mark.parametrize('kind', ['audio', 'image'])
def test_media_uses_same_stage_contract_without_future_modalities(web_rig, monkeypatch, kind):
    client, app, wire = web_rig
    course, _ = seed(client)
    content_id = generated(client, course)
    client.post('/api/contents/' + content_id + '/review', json={'version': 1, 'action': 'approve'})
    original = getattr(app.state.pipeline.providers, 'speech' if kind == 'audio' else 'image')
    snapshots = []

    async def record(data, job_id):
        row = app.state.store.one('SELECT * FROM jobs WHERE id=?', (job_id,))
        snapshots.append(job_progress.snapshot(app.state.store, row))
        return await original(data, job_id)

    monkeypatch.setattr(app.state.pipeline.providers, 'speech' if kind == 'audio' else 'image', record)
    final = done(client, client.post('/api/contents/' + content_id + '/media',
        json={'version': 1, 'kind': kind, 'request_key': 'progress-media-' + kind}))
    assert final['status'] == 'succeeded'
    assert snapshots[0]['active_stage'] == kind
    assert snapshots[0]['activity'] == 'generating_' + kind
    assert stage_states(final['timeline']) == {kind: 'completed', 'saving': 'completed'}
    assert final['result']['content_id'] == content_id
    assert final['result']['media_id']


def test_old_job_has_no_invented_history_and_timeline_keeps_owner_boundary(web_rig):
    client, app, wire = web_rig
    course, _ = seed(client)
    store = app.state.store
    owner = client.headers['X-Account-ID']
    row, _ = store.job('old-no-timeline', 'generate', request(course), owner_id=owner)
    store.execute("UPDATE jobs SET status='succeeded',result=? WHERE id=?", (dumps({'content_id': 'old-id'}), row['id']))
    for malformed in ('null', '{"agent": {}}', 'broken JSON'):
        store.save_job_evidence(row['id'], {})
        store.execute('UPDATE job_evidence SET evidence=? WHERE job_id=?', (malformed, row['id']))
        response = client.get('/api/jobs/' + row['id'])
        assert response.status_code == 200
        assert response.json()['timeline']['recorded'] is False
        assert response.json()['timeline']['stages'] == []
        assert 'progress' not in response.json()
    job_progress.begin(store, row['id'], ['retrieval', 'writing', 'saving'])
    with another_client(app, client) as other:
        assert other.get('/api/jobs/' + row['id']).status_code == 401
        register(other, 'progress-other@example.test', 'Other')
        forbidden = other.get('/api/jobs/' + row['id'])
        assert forbidden.status_code == 404
        assert 'timeline' not in forbidden.text


def test_direct_research_update_does_not_create_job_or_timeline(tmp_path):
    store = Store(tmp_path)
    job_progress.update(store, 'not-a-persisted-job', 'retrieval', activity='embedding')
    job_progress.finish(store, 'not-a-persisted-job', 'succeeded')
    assert store.all('SELECT * FROM jobs') == []
    assert store.all('SELECT * FROM job_timelines') == []


def test_job_status_wins_if_process_died_between_terminal_and_timeline_writes(agent_case):
    rig = agent_case(count=1)
    job_progress.begin(rig.store, rig.job_id, ['retrieval', 'writing', 'saving'])
    job_progress.update(rig.store, rig.job_id, 'retrieval', activity='embedding')
    rig.store.execute("UPDATE jobs SET status='failed' WHERE id=?", (rig.job_id,))
    value = timeline(rig)
    assert value['activity'] is None
    assert stage_states(value) == {'retrieval': 'failed', 'writing': 'pending', 'saving': 'pending'}


@pytest.mark.parametrize('corrupt', [None, [], {'stages': [None]}, {'stages': [{}]},
    {'completed': -1}, {'total': True}, {'active_stage': []}, {'stages': [{'id': 'private source text', 'kind': 'text', 'status': 'active'}]}])
def test_malformed_display_history_falls_back_to_unknown(agent_case, corrupt):
    rig = agent_case(count=1)
    job_progress.begin(rig.store, rig.job_id, ['retrieval', 'writing', 'saving'], total=1)
    value = timeline(rig)
    raw = (value | corrupt) if isinstance(corrupt, dict) else corrupt
    rig.store.execute('UPDATE job_timelines SET timeline=? WHERE job_id=?', (dumps(raw), rig.job_id))
    snapshot = timeline(rig)
    assert snapshot['recorded'] is False and snapshot['stages'] == []


def test_display_projection_never_returns_unrecognized_metadata(agent_case):
    rig = agent_case(count=1)
    job_progress.begin(rig.store, rig.job_id, ['retrieval', 'writing', 'saving'])
    value = timeline(rig)
    value['internal_prompt'] = 'PRIVATE_MODEL_TEXT'
    value['stages'][0]['source'] = 'PRIVATE_SOURCE_TEXT'
    rig.store.execute('UPDATE job_timelines SET timeline=? WHERE job_id=?', (dumps(value), rig.job_id))
    assert 'PRIVATE' not in dumps(timeline(rig))


def test_failed_display_write_does_not_reverse_successful_business_result(agent_case, monkeypatch):
    rig = agent_case(count=1)
    original = job_progress._write

    def failed_finish(db, job_id, value):
        if value['stages'][-1]['status'] == 'completed':
            raise sqlite3.OperationalError('fixture progress write failure')
        return original(db, job_id, value)

    monkeypatch.setattr(job_progress, '_write', failed_finish)
    assert rig.run()['status'] == 'succeeded'
    assert rig.contents()
    assert set(stage_states(timeline(rig)).values()) == {'completed'}
    assert timeline(rig)['activity'] is None


def test_explicit_stage_reexecution_is_visible_without_resetting_accepted_count(agent_case):
    rig = agent_case(count=2)
    job_progress.begin(rig.store, rig.job_id, ['retrieval', 'planning', 'writing', 'saving'], total=2)
    rig.store.execute("UPDATE jobs SET status='running' WHERE id=?", (rig.job_id,))
    job_progress.update(rig.store, rig.job_id, 'planning', complete=True, completed=1)
    job_progress.update(rig.store, rig.job_id, 'planning', activity='repairing')
    value = timeline(rig)
    assert value['active_stage'] == 'planning' and value['activity'] == 'repairing'
    assert value['completed'] == 1
    assert stage_states(value)['planning'] == 'active'


@pytest.mark.parametrize('workflow', ['legacy_v3', 'agent_v1'])
def test_empty_retrieval_blocks_at_retrieval_without_generating(agent_case, workflow):
    rig = agent_case(count=1, settings_options={'generation_workflow': workflow})

    async def empty(*args, **kwargs):
        return [], {'selected_count': 0}

    rig.pipeline.retrieve = empty
    assert rig.run()['status'] == 'insufficient_evidence'
    assert stage_states(timeline(rig)) == {'retrieval': 'blocked', 'writing': 'pending', 'saving': 'pending'}
    assert rig.text_call_count() == 0


def test_planner_schema_repair_stays_in_planning_stage(agent_case, monkeypatch):
    from app.generation_agent import GenerationAgent
    from app.models import GenerateRequest
    rig = agent_case(count=1)
    rig.store.execute("UPDATE jobs SET status='running' WHERE id=?", (rig.job_id,))
    job_progress.begin(rig.store, rig.job_id, ['retrieval', 'planning', 'writing', 'saving'], total=1)
    observations = []

    async def repair_fixture(*args):
        observations.append(timeline(rig))
        return {}

    monkeypatch.setattr(rig.providers, 'generate', repair_fixture)

    async def check():
        agent = GenerationAgent(rig.pipeline, GenerateRequest(**json.loads(rig.job()['payload'])), rig.job_id)
        await agent.initialize()
        await agent.call({}, 'plan_schema_repair', {}, 'Fixture-only JSON repair')

    asyncio.run(check())
    assert observations[0]['active_stage'] == 'planning'
    assert observations[0]['activity'] == 'repairing'
    assert stage_states(observations[0])['writing'] == 'pending'


def test_fusion_and_reranker_publish_only_actual_retrieval_activities(web_rig, monkeypatch):
    from app.config import Endpoint
    from test_query_fusion_runtime import install_rewriter
    client, app, wire = web_rig
    course, _ = seed(client)
    app.state.settings.rerank = Endpoint('https://test.invalid/v1', 'test-key-not-real', 'fixture-rerank', '/rerank')
    install_rewriter(monkeypatch, wire)
    observations = []
    provider = app.state.pipeline.providers

    def wrapper(original):
        async def inspect(*args, **kwargs):
            job_id = args[-1]
            row = app.state.store.one('SELECT * FROM jobs WHERE id=?', (job_id,))
            observations.append(job_progress.snapshot(app.state.store, row))
            return await original(*args, **kwargs)
        return inspect

    for name in ('embed', 'generate', 'rerank'):
        monkeypatch.setattr(provider, name, wrapper(getattr(provider, name)))
    final = done(client, client.post('/api/generations', json=request(course) | {'query_fusion': True}))
    assert final['status'] == 'succeeded'
    assert [row['activity'] for row in observations] == ['embedding', 'rewriting', 'rewriting', 'reranking', 'writing', 'reviewing']
    assert all(row['active_stage'] == 'retrieval' for row in observations[:4])
    assert app.state.store.one('SELECT count(*) AS n FROM calls WHERE job_id=?', (final['id'],))['n'] == 6


def test_web_restart_leaves_unassigned_legacy_progress_untouched(agent_case):
    rig = agent_case(count=1)
    rig.pipeline.enforce_account_ownership = True
    job_progress.begin(rig.store, rig.job_id, ['retrieval', 'writing', 'saving'])
    job_progress.update(rig.store, rig.job_id, 'retrieval', activity='embedding')
    rig.store.execute("UPDATE jobs SET status='failed' WHERE id=?", (rig.job_id,))
    before = rig.store.one('SELECT timeline FROM job_timelines WHERE job_id=?', (rig.job_id,))['timeline']

    async def restart():
        await rig.pipeline.start()
        await rig.pipeline.stop()

    asyncio.run(restart())
    assert rig.store.one('SELECT timeline FROM job_timelines WHERE job_id=?', (rig.job_id,))['timeline'] == before
    assert rig.text_call_count() == 0


@pytest.fixture
def progress_clock(monkeypatch):
    from datetime import datetime, timedelta, timezone

    class Clock:
        tick = 0
        wall = 0.0

        def advance(self, seconds, *, wall_seconds=None):
            self.tick += int(seconds * 1_000_000_000)
            self.wall += seconds if wall_seconds is None else wall_seconds

        def iso(self):
            return (datetime(2026, 9, 20, tzinfo=timezone.utc) + timedelta(seconds=self.wall)).isoformat()

    clock = Clock()
    monkeypatch.setattr(job_progress.time, 'monotonic_ns', lambda: clock.tick)
    monkeypatch.setattr(job_progress, 'now', clock.iso)
    return clock


def timed_case(agent_case):
    rig = agent_case(count=2)
    rig.store.execute("UPDATE jobs SET status='running' WHERE id=?", (rig.job_id,))
    job_progress.begin(rig.store, rig.job_id, ['retrieval', 'writing', 'saving'], total=2)
    return rig


def raw_timeline(rig):
    return rig.store.one('SELECT timeline FROM job_timelines WHERE job_id=?', (rig.job_id,))['timeline']


def one_stage(rig, stage):
    return next(s for s in timeline(rig)['stages'] if s['id'] == stage)


def test_server_timing_uses_monotonic_clock_and_repeated_updates_do_not_restart_it(agent_case, progress_clock):
    rig = timed_case(agent_case)
    job_progress.update(rig.store, rig.job_id, 'retrieval', activity='embedding')
    start = one_stage(rig, 'retrieval')['started_at']
    before = raw_timeline(rig)
    progress_clock.advance(1.25, wall_seconds=900)
    assert one_stage(rig, 'retrieval')['elapsed_ms'] == 1250
    assert one_stage(rig, 'retrieval')['elapsed_ms'] == 1250
    assert raw_timeline(rig) == before  # Reading is not a timing write or heartbeat.
    event_count = len(timeline(rig)['events'])
    job_progress.update(rig.store, rig.job_id, 'retrieval', activity='embedding')
    assert one_stage(rig, 'retrieval')['started_at'] == start
    assert len(timeline(rig)['events']) == event_count
    progress_clock.advance(.75, wall_seconds=-1000)
    job_progress.update(rig.store, rig.job_id, 'retrieval', complete=True)
    frozen = one_stage(rig, 'retrieval')
    assert frozen['elapsed_ms'] == 2000 and frozen['timing_complete'] is True
    progress_clock.advance(50)
    assert one_stage(rig, 'retrieval') == frozen
    job_progress.update(rig.store, rig.job_id, 'writing', activity='writing')
    progress_clock.advance(3)
    assert timeline(rig)['elapsed_ms'] == 5000
    assert timeline(rig)['timing_complete'] is True
    assert '_timer' not in dumps(timeline(rig)) and job_progress.RUN_ID not in dumps(timeline(rig))


def test_failed_attempts_accumulate_but_paused_and_queued_time_are_excluded(agent_case, progress_clock):
    rig = timed_case(agent_case)
    job_progress.update(rig.store, rig.job_id, 'writing', activity='writing')
    progress_clock.advance(3)
    rig.store.execute("UPDATE jobs SET status='failed' WHERE id=?", (rig.job_id,))
    job_progress.finish(rig.store, rig.job_id, 'failed')
    first = one_stage(rig, 'writing')
    progress_clock.advance(10)
    with rig.store.connect() as db:
        db.execute("UPDATE jobs SET status='queued' WHERE id=?", (rig.job_id,))
        job_progress.queued(db, rig.job_id)
    progress_clock.advance(20)
    assert one_stage(rig, 'writing')['elapsed_ms'] == 3000
    assert timeline(rig)['activity'] is None
    rig.store.execute("UPDATE jobs SET status='running' WHERE id=?", (rig.job_id,))
    job_progress.update(rig.store, rig.job_id, 'writing', activity='solving')
    progress_clock.advance(1.5)
    job_progress.update(rig.store, rig.job_id, 'writing', complete=True)
    last = one_stage(rig, 'writing')
    assert last['elapsed_ms'] == 4500 and last['timing_complete'] is True
    assert last['started_at'] == first['started_at']
    assert last['finished_at'] != first['finished_at']
    codes = [e['code'] for e in timeline(rig)['events']]
    assert codes.count('stage_started') == 2 and codes.count('task_resumed') == 1


def test_cancelled_timer_and_event_are_idempotent_and_resume_excludes_pause(agent_case, progress_clock):
    rig = timed_case(agent_case)
    job_progress.update(rig.store, rig.job_id, 'retrieval', activity='embedding')
    progress_clock.advance(1)
    job_progress.update(rig.store, rig.job_id, 'retrieval', complete=True)
    job_progress.update(rig.store, rig.job_id, 'writing', activity='reviewing', completed=1)
    progress_clock.advance(2.5)
    rig.store.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (rig.job_id,))
    job_progress.finish(rig.store, rig.job_id, 'cancelled')
    first = timeline(rig)
    assert first['elapsed_ms'] == 3500 and first['timing_complete'] is True
    assert first['completed'] == 1 and first['activity'] is None
    assert stage_states(first) == {'retrieval': 'completed', 'writing': 'cancelled', 'saving': 'pending'}
    assert first['events'][-1]['code'] == 'task_cancelled'
    stored = raw_timeline(rig)
    progress_clock.advance(30)
    job_progress.finish(rig.store, rig.job_id, 'cancelled')
    assert raw_timeline(rig) == stored
    assert timeline(rig) == first
    with rig.store.connect() as db:
        db.execute("UPDATE jobs SET status='queued' WHERE id=?", (rig.job_id,))
        job_progress.queued(db, rig.job_id)
    progress_clock.advance(20)
    assert timeline(rig)['elapsed_ms'] == 3500
    assert stage_states(timeline(rig)) == {'retrieval': 'completed', 'writing': 'pending', 'saving': 'pending'}
    rig.store.execute("UPDATE jobs SET status='running' WHERE id=?", (rig.job_id,))
    job_progress.update(rig.store, rig.job_id, 'writing', activity='reviewing')
    progress_clock.advance(1.5)
    job_progress.update(rig.store, rig.job_id, 'writing', complete=True)
    last = timeline(rig)
    assert last['elapsed_ms'] == 5000 and one_stage(rig, 'writing')['elapsed_ms'] == 4000
    codes = [event['code'] for event in last['events']]
    assert codes.count('task_cancelled') == codes.count('task_resumed') == 1
    assert 'task_failed' not in codes


def test_crash_keeps_only_checkpointed_duration_and_marks_missing_tail(agent_case, progress_clock, monkeypatch):
    rig = timed_case(agent_case)
    job_progress.update(rig.store, rig.job_id, 'writing', activity='writing')
    progress_clock.advance(2)
    job_progress.update(rig.store, rig.job_id, 'writing', activity='reviewing')
    progress_clock.advance(4)
    # Simulate a different interpreter boot. Its monotonic origin is unrelated.
    monkeypatch.setattr(job_progress, 'RUN_ID', 'f' * 32)
    progress_clock.advance(10000)
    assert one_stage(rig, 'writing')['elapsed_ms'] == 2000
    assert one_stage(rig, 'writing')['timing_complete'] is False
    rig.store.execute("UPDATE jobs SET status='failed' WHERE id=?", (rig.job_id,))
    job_progress.finish(rig.store, rig.job_id, 'failed', interruption='restart')
    crashed = one_stage(rig, 'writing')
    assert crashed['elapsed_ms'] == 2000 and crashed['finished_at'] is None
    assert timeline(rig)['events'][-1]['data'] == {'cause': 'restart'}
    progress_clock.advance(5000)
    assert one_stage(rig, 'writing') == crashed
    with rig.store.connect() as db:
        job_progress.queued(db, rig.job_id)
    rig.store.execute("UPDATE jobs SET status='running' WHERE id=?", (rig.job_id,))
    job_progress.update(rig.store, rig.job_id, 'writing', activity='reviewing')
    progress_clock.advance(5)
    job_progress.update(rig.store, rig.job_id, 'writing', complete=True)
    assert one_stage(rig, 'writing')['elapsed_ms'] == 7000
    assert timeline(rig)['timing_complete'] is False


def test_graceful_service_stop_has_a_known_stop_time(agent_case, progress_clock):
    rig = timed_case(agent_case)
    job_progress.update(rig.store, rig.job_id, 'writing', activity='writing')
    progress_clock.advance(2.5)
    rig.store.execute("UPDATE jobs SET status='failed' WHERE id=?", (rig.job_id,))
    job_progress.finish(rig.store, rig.job_id, 'failed', interruption='service_stop')
    stopped = one_stage(rig, 'writing')
    assert stopped['elapsed_ms'] == 2500 and stopped['timing_complete'] is True
    assert stopped['finished_at'] == progress_clock.iso()
    assert timeline(rig)['events'][-1]['data'] == {'cause': 'service_stop'}
    progress_clock.advance(50)
    assert one_stage(rig, 'writing') == stopped


def test_legacy_stage_times_remain_unknown_without_invented_elapsed_history(agent_case, progress_clock):
    rig = timed_case(agent_case)
    value = json.loads(raw_timeline(rig))
    for stage in value['stages']:
        for key in ('elapsed_ms', 'timing_complete', 'started_at', 'finished_at', '_elapsed_ns', '_timer'):
            stage.pop(key, None)
    for key in ('events', '_event_seq', 'events_truncated'):
        value.pop(key, None)
    rig.store.execute('UPDATE job_timelines SET timeline=? WHERE job_id=?', (dumps(value), rig.job_id))
    assert timeline(rig)['elapsed_ms'] is None
    assert timeline(rig)['timing_complete'] is False
    assert timeline(rig)['events'] == []
    assert all(s['elapsed_ms'] is None and not s['timing_complete'] for s in timeline(rig)['stages'])
    job_progress.update(rig.store, rig.job_id, 'writing', activity='writing')
    progress_clock.advance(2)
    assert one_stage(rig, 'writing')['elapsed_ms'] == 2000
    assert one_stage(rig, 'writing')['timing_complete'] is False


def test_event_history_is_bounded_monotonic_and_uses_only_whitelisted_values(agent_case, progress_clock):
    rig = timed_case(agent_case)
    job_progress.update(rig.store, rig.job_id, 'writing', activity='writing')
    for number in range(130):
        job_progress.event(rig.store, rig.job_id, 'writing', 'question_passed', number=number + 1,
            completed=number + 1, total=130, hidden_reasoning='PRIVATE_COT')
    value = timeline(rig)
    assert len(value['events']) == 120 and value['events_truncated'] is True
    assert [e['id'] for e in value['events']] == list(range(13, 133))
    assert 'PRIVATE_COT' not in dumps(value)
    before = value['events']
    job_progress.event(rig.store, rig.job_id, 'writing', 'model_reasoning', reasoning='PRIVATE_COT')
    job_progress.event(rig.store, rig.job_id, 'retrieval', 'fusion_fallback', reason='PRIVATE_MODEL_REASON')
    job_progress.event(rig.store, rig.job_id, 'writing', 'question_passed', number=True, completed=1, total=1)
    assert timeline(rig)['events'] == before
    job_progress.event(rig.store, rig.job_id, 'retrieval', 'fusion_fallback', reason='ambiguous_input')
    assert timeline(rig)['events'][-1]['id'] == 133


def test_malformed_imported_events_and_timing_never_leak_private_fields(agent_case):
    rig = timed_case(agent_case)
    job_progress.event(rig.store, rig.job_id, 'retrieval', 'retrieval_selected', selected=3)
    value = json.loads(raw_timeline(rig))
    value['events'][0]['data']['model_reasoning'] = 'PRIVATE_REASONING'
    value['events'].extend([
        {'id': 2, 'stage': 'retrieval', 'code': 'activity_changed', 'at': now(), 'data': {'activity': 'PRIVATE_TEXT'}},
        {'id': 3, 'stage': 'retrieval', 'code': 'unknown', 'at': now(), 'data': {'reason': 'PRIVATE_TEXT'}}, None])
    value['stages'][0].update(elapsed_ms='PRIVATE_TIME', started_at='PRIVATE_START', timing_complete=True)
    rig.store.execute('UPDATE job_timelines SET timeline=? WHERE job_id=?', (dumps(value), rig.job_id))
    clean = timeline(rig)
    assert len(clean['events']) == 1 and clean['events'][0]['data'] == {'selected': 3}
    assert 'PRIVATE' not in dumps(clean)
    assert clean['stages'][0]['elapsed_ms'] is None and clean['stages'][0]['timing_complete'] is False


@pytest.mark.parametrize('reason', ['format', 'difficulty', 'quality', 'evidence', 'duplicate'])
def test_check_failure_events_only_persist_safe_categories_and_keep_legacy_events(agent_case, reason):
    rig = timed_case(agent_case)
    job_progress.event(rig.store, rig.job_id, 'writing', 'question_check_failed',
        number=2, attempt=3, reason=reason, model_reasoning='PRIVATE_REASONING', detail='PRIVATE_OUTPUT')
    event = timeline(rig)['events'][-1]
    assert event['code'] == 'question_check_failed'
    assert event['data'] == {'number': 2, 'attempt': 3, 'reason': reason}
    assert 'PRIVATE' not in raw_timeline(rig)
    before = timeline(rig)['events']
    for data in [
        {'number': 2, 'attempt': 3, 'reason': 'PRIVATE_REASON'},
        {'number': 2, 'attempt': 3, 'reason': None},
        {'number': 2, 'attempt': 3, 'reason': ['format']},
        {'number': True, 'attempt': 3, 'reason': reason},
        {'number': 2, 'attempt': '3', 'reason': reason},
    ]:
        job_progress.event(rig.store, rig.job_id, 'writing', 'question_check_failed', **data)
    assert timeline(rig)['events'] == before
    # Reading imported data follows the same whitelist as the write boundary.
    value = json.loads(raw_timeline(rig))
    value['events'][-1]['data']['private'] = 'PRIVATE_IMPORTED'
    value['events'].append({'id': event['id'] + 1, 'stage': 'writing',
        'code': 'question_check_failed', 'at': now(),
        'data': {'number': 2, 'attempt': 3, 'reason': 'PRIVATE_REASON'}})
    rig.store.execute('UPDATE job_timelines SET timeline=? WHERE job_id=?', (dumps(value), rig.job_id))
    assert timeline(rig)['events'] == before
    assert 'PRIVATE' not in dumps(timeline(rig))
    job_progress.event(rig.store, rig.job_id, 'writing', 'question_rejected', number=2, attempt=3)
    assert timeline(rig)['events'][-1]['data'] == {'number': 2, 'attempt': 3}


def test_format_adaptation_events_only_record_host_owned_steps_and_level(agent_case):
    rig = timed_case(agent_case)
    job_progress.event(rig.store, rig.job_id, 'writing', 'question_format_adapting',
        number=2, private='PRIVATE_RAW_OUTPUT')
    job_progress.event(rig.store, rig.job_id, 'writing', 'question_format_adapted',
        number=2, level='compatible', private='PRIVATE_RAW_OUTPUT')
    before = timeline(rig)['events']
    assert [e['code'] for e in before] == ['question_format_adapting', 'question_format_adapted']
    assert [e['data'] for e in before] == [{'number': 2}, {'number': 2, 'level': 'compatible'}]
    for level in ('PRIVATE_RAW_OUTPUT', 'unchecked', None, ['compatible']):
        job_progress.event(rig.store, rig.job_id, 'writing', 'question_format_adapted', number=2, level=level)
    job_progress.event(rig.store, rig.job_id, 'writing', 'question_format_adapting', number=True)
    assert timeline(rig)['events'] == before
    assert 'PRIVATE' not in raw_timeline(rig)


def test_real_agent_events_report_observed_passes_and_repairs_without_model_text(agent_case):
    rig = agent_case(count=2, wire_options={'repair_once_at': 2})
    assert rig.run()['status'] == 'succeeded'
    events = timeline(rig)['events']
    selected = [e for e in events if e['code'] == 'retrieval_selected']
    assert [e['data'] for e in selected] == [{'selected': 1}]
    assert [e['data']['number'] for e in events if e['code'] == 'question_started'] == [1, 2]
    assert [e['data']['number'] for e in events if e['code'] == 'question_passed'] == [1, 2]
    assert [e['data'] for e in events if e['code'] == 'question_check_failed'] == [{'number': 2, 'attempt': 1, 'reason': 'quality'}]
    assert [e['data'] for e in events if e['code'] == 'question_repair'] == [{'number': 2, 'attempt': 2}]
    assert events[-1]['code'] == 'task_succeeded'
    assert 'PRIVATE_AUTHOR' not in dumps(timeline(rig))
    assert 'PRIVATE_AUTHOR' in dumps(rig.evidence())  # Raw evidence remains separate.
    assert timeline(rig)['elapsed_ms'] >= 0 and timeline(rig)['timing_complete'] is True
    assert all(s['started_at'] and s['finished_at'] and s['elapsed_ms'] >= 0 for s in timeline(rig)['stages'])


def test_optional_event_failure_does_not_change_generation_result(agent_case, monkeypatch):
    rig = agent_case(count=1)
    original = job_progress._write

    def fail_event(db, job_id, value):
        if value['events'] and value['events'][-1]['code'] == 'question_passed':
            raise sqlite3.OperationalError('fixture optional event failure')
        return original(db, job_id, value)

    monkeypatch.setattr(job_progress, '_write', fail_event)
    assert rig.run()['status'] == 'succeeded'
    assert rig.contents() and rig.text_call_count() == 3


def test_final_rejection_does_not_claim_another_repair_started(agent_case):
    rig = agent_case(count=1, wire_options={'review_fail_at': 1}, settings_options={'agent_max_repairs': 0})
    assert rig.run()['status'] == 'failed'
    events = timeline(rig)['events']
    assert [e['data'] for e in events if e['code'] == 'question_check_failed'] == [
        {'number': 1, 'attempt': attempt, 'reason': 'quality'} for attempt in (1, 2, 3)]
    assert [e['data'] for e in events if e['code'] == 'question_repair'] == [
        {'number': 1, 'attempt': attempt} for attempt in (2, 3)]
    assert events[-1]['code'] == 'task_failed'


def test_clock_failure_is_nonfatal_for_generation_and_progress_read(agent_case, monkeypatch):
    rig = agent_case(count=1)

    def failed_clock():
        raise RuntimeError('fixture monotonic clock unavailable')

    monkeypatch.setattr(job_progress.time, 'monotonic_ns', failed_clock)
    assert rig.run()['status'] == 'succeeded'
    assert rig.contents() and rig.text_call_count() == 3
    assert timeline(rig)['recorded'] is False
    assert timeline(rig)['elapsed_ms'] is None
