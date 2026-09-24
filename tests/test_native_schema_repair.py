"""Bounded native JSON-contract recovery with mock HTTP, not model quality claims."""
import copy
import json

import pytest

from app.generation_agent import (GenerationAgent, NATIVE_ROLE_SCHEMA_REPAIR_POLICY,
                                  authorize_resume)
from app.store import dumps
from test_generation_agent import AgentWire, agent_case  # noqa: F401
from test_harness_schema_repair import (INVALID_KEY, INVALID_TEXT, alter_schema,
                                       attempt)  # noqa: F401


OPTIONS = {'agent_runtime': 'native', 'agent_max_repairs': 0}


def q1_wrong_review():
    # Exact key pattern observed in Q1 os_interrupt_deadlock_en. Values/text
    # are fixtures; near-synonyms must never become host-approved assertions.
    return {'answer_correct': True, 'evidence_ok': True, 'mcq_key_ok': True,
            'explanation_ok': True, 'sources_ok': True, 'ambiguity_free': True,
            'numeric_ok': True, 'context_ok': True, 'confidence': 'high',
            'issues': [], 'feedback': INVALID_TEXT}


def test_q1_wrong_reviewer_schema_gets_one_fresh_audit_without_mapping_keys(
        agent_case, monkeypatch):
    original = AgentWire.__call__

    def respond(self, request):
        response = original(self, request)
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        if contract['task'] == 'agent_review' and 'schema_repair' not in contract:
            return self.response(q1_wrong_review())
        return response

    monkeypatch.setattr(AgentWire, '__call__', respond)
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'succeeded', rig.job()
    record = attempt(rig)
    assert record['review']['response'] == q1_wrong_review()
    repair = record['review_schema_repair']
    assert repair['status'] == 'validated' and len(repair['history']) == 1
    diagnostics = repair['validation_diagnostics']
    original_contract = json.loads(record['review']['history'][0]['messages'][1]['content'])
    required = set(original_contract['schema']['required'])
    assert {item['loc'][0] for item in diagnostics if item['type'] == 'missing'} == required - q1_wrong_review().keys()
    assert sum(item['type'] == 'extra_forbidden' for item in diagnostics) == 6
    messages = repair['history'][0]['messages']
    assert INVALID_TEXT not in dumps(messages)
    contract = json.loads(messages[1]['content'])
    assert contract.pop('schema_repair')['mode'] == 'fresh_re_evaluation'
    assert contract == original_contract
    assert 'evidence_ok' not in repair['response']
    assert rig.text_call_count() == 4 and len(rig.contents()) == 1
    assert rig.evidence()['configuration']['role_schema_repair_policy'] == NATIVE_ROLE_SCHEMA_REPAIR_POLICY


@pytest.mark.parametrize('phase', ['solve', 'review'])
@pytest.mark.parametrize('first', ['extra', 'missing'])
def test_native_re_evaluation_keeps_original_output_and_sanitizes_diagnostics(
        agent_case, alter_schema, phase, first):
    alter_schema(phase, first)
    # The native allowance is independent of the Harness-only toggle.
    rig = agent_case(count=1, settings_options=OPTIONS | {'harness_schema_repairs': 0})
    assert rig.run()['status'] == 'succeeded', rig.job()
    record = attempt(rig)
    assert INVALID_TEXT in dumps(record[phase]['response'])
    repair = record[phase + '_schema_repair']
    assert repair['status'] == 'validated' and len(repair['history']) == 1
    assert INVALID_TEXT not in dumps(repair['history'][0]['messages'])
    assert INVALID_KEY not in dumps(repair['history'][0]['messages'])
    assert rig.text_call_count() == 4 and len(rig.contents()) == 1


@pytest.mark.parametrize('phase,issue', [('solve', 'not_answerable'), ('review', 'answer_correct')])
def test_native_repaired_negative_judgment_still_fails_content_gates(
        agent_case, alter_schema, phase, issue):
    alter_schema(phase, repaired='negative')
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    attempts = rig.evidence()['agent']['slots'][0]['attempts']
    assert len(attempts) == 3 and not rig.contents()
    for record in attempts:
        assert record[phase + '_schema_repair']['status'] == 'validated'
        assert record['status'] == 'rejected' and issue in record['feedback']['issues']


@pytest.mark.parametrize('field,issue', [
    ('condition_issues', 'question_conditions_invalid'),
    ('explanation_issues', 'explanation_check_failed'),
])
def test_native_schema_repair_preserves_structured_negative_quality_checks(
        agent_case, alter_schema, monkeypatch, field, issue):
    alter_schema()
    original = AgentWire.__call__

    def respond(self, request):
        response = original(self, request)
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        if contract['task'] == 'agent_review' and 'schema_repair' in contract:
            body = json.loads(response.json()['choices'][0]['message']['content'])
            target = body['question_checks'] if field == 'condition_issues' else body
            target[field] = ['A concrete fixture defect remains after the schema is repaired.']
            return self.response(body)
        return response

    monkeypatch.setattr(AgentWire, '__call__', respond)
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed' and not rig.contents()
    records = rig.evidence()['agent']['slots'][0]['attempts']
    assert len(records) == 3
    for record in records:
        assert record['review_schema_repair']['status'] == 'validated'
        assert issue in record['feedback']['issues']


