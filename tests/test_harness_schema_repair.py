"""Bounded role re-evaluation with fixture outputs, not live-model accuracy."""
import copy
import json

import httpx
import pytest

from app.generation_agent import GenerationAgent, authorize_resume
from app.store import dumps
from test_generation_agent import AgentWire, agent_case  # noqa: F401
from test_harness_quality_gates import offline_harness  # noqa: F401


INVALID_TEXT = 'INVALID_MODEL_ANSWER_MUST_NOT_ENTER_REPAIR_8137'
INVALID_KEY = 'UNTRUSTED_FIELD_NAME_MUST_NOT_ENTER_REPAIR_5201'
OPTIONS = {'agent_runtime': 'deepseek_harness', 'agent_max_repairs': 0,
           'harness_schema_repairs': 1}


def attempt(rig):
    return rig.evidence()['agent']['slots'][0]['attempts'][0]


@pytest.fixture
def alter_schema(monkeypatch):
    original = AgentWire.__call__

    def install(phase='review', first='extra', repaired='valid'):
        task = 'agent_' + phase

        def respond(self, request):
            response = original(self, request)
            contract = json.loads(json.loads(request.content)['messages'][1]['content'])
            if response.status_code != 200 or contract['task'] != task:
                return response
            mode = repaired if 'schema_repair' in contract else first
            if mode == 'http_failure':
                return httpx.Response(429, text='fixture response must not be retried automatically')
            if mode in ('invalid_json', 'truncated'):
                return httpx.Response(200, json={'choices': [{
                    'finish_reason': 'length' if mode == 'truncated' else 'stop',
                    'message': {'content': '{"incomplete":'}}]})
            body = json.loads(response.json()['choices'][0]['message']['content'])
            if mode == 'extra':
                body['feedback_note'] = INVALID_TEXT
                body[INVALID_KEY] = INVALID_TEXT
                body['answer' if phase == 'solve' else 'feedback'] = INVALID_TEXT
            elif mode == 'missing':
                del body['answerable' if phase == 'solve' else 'answer_correct']
                body['explanation' if phase == 'solve' else 'feedback'] = INVALID_TEXT
            elif mode == 'negative':
                body['answerable' if phase == 'solve' else 'answer_correct'] = False
            elif mode == 'difficulty_mismatch':
                body['assessed_difficulty'] = 'medium'
            elif mode != 'valid':
                raise AssertionError(mode)
            return self.response(body)

        monkeypatch.setattr(AgentWire, '__call__', respond)

    return install


@pytest.mark.parametrize('phase', ['solve', 'review'])
@pytest.mark.parametrize('first', ['extra', 'missing'])
def test_one_schema_recheck_keeps_original_evidence_and_re_evaluates_original_task(
        agent_case, offline_harness, alter_schema, phase, first):
    alter_schema(phase, first)
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'succeeded', rig.job()
    record = attempt(rig)
    assert INVALID_TEXT in dumps(record[phase]['response'])
    assert record[phase]['history'][0]['status'] == 'completed'
    repair = record[phase + '_schema_repair']
    assert repair['status'] == 'validated'
    assert repair['original_phase'] == phase
    assert len(repair['history']) == 1
    assert repair['history'][0]['status'] == 'completed'
    assert INVALID_TEXT not in dumps(repair['response'])
    first_messages = record[phase]['history'][0]['messages']
    repair_messages = repair['history'][0]['messages']
    original_contract = json.loads(first_messages[1]['content'])
    repair_contract = json.loads(repair_messages[1]['content'])
    assert first_messages[0] == repair_messages[0]
    reminder = repair_contract.pop('schema_repair')
    assert repair_contract == original_contract
    assert reminder['mode'] == 'fresh_re_evaluation'
    assert set(reminder['allowed_top_level_fields']) == set(original_contract['schema']['properties'])
    assert set(reminder['required_top_level_fields']) == set(original_contract['schema']['required'])
    assert INVALID_TEXT not in dumps(repair_messages)
    assert INVALID_KEY not in dumps(repair_messages)
    assert all(set(item) == {'loc', 'type'} for item in reminder['validation_diagnostics'])
    if first == 'extra':
        assert reminder['validation_diagnostics'] == [
            {'loc': ['unknown_field'], 'type': 'extra_forbidden'},
            {'loc': ['unknown_field'], 'type': 'extra_forbidden'}]
    assert rig.evidence()['configuration']['harness']['schema_repairs'] == 1
    expected = ['author', 'solve', 'review']
    expected.insert(expected.index(phase) + 1, phase + '_schema_repair')
    assert offline_harness == expected
    assert rig.text_call_count() == 4 and len(rig.contents()) == 1


