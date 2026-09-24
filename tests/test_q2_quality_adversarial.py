"""Adversarial controller checks with preset HTTP responses, never live APIs.

Passing these proves rejection paths and checkpoint compatibility, not model
ability to identify the concrete semantic mistakes in real course questions.
"""
import json

import pytest

from app.generation_agent import authorize_resume
from app.store import dumps
from test_generation_agent import AgentWire, agent_case
from test_flexible_agent import flexible_case, flexible_wire
from test_harness_quality_gates import offline_harness
from test_cpu_spec_agent import structured_wire, rig_for


@pytest.fixture
def mutate_wire(monkeypatch):
    original = AgentWire.__call__

    def install(mutation):
        def respond(self, request):
            response = original(self, request)
            contract = json.loads(json.loads(request.content)['messages'][1]['content'])
            if response.status_code != 200:
                return response
            value = json.loads(response.json()['choices'][0]['message']['content'])
            mutation(value, contract)
            return self.response(value)
        monkeypatch.setattr(AgentWire, '__call__', respond)
    return install


@pytest.mark.parametrize('negative', [
    'answer_correct', 'explanation_correct', 'source_supported',
    'ambiguity_free', 'calculations_verified', 'distinct_from_previous',
])
def test_no_tools_does_not_waive_any_other_negative_gate(agent_case, negative):
    rig = agent_case(count=1, wire_options={'review_override': {
        'tool_inputs_match_question': False, negative: False}})
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    for attempt in rig.evidence()['agent']['slots'][0]['attempts']:
        assert negative in attempt['feedback']['issues']
        assert 'tool_inputs_match_question' not in attempt['feedback']['issues']
        assert 'difficulty_candidate' not in attempt


def test_false_tool_match_is_not_waived_when_a_tool_was_requested(agent_case):
    tool = {'policy': 'stcf', 'processes': [{'id': 'A', 'arrival': 0, 'burst': 1}], 'switch_cost': 0}
    rig = agent_case(count=1, wire_options={'requires_calculation': True, 'tool_input': tool,
        'review_override': {'tool_inputs_match_question': False}})
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    for attempt in rig.evidence()['agent']['slots'][0]['attempts']:
        assert 'tool_inputs_match_question' in attempt['feedback']['issues']
        assert 'difficulty_candidate' not in attempt


def test_difficulty_relaxation_cannot_keep_a_failed_content_candidate(agent_case, mutate_wire):
    def mutate(value, contract):
        if contract['task'] == 'agent_solve':
            value['assessed_difficulty'] = 'easy'
        if contract['task'] == 'agent_review':
            value['explanation_issues'] = ['The explanation reverses the stated operation order.']
    mutate_wire(mutate)
    rig = agent_case(count=1)
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    for attempt in rig.evidence()['agent']['slots'][0]['attempts']:
        assert 'explanation_check_failed' in attempt['feedback']['issues']
        assert 'difficulty_candidate' not in attempt


@pytest.mark.parametrize('field', ['condition_issues', 'explanation_issues'])
def test_flexible_invalid_quote_does_not_retry_a_valid_new_negative_verdict(
        agent_case, offline_harness, flexible_wire, field):
    def invalid_quote_and_negative(value, contract, count):
        value['requirement_checks'][0]['answer_evidence'] = 'This quote does not occur in the candidate.'
        if field == 'condition_issues':
            value['question_checks']['condition_issues'] = ['The required initial state is absent.']
        else:
            value['explanation_issues'] = ['The explanation contradicts the initial state.']
    flexible_wire['review_mutation'] = invalid_quote_and_negative
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'failed'
    assert 'review_evidence_repair' not in offline_harness
    assert not rig.contents()


def test_flexible_evidence_repair_preserves_new_contract_and_no_tool_applicability(
        agent_case, offline_harness, flexible_wire):
    def invalid_quote_once(value, contract, count):
        value['tool_inputs_match_question'] = False
        if count == 1:
            value['requirement_checks'][0]['answer_evidence'] = 'This quote does not occur in the candidate.'
    flexible_wire['review_mutation'] = invalid_quote_once
    rig = flexible_case(agent_case, count=1, settings_options={'harness_schema_repairs': 1})
    assert rig.run()['status'] == 'succeeded', rig.job()
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    assert attempt['review_evidence_repair']['status'] == 'validated'
    assert attempt['review_question_checks'] == {'condition_issues': [], 'option_checks': []}
    assert attempt['tool_check_applicability']['inputs_match_status'] == 'not_applicable'