@pytest.mark.parametrize('phase', ['solve', 'review'])
def test_native_second_invalid_schema_stops_without_another_recheck(
        agent_case, alter_schema, phase):
    alter_schema(phase, repaired='missing')
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    record = attempt(rig)
    assert record[phase + '_schema_repair']['status'] == 'invalid_schema'
    assert len(record[phase + '_schema_repair']['history']) == 1
    assert record['status'] == rig.evidence()['agent']['status'] == 'protocol_failure'
    assert rig.text_call_count() == (3 if phase == 'solve' else 4)
    assert len(rig.evidence()['agent']['slots'][0]['attempts']) == 1 and not rig.contents()


@pytest.mark.parametrize('mode', ['http_failure', 'invalid_json', 'truncated'])
@pytest.mark.parametrize('in_repair', [False, True])
def test_native_transport_or_incomplete_json_never_automatically_replayed(
        agent_case, alter_schema, mode, in_repair):
    alter_schema(first='extra' if in_repair else mode, repaired=mode if in_repair else 'valid')
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    record = attempt(rig)
    assert rig.text_call_count() == (4 if in_repair else 3)
    assert not rig.contents()
    if in_repair:
        repair = record['review_schema_repair']
        assert len(repair['history']) == 1 and 'response' not in repair
        assert repair['status'] == ('provider_error' if mode == 'http_failure' else 'invalid_json')
    else:
        assert 'review_schema_repair' not in record


@pytest.mark.parametrize('limit', ['agent_max_calls', 'max_daily_calls'])
def test_native_repair_cannot_exceed_job_or_daily_budget(agent_case, alter_schema, limit):
    alter_schema()
    rig = agent_case(count=1, settings_options=OPTIONS | {limit: 4})
    assert rig.run()['status'] == 'failed'
    record = attempt(rig)['review_schema_repair']
    assert record['status'] == 'blocked' and record['history'] == []
    assert rig.text_call_count() == 3 and not rig.contents()
    assert rig.store.one('SELECT count(*) AS n FROM calls')['n'] == 4


@pytest.mark.parametrize('boundary', ['cancelled', 'scope_changed', 'context_limit'])
def test_native_repair_rechecks_cancellation_source_scope_and_context(
        agent_case, alter_schema, monkeypatch, boundary):
    alter_schema()
    original_call = GenerationAgent.call

    async def intervene(self, attempt, phase, contract, system):
        if phase == 'review_schema_repair':
            if boundary == 'cancelled':
                self.store.request_cancel(self.job_id)
            elif boundary == 'context_limit':
                self.settings.agent_context_chars = 1
            else:
                def denied():
                    raise ValueError('Fixture source revoked before schema repair.')
                self.check_scope = denied
        return await original_call(self, attempt, phase, contract, system)

    monkeypatch.setattr(GenerationAgent, 'call', intervene)
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == ('cancelled' if boundary == 'cancelled' else 'failed')
    repair = attempt(rig)['review_schema_repair']
    assert repair['status'] == 'blocked' and repair['blocked_by'] == boundary
    assert repair['history'] == [] and 'response' not in repair
    assert rig.text_call_count() == 3 and not rig.contents()


def test_native_resume_reuses_validated_solver_schema_repair(agent_case, alter_schema):
    alter_schema('solve')
    rig = agent_case(count=1, settings_options=OPTIONS,
                     wire_options={'fail_phase': ('agent_review', 1)})
    assert rig.run()['status'] == 'failed'
    before = copy.deepcopy(attempt(rig))
    assert before['solve_schema_repair']['status'] == 'validated'
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded'
    assert attempt(rig)['solve_schema_repair'] == before['solve_schema_repair']
    assert rig.text_call_count() == 5


def test_native_only_explicit_resume_can_retry_interrupted_schema_repair(agent_case, alter_schema):
    alter_schema(repaired='http_failure')
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    before = copy.deepcopy(attempt(rig))
    assert rig.text_call_count() == 4
    alter_schema(repaired='valid')
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded'
    after = attempt(rig)
    assert after['review'] == before['review']
    assert after['review_schema_repair']['history'][0] == before['review_schema_repair']['history'][0]
    assert [h['resume_count'] for h in after['review_schema_repair']['history']] == [0, 1]
    assert rig.text_call_count() == 5


def test_native_legacy_checkpoint_retains_no_schema_repair_behavior(agent_case, alter_schema):
    alter_schema()
    rig = agent_case(count=1, settings_options=OPTIONS,
                     wire_options={'fail_phase': ('agent_review', 1)})
    assert rig.run()['status'] == 'failed'
    evidence = rig.evidence()
    evidence['configuration'].pop('role_schema_repair_policy')
    rig.store.save_job_evidence(rig.job_id, evidence)
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'protocol_failure'
    assert 'role_schema_repair_policy' not in rig.evidence()['configuration']
    assert 'review_schema_repair' not in attempt(rig)
    assert rig.text_call_count() == 4