@pytest.mark.parametrize('phase,issue', [('solve', 'not_answerable'), ('review', 'answer_correct')])
def test_repaired_valid_schema_with_negative_judgment_is_still_rejected(
        agent_case, offline_harness, alter_schema, phase, issue):
    alter_schema(phase, repaired='negative')
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    record = attempt(rig)
    assert record[phase + '_schema_repair']['status'] == 'validated'
    assert record['status'] == 'rejected'
    assert issue in record['feedback']['issues']
    assert rig.evidence()['agent']['status'] == 'quality_failed'
    assert not rig.contents()
    assert offline_harness.count(phase + '_schema_repair') == 3
    assert len(rig.evidence()['agent']['slots'][0]['attempts']) == 3


def test_valid_repaired_solver_difficulty_mismatch_keeps_answer_review_and_discloses_adjustment(
        agent_case, offline_harness, alter_schema):
    alter_schema('solve', repaired='difficulty_mismatch')
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'succeeded'
    record = attempt(rig)
    assert record['quality_checks']['correctness']['status'] == 'passed'
    assert record['quality_checks']['difficulty']['status'] == 'mismatch'
    assert record['feedback']['issues'] == ['difficulty_mismatch']
    assert offline_harness == ['author', 'solve', 'solve_schema_repair', 'review'] * 3
    assert rig.contents()
    assert rig.evidence()['difficulty_acceptance']['status'] == 'adjusted'


@pytest.mark.parametrize('phase', ['solve', 'review'])
def test_second_schema_failure_stops_after_exactly_one_recheck(
        agent_case, offline_harness, alter_schema, phase):
    alter_schema(phase, repaired='missing')
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    record = attempt(rig)
    repair = record[phase + '_schema_repair']
    assert repair['status'] == 'invalid_schema'
    assert repair['repair_validation_diagnostics'][0]['type'] == 'missing'
    assert len(repair['history']) == 1 and 'response' in repair
    assert record['status'] == rig.evidence()['agent']['status'] == 'protocol_failure'
    assert offline_harness.count(phase + '_schema_repair') == 1
    assert len(rig.evidence()['agent']['slots'][0]['attempts']) == 1
    assert rig.text_call_count() == (3 if phase == 'solve' else 4)
    assert not rig.contents()


@pytest.mark.parametrize('phase', ['solve', 'review'])
@pytest.mark.parametrize('mode', ['http_failure', 'invalid_json', 'truncated'])
def test_transport_json_and_truncation_failures_never_start_schema_repair(
        agent_case, offline_harness, alter_schema, phase, mode):
    alter_schema(phase, first=mode)
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    record = attempt(rig)
    assert phase + '_schema_repair' not in record
    assert offline_harness == (['author', 'solve'] if phase == 'solve' else ['author', 'solve', 'review'])
    assert rig.text_call_count() == len(offline_harness)
    assert rig.evidence()['agent']['status'] == ('provider_error' if mode == 'http_failure' else 'protocol_failure')
    assert not rig.contents()


@pytest.mark.parametrize('mode', ['http_failure', 'invalid_json', 'truncated'])
def test_failed_schema_repair_call_is_not_retried_automatically(
        agent_case, offline_harness, alter_schema, mode):
    alter_schema(repaired=mode)
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    record = attempt(rig)['review_schema_repair']
    assert record['status'] == ('provider_error' if mode == 'http_failure' else 'invalid_json')
    assert 'response' not in record
    assert len(record['history']) == 1 and rig.text_call_count() == 4
    assert offline_harness == ['author', 'solve', 'review', 'review_schema_repair']
    assert not rig.contents()