def test_mcq_solver_answer_cannot_contradict_its_own_option_verdict(agent_case, mutate_wire):
    def contradiction(value, contract):
        if contract['task'] == 'agent_author':
            value['questions'][0].update(kind='mcq', options=['Supported answer', 'False answer',
                'Another false answer', 'Fourth false answer'], answer='A')
        if contract['task'] == 'agent_solve':
            value['answer'] = 'B'
    mutate_wire(contradiction)
    rig = agent_case(count=1)
    payload = json.loads(rig.job()['payload'])
    payload['question_type'] = 'mcq'
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()


@pytest.mark.parametrize('review_problem', [None, 'uncertain', 'two_correct', 'wrong_key'])
def test_tool_first_mcq_defers_solver_verdicts_but_never_final_review(
        agent_case, mutate_wire, review_problem):
    def mutate(value, contract):
        if contract['task'] == 'agent_author':
            value['questions'][0].update(kind='mcq', options=['Supported answer', 'False answer',
                'Another false answer', 'Fourth false answer'], answer='A')
        if contract['task'] == 'agent_solve':
            value['answer'] = 'Pending deterministic tool results'
            for option in value['question_checks']['option_checks']:
                option['verdict'] = 'uncertain'
        if contract['task'] == 'agent_review' and review_problem:
            checks = value['question_checks']['option_checks']
            if review_problem == 'uncertain':
                checks[1]['verdict'] = 'uncertain'
            if review_problem == 'two_correct':
                checks[1]['verdict'] = 'correct'
            if review_problem == 'wrong_key':
                checks[0]['verdict'] = 'incorrect'
                checks[1]['verdict'] = 'correct'
    mutate_wire(mutate)
    rig = agent_case(count=1, wire_options={'requires_calculation': True, 'tool_input': {
        'policy': 'stcf', 'processes': [{'id': 'A', 'arrival': 0, 'burst': 1}], 'switch_cost': 0}})
    payload = json.loads(rig.job()['payload'])
    payload['question_type'] = 'mcq'
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    assert rig.run()['status'] == ('succeeded' if review_problem is None else 'failed')
    attempts = rig.evidence()['agent']['slots'][0]['attempts']
    assert all('review' in attempt for attempt in attempts)
    assert bool(rig.contents()) is (review_problem is None)


@pytest.mark.parametrize('problem', ['conditions', 'explanation', 'source_supported', 'tool_inputs_match_question'])
def test_compiled_cpu_facts_cannot_override_a_negative_new_quality_check(
        agent_case, structured_wire, monkeypatch, problem):
    original = AgentWire.__call__
    def respond(self, request):
        response = original(self, request)
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        value = json.loads(response.json()['choices'][0]['message']['content'])
        for key, field in self.quality_fields(contract).items():
            value.setdefault(key, field)
        if contract['task'] == 'agent_review':
            if problem == 'conditions':
                value['question_checks']['condition_issues'] = ['The task assumes an unstated convention.']
            elif problem == 'explanation':
                value['explanation_issues'] = ['The prose contradicts the computed facts.']
            else:
                value[problem] = False
        return self.response(value)
    monkeypatch.setattr(AgentWire, '__call__', respond)
    rig = rig_for(agent_case)
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    for attempt in rig.evidence()['agent']['slots'][0]['attempts']:
        assert 'review' in attempt and 'difficulty_candidate' not in attempt


def test_legacy_checkpoint_does_not_gain_the_tool_na_exception(agent_case):
    rig = agent_case(count=1, wire_options={'fail_phase': ('agent_author', 1),
        'review_override': {'tool_inputs_match_question': False}})
    assert rig.run()['status'] == 'failed'
    legacy = rig.evidence()
    legacy['configuration'].pop('review_policy_revision')
    legacy['configuration']['agent_prompt_revision'] = '20260919-tool-first-v3'
    legacy['configuration'].pop('role_schema_repair_policy', None)
    rig.store.save_job_evidence(rig.job_id, legacy)
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'failed'
    final = rig.evidence()
    assert 'review_policy_revision' not in final['configuration']
    assert 'role_schema_repair_policy' not in final['configuration']
    assert final['agent']['status'] == 'quality_failed'
    assert not rig.contents()
