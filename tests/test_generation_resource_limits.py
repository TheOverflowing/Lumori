"""Local quota stops are recoverable host limits, never upstream failures.

Isolated SQLite and HTTP fixtures exercise the normal Pipeline without a real
provider. These tests do not alter the frozen Q1 results or spend API calls.
"""
import asyncio

import httpx
import pytest

from app.generation_agent import authorize_resume, progress
from app.task_control import LocalCallLimit
from app.store import now
from test_agent_resume_api import rig as account_rig, seed_failed
from test_generation_agent import agent_case


def test_q1_three_remaining_calls_is_local_preflight_limit_and_recovers(agent_case):
    rig = agent_case(count=1, settings_options={'max_daily_calls': 192, 'agent_max_calls': 12})
    # Reproduce Q1: other work consumed 189 calls, this job has not called anything.
    with rig.store.connect() as db:
        db.executemany('INSERT INTO calls(id,capability,model,status,usage,created_at) VALUES(?,?,?,?,?,?)',
            [(f'prior-{i}', 'text', 'fixture', 'succeeded', '{}', now()) for i in range(189)])
    assert rig.run()['status'] == 'failed'
    evidence = rig.evidence()
    assert evidence['agent']['status'] == 'call_limit'
    assert evidence['agent']['resource_limit'] == {
        'resource': 'api_calls', 'scope': 'daily', 'resumable': True}
    assert progress(evidence)['resumable'] is True
    assert rig.store.calls_for_job(rig.job_id) == []
    assert rig.retrieval_calls == [] and rig.wire.contracts == []
    assert '今日剩余额度' in rig.job()['error']

    # A new UTC day restores daily headroom; the checkpoint and job are reused.
    rig.store.execute("UPDATE calls SET created_at='2000-01-01T00:00:00+00:00'")
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded'
    assert len(rig.store.calls_for_job(rig.job_id)) == 4
    assert rig.evidence()['agent']['resume_count'] == 1
    assert 'local daily call limit' in rig.evidence()['agent']['resume_events'][0]['note']


def test_frozen_job_budget_is_not_resumable_or_reset_by_new_day(agent_case):
    rig = agent_case(count=2, settings_options={'agent_max_calls': 6})
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'call_limit'
    assert rig.evidence()['agent']['resource_limit']['scope'] == 'job'
    assert progress(rig.evidence())['resumable'] is False
    with pytest.raises(ValueError, match='没有可继续'):
        authorize_resume(rig.store, rig.job_id)
    assert rig.store.calls_for_job(rig.job_id) == []


def test_daily_limit_mid_generation_preserves_passed_slot_and_resumes(agent_case):
    rig = agent_case(count=2, wire_options={'review_fail_at': 2},
                     settings_options={'max_daily_calls': 7})
    assert rig.run()['status'] == 'failed'
    before = rig.evidence()
    assert before['agent']['status'] == 'call_limit'
    assert before['agent']['slots'][0]['status'] == 'passed'
    assert progress(before)['resumable'] is True
    assert len(rig.store.calls_for_job(rig.job_id)) == 7
    rig.store.execute("UPDATE calls SET created_at='2000-01-01T00:00:00+00:00'")
    rig.wire.review_fail_at = None
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded'
    assert rig.evidence()['agent']['slots'][0] == before['agent']['slots'][0]
    assert rig.wire.stage_counts[('agent_author', 1)] == 1
    assert len(rig.retrieval_calls) == 1
    assert len(rig.store.calls_for_job(rig.job_id)) == 10


@pytest.mark.parametrize('capability', ['embedding', 'text'])
def test_atomic_reservation_limit_remains_local_after_preflight(agent_case, monkeypatch, capability):
    rig = agent_case(count=1)
    reserve = rig.store.reserve_call
    rejected = False

    def limited(job_id, requested, model, limit):
        nonlocal rejected
        if requested == capability and not rejected:
            rejected = True
            raise LocalCallLimit('Fixture competing task used the daily reservation.', scope='daily')
        return reserve(job_id, requested, model, limit)

    monkeypatch.setattr(rig.store, 'reserve_call', limited)
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'call_limit'
    assert rig.evidence()['agent']['call_count'] == 0
    assert rig.wire.contracts == []
    assert progress(rig.evidence())['resumable'] is True
    if capability == 'text':
        invocation = rig.evidence()['agent']['slots'][0]['attempts'][0]['author']['history'][0]
        assert invocation['status'] == 'call_limit'
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded'
    assert rig.evidence()['agent']['call_count'] == 3
    assert len(rig.store.calls_for_job(rig.job_id)) == 4


@pytest.mark.parametrize('status', [429, 503])
def test_upstream_http_failure_stays_provider_error_with_failed_call(agent_case, status):
    rig = agent_case(count=1)
    asyncio.run(rig.providers.client.aclose())
    rig.providers.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={'error': {'message': 'fixture-secret'}})))
    assert rig.run()['status'] == 'failed'
    evidence = rig.evidence()
    assert evidence['agent']['status'] == 'provider_error'
    assert 'resource_limit' not in evidence['agent']
    calls = rig.store.calls_for_job(rig.job_id)
    assert len(calls) == 2 and calls[-1]['status'] == 'failed'
    assert calls[-1]['http_status'] == status
    assert evidence['agent']['call_count'] == 1
    assert progress(evidence)['resumable'] is True
    assert 'fixture-secret' not in rig.job()['error']


@pytest.mark.parametrize('scope', ['daily', 'job'])
def test_store_limit_has_typed_scope_and_does_not_reserve_extra_call(agent_case, scope):
    rig = agent_case(count=1)
    if scope == 'job':
        rig.store.save_job_evidence(rig.job_id, {
            'schema_version': 'education-agent-v1', 'configuration': {'max_calls': 1}})
    rig.store.reserve_call(rig.job_id, 'text', 'fixture', 100)
    with pytest.raises(LocalCallLimit) as caught:
        rig.store.reserve_call(rig.job_id, 'text', 'fixture', 1 if scope == 'daily' else 100)
    assert caught.value.scope == scope
    assert len(rig.store.calls_for_job(rig.job_id)) == 1


@pytest.mark.parametrize('scope,resumable', [('daily', True), ('job', False), (None, False)])
def test_resume_api_exposes_only_recoverable_local_limits(account_rig, scope, resumable):
    job_id, evidence = seed_failed(account_rig, agent_status='call_limit')
    if scope:
        evidence['agent']['resource_limit'] = {
            'resource': 'api_calls', 'scope': scope, 'resumable': resumable}
        account_rig.store.save_job_evidence(job_id, evidence)
    before_calls = account_rig.store.calls_for_job(job_id)
    assert account_rig.alice.get('/api/jobs/' + job_id).json()['progress']['resumable'] is resumable
    assert account_rig.bob.post('/api/jobs/' + job_id + '/resume').status_code == 404
    result = account_rig.alice.post('/api/jobs/' + job_id + '/resume')
    assert result.status_code == (200 if resumable else 409)
    assert account_rig.store.calls_for_job(job_id) == before_calls
    assert account_rig.enqueued == ([job_id] if resumable else [])