@pytest.mark.parametrize('phase,mode', [('solve', 'negative'), ('solve', 'difficulty_mismatch'),
                                      ('review', 'negative')])
def test_valid_quality_failures_never_trigger_schema_repair(
        agent_case, offline_harness, alter_schema, phase, mode):
    alter_schema(phase, first=mode)
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == ('succeeded' if mode == 'difficulty_mismatch' else 'failed')
    assert not any(name.endswith('_schema_repair') for name in offline_harness)
    if mode == 'difficulty_mismatch':
        assert rig.evidence()['difficulty_acceptance']['status'] == 'adjusted'
        assert len(rig.evidence()['agent']['slots'][0]['attempts']) == 3
    else:
        assert rig.evidence()['agent']['status'] == 'quality_failed'
        assert not rig.contents()


@pytest.mark.parametrize('settings', [
    {'agent_runtime': 'deepseek_harness', 'harness_schema_repairs': 0},
])
def test_disabled_harness_behavior_remains_single_call_protocol_failure(
        agent_case, offline_harness, alter_schema, settings):
    alter_schema()
    rig = agent_case(count=1, settings_options=OPTIONS | settings)
    assert rig.run()['status'] == 'failed'
    assert 'review_schema_repair' not in attempt(rig)
    assert rig.text_call_count() == 3
    assert rig.evidence()['agent']['status'] == 'protocol_failure'
    assert not rig.contents()


@pytest.mark.parametrize('limit,status', [('agent_max_calls', 'call_limit'),
                                       ('max_daily_calls', 'call_limit')])
def test_schema_repair_obeys_existing_budget_without_an_extra_call(
        agent_case, offline_harness, alter_schema, limit, status):
    alter_schema()
    rig = agent_case(count=1, settings_options=OPTIONS | {limit: 4})
    assert rig.run()['status'] == 'failed'
    repair = attempt(rig)['review_schema_repair']
    assert repair['status'] == 'blocked' and repair['blocked_by'] == status
    assert repair['history'] == []
    assert rig.text_call_count() == 3
    assert rig.store.one('SELECT count(*) AS n FROM calls')['n'] == 4  # Includes retrieval.
    assert rig.evidence()['agent']['status'] == status
    assert offline_harness == ['author', 'solve', 'review'] and not rig.contents()


def test_schema_repair_obeys_context_limit_before_network(
        agent_case, offline_harness, alter_schema, monkeypatch):
    alter_schema()
    original_call = GenerationAgent.call

    async def lower_context_for_repair(self, attempt, phase, contract, system):
        if phase == 'review_schema_repair':
            self.settings.agent_context_chars = 100
        return await original_call(self, attempt, phase, contract, system)

    monkeypatch.setattr(GenerationAgent, 'call', lower_context_for_repair)
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    repair = attempt(rig)['review_schema_repair']
    assert repair['status'] == 'blocked' and repair['blocked_by'] == 'context_limit'
    assert repair['history'] == [] and rig.text_call_count() == 3
    assert offline_harness == ['author', 'solve', 'review'] and not rig.contents()


def test_resume_reuses_validated_schema_repair_and_only_retries_interrupted_review(
        agent_case, offline_harness, alter_schema):
    alter_schema('solve')
    rig = agent_case(count=1, settings_options=OPTIONS,
                     wire_options={'fail_phase': ('agent_review', 1)})
    assert rig.run()['status'] == 'failed'
    before = copy.deepcopy(attempt(rig))
    assert before['solve_schema_repair']['status'] == 'validated'
    assert rig.evidence()['agent']['status'] == 'provider_error'
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    after = attempt(rig)
    assert after['solve'] == before['solve']
    assert after['solve_schema_repair'] == before['solve_schema_repair']
    assert offline_harness == ['author', 'solve', 'solve_schema_repair', 'review', 'review']
    assert rig.text_call_count() == 5 and len(rig.contents()) == 1


