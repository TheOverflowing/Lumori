"""Startup only retires candidate indexes belonging to newly interrupted jobs."""
import asyncio
import json

from app import exploration_staging
from app.config import Settings
from app.models import GenerateRequest
from app.pipeline import Pipeline
from app.store import Store, dumps, now


class NoRemoteProviders:
    async def close(self):
        pass


def make_pipeline(tmp_path):
    settings = Settings(data_dir=tmp_path, workers=0)
    store = Store(tmp_path)
    store.execute('INSERT INTO users VALUES(?,?,?,?,?)', ('owner', 'owner@example.test', 'fixture-only', 'Owner', now()))
    store.execute('INSERT INTO courses VALUES(?,?,?)', ('course', 'Course', now()))
    store.execute('INSERT INTO course_owners VALUES(?,?)', ('course', 'owner'))
    return Pipeline(settings, store, NoRemoteProviders(), enforce_account_ownership=True)


def seed(pipeline, name, status, *, owned=True):
    request = GenerateRequest(course_id='course', topic='Public educational material', request_key='recovery-' + name)
    job, _ = pipeline.store.job(request.request_key, 'generate', request.model_dump(), owner_id='owner' if owned else None)
    pipeline.store.execute('UPDATE jobs SET status=? WHERE id=?', (status, job['id']))
    stage = {'status': 'indexing', 'configuration': {'course_id': 'course'},
             'fingerprint': 'fixture', 'created_at': now(),
             'audit': {'embedding_calls': 1, 'chunk_count': 1},
             'chunks': [{'text': 'Temporary source body'}], 'vectors': [[1., 0.]],
             'report': {'pages': [{'text': 'Temporary source body'}]}, 'passages': []}
    pipeline.store.execute('INSERT INTO exploration_candidate_indexes VALUES(?,?,?)', (job['id'], 'fixture-sha', dumps(stage)))
    return job['id'], stage


def stage(pipeline, job_id):
    return json.loads(pipeline.store.one('SELECT state FROM exploration_candidate_indexes WHERE job_id=?', (job_id,))['state'])


def start(pipeline):
    async def run():
        await pipeline.start()
        await pipeline.stop()
    asyncio.run(run())


def test_recovery_retires_only_newly_interrupted_owned_jobs(tmp_path):
    pipeline = make_pipeline(tmp_path)
    interrupted, _ = seed(pipeline, 'interrupted', 'running')
    queued, queued_stage = seed(pipeline, 'queued', 'queued')
    old_failed, failed_stage = seed(pipeline, 'old-failed', 'failed')
    legacy_running, legacy_stage = seed(pipeline, 'legacy-running', 'running', owned=False)
    start(pipeline)
    recovered = stage(pipeline, interrupted)
    assert pipeline.store.one('SELECT status FROM jobs WHERE id=?', (interrupted,))['status'] == 'failed'
    assert recovered['status'] == 'retired' and recovered['previous_status'] == 'indexing'
    assert recovered['audit'] == {'embedding_calls': 1, 'chunk_count': 1}
    assert not {'chunks', 'vectors', 'report', 'passages'}.intersection(recovered)
    assert stage(pipeline, queued) == queued_stage
    assert stage(pipeline, old_failed) == failed_stage
    assert stage(pipeline, legacy_running) == legacy_stage
    assert pipeline.store.one('SELECT status FROM jobs WHERE id=?', (legacy_running,))['status'] == 'running'


def test_newly_running_job_after_recovery_capture_is_not_retired(tmp_path, monkeypatch):
    pipeline = make_pipeline(tmp_path)
    interrupted, _ = seed(pipeline, 'old-running', 'running')
    original = exploration_staging.retire_stages
    later = []

    def retire(store, job_id):
        if not later:
            # Simulate another task becoming active after the recovery update.
            # Cleanup must use its captured IDs, not a fresh broad status scan.
            later.append(seed(pipeline, 'new-running', 'running'))
        return original(store, job_id)

    monkeypatch.setattr(exploration_staging, 'retire_stages', retire)
    start(pipeline)
    new_id, new_stage = later[0]
    assert stage(pipeline, interrupted)['status'] == 'retired'
    assert stage(pipeline, new_id) == new_stage
    assert pipeline.store.one('SELECT status FROM jobs WHERE id=?', (new_id,))['status'] == 'running'