def test_changed_schema_repair_policy_blocks_checkpoint_resume_without_new_calls(
        agent_case, offline_harness, alter_schema):
    alter_schema('solve')
    rig = agent_case(count=1, settings_options=OPTIONS,
                     wire_options={'fail_phase': ('agent_review', 1)})
    assert rig.run()['status'] == 'failed'
    authorize_resume(rig.store, rig.job_id)
    evidence = copy.deepcopy(rig.evidence())
    rig.settings.harness_schema_repairs = 0
    assert rig.run()['status'] == 'failed'
    assert '配置已改变' in rig.job()['error']
    assert rig.evidence() == evidence and rig.text_call_count() == 4


@pytest.mark.parametrize('tool_input,issue', [
    (None, 'calculation_without_supported_tool'),
    ({'policy': 'stcf', 'processes': []}, 'invalid_tool_input'),
])
def test_schema_valid_repaired_solver_still_requires_supported_calculations(
        agent_case, offline_harness, alter_schema, tool_input, issue):
    alter_schema('solve')
    rig = agent_case(count=1, settings_options=OPTIONS,
                     wire_options={'requires_calculation': True, 'tool_input': tool_input})
    assert rig.run()['status'] == 'failed'
    record = attempt(rig)
    assert record['solve_schema_repair']['status'] == 'validated'
    assert issue in record['feedback']['issues']
    assert record['quality_checks']['correctness']['status'] == 'not_evaluated'
    assert offline_harness == ['author', 'solve', 'solve_schema_repair'] * 3
    assert rig.text_call_count() == 9 and not rig.contents()


def test_scope_change_before_schema_repair_blocks_the_extra_call(
        agent_case, offline_harness, alter_schema, monkeypatch):
    alter_schema()
    original_call = GenerationAgent.call

    async def revoke_scope_before_repair(self, attempt, phase, contract, system):
        if phase == 'review_schema_repair':
            def denied():
                raise ValueError('Fixture source was disabled before repair.')
            self.check_scope = denied
        return await original_call(self, attempt, phase, contract, system)

    monkeypatch.setattr(GenerationAgent, 'call', revoke_scope_before_repair)
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    record = attempt(rig)
    repair = record['review_schema_repair']
    assert repair['status'] == 'blocked' and repair['blocked_by'] == 'scope_changed'
    assert repair['history'] == [] and 'response' not in repair
    assert INVALID_TEXT in dumps(record['review']['response'])
    assert rig.evidence()['agent']['status'] == 'scope_changed'
    assert offline_harness == ['author', 'solve', 'review']
    assert rig.text_call_count() == 3 and not rig.contents()


def test_explicit_resume_retries_only_interrupted_schema_repair_and_keeps_both_call_records(
        agent_case, offline_harness, alter_schema):
    alter_schema(repaired='http_failure')
    rig = agent_case(count=1, settings_options=OPTIONS)
    assert rig.run()['status'] == 'failed'
    before = copy.deepcopy(attempt(rig))
    assert before['review_schema_repair']['status'] == 'provider_error'
    assert len(before['review_schema_repair']['history']) == 1
    assert rig.text_call_count() == 4
    alter_schema(repaired='valid')
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded'
    after = attempt(rig)
    assert after['review'] == before['review']
    assert after['solve'] == before['solve']
    repair = after['review_schema_repair']
    assert repair['status'] == 'validated'
    assert repair['history'][0] == before['review_schema_repair']['history'][0]
    assert [entry['status'] for entry in repair['history']] == ['provider_error', 'completed']
    assert [entry['resume_count'] for entry in repair['history']] == [0, 1]
    assert offline_harness == ['author', 'solve', 'review', 'review_schema_repair', 'review_schema_repair']
    assert rig.text_call_count() == 5 and len(rig.contents()) == 1
