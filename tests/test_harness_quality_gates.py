"""Offline H4a orchestration checks, not measurements of model judgment quality.

The Harness phase boundary forwards into AgentWire's isolated HTTP transport.
No Harness process, model API, or real credentials are used. Preset judgments
verify gate order, repair feedback, checkpoint isolation and durable evidence.
"""
import copy
import json

import pytest

import app.generation_agent as generation_agent
import app.harness_runner as harness_runner
from app.generation_agent import authorize_resume
from app.store import dumps
from test_generation_agent import AgentWire, agent_case


SOLVER_ANSWER = 'UNREVIEWED_SOLVER_ANSWER_731'
SOLVER_EXPLANATION = 'UNREVIEWED_SOLVER_EXPLANATION_947'
TOOL_PROCESS = 'PRIVATE_TOOL_PROCESS_823'
TOOL_INPUT = {
    'policy': 'rr',
    'processes': [
        {'id': TOOL_PROCESS, 'arrival': 0, 'burst': 2},
        {'id': 'B', 'arrival': 0, 'burst': 1},
    ],
    'quantum': 2, 'switch_cost': 0, 'boundary': 'before',
    'during_switch': 'tail', 'early_finish_free': False,
}


@pytest.fixture
def offline_harness(monkeypatch):
    phases = []

    async def phase(agent, messages, phase, report):
        phases.append(phase)
        report['fixture'] = 'orchestration-only-no-harness-process'
        return await agent.pipeline.providers.generate(messages, agent.job_id)

    monkeypatch.setattr(harness_runner, 'verify_runtime', lambda settings: None)
    monkeypatch.setattr(harness_runner, 'run_harness_phase', phase)
    return phases


def override_solver(monkeypatch, **overrides):
    original = AgentWire.__call__

    def respond(self, request):
        response = original(self, request)
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        if response.status_code == 200 and contract['task'] == 'agent_solve':
            body = json.loads(response.json()['choices'][0]['message']['content'])
            return self.response(body | copy.deepcopy(overrides))
        return response

    monkeypatch.setattr(AgentWire, '__call__', respond)


def attempts(rig):
    return rig.evidence()['agent']['slots'][0]['attempts']


def test_harness_reviews_answer_even_when_difficulty_is_mismatched(
        agent_case, monkeypatch, offline_harness):
    override_solver(monkeypatch, assessed_difficulty='medium')
    rig = agent_case(count=1, settings_options={
        'agent_runtime': 'deepseek_harness', 'agent_max_repairs': 0})
    assert rig.run()['status'] == 'succeeded'
    assert offline_harness == ['author', 'solve', 'review'] * 3
    attempt = attempts(rig)[0]
    assert attempt['quality_checks']['difficulty'] == {
        'status': 'mismatch', 'target': 'hard', 'assessed': 'medium'}
    assert attempt['quality_checks']['correctness'] == {'status': 'passed', 'issues': []}
    assert attempt['feedback']['issues'] == ['difficulty_mismatch']
    assert 'review' in attempt and rig.contents()
    assert rig.evidence()['difficulty_acceptance']['strict_passed'] is False
    assert rig.text_call_count() == 9


def test_harness_records_answer_failure_and_difficulty_failure_separately(
        agent_case, monkeypatch, offline_harness):
    override_solver(monkeypatch, assessed_difficulty='medium')
    rig = agent_case(count=1, wire_options={'review_override': {'answer_correct': False}},
                     settings_options={'agent_runtime': 'deepseek_harness', 'agent_max_repairs': 0})
    assert rig.run()['status'] == 'failed'
    attempt = attempts(rig)[0]
    assert attempt['quality_checks']['difficulty']['status'] == 'mismatch'
    assert attempt['quality_checks']['correctness'] == {
        'status': 'failed', 'issues': ['answer_correct']}
    assert set(attempt['feedback']['issues']) == {'answer_correct', 'difficulty_mismatch'}
    assert offline_harness == ['author', 'solve', 'review'] * 3
    assert not rig.contents()


def test_difficulty_only_repair_does_not_copy_solver_text_or_tool_results(
        agent_case, monkeypatch, offline_harness):
    override_solver(monkeypatch, assessed_difficulty='medium',
                    answer=SOLVER_ANSWER, explanation=SOLVER_EXPLANATION)
    rig = agent_case(count=1, wire_options={
        'requires_calculation': True, 'tool_input': TOOL_INPUT},
        settings_options={'agent_runtime': 'deepseek_harness'})
    assert rig.run()['status'] == 'succeeded'
    assert offline_harness == ['author', 'solve', 'review'] * 3
    previous = attempts(rig)[0]
    repair = next(c['repair_feedback'] for c in rig.wire.contracts if 'repair_feedback' in c)
    assert repair == previous['feedback']
    assert set(repair) == {'issues', 'difficulty', 'repair_action'}
    for secret in (SOLVER_ANSWER, SOLVER_EXPLANATION, TOOL_PROCESS):
        assert secret not in dumps(repair)
    assert 'tool_results' not in repair and 'solver' not in repair and 'review' not in repair
    # Full responses and exact arithmetic remain in the audit record.
    assert previous['solve']['response']['answer'] == SOLVER_ANSWER
    assert previous['tool_results'][0]['request']['input'] == TOOL_INPUT
    assert previous['quality_checks']['correctness']['status'] == 'passed'
    assert rig.text_call_count() == 9 and rig.contents()
    assert rig.evidence()['difficulty_acceptance']['status'] == 'adjusted'


@pytest.mark.parametrize('solver_overrides,wire_options,issue', [
    ({'answerable': False}, {}, 'not_answerable'),
    ({'ambiguity_free': False}, {}, 'ambiguous_question'),
    ({'confidence': 'low'}, {}, 'solver_low_confidence'),
    ({}, {'requires_calculation': True}, 'calculation_without_supported_tool'),
    ({}, {'requires_calculation': True, 'tool_input': {'policy': 'arbitrary-code'}}, 'invalid_tool_input'),
])
def test_harness_pre_review_blockers_remain_unassessed_without_unverified_feedback(
        agent_case, monkeypatch, offline_harness, solver_overrides, wire_options, issue):
    override_solver(monkeypatch, assessed_difficulty='medium', answer=SOLVER_ANSWER,
                    explanation=SOLVER_EXPLANATION, **solver_overrides)
    rig = agent_case(count=1, wire_options=wire_options,
                     settings_options={'agent_runtime': 'deepseek_harness'})
    assert rig.run()['status'] == 'failed'
    assert offline_harness == ['author', 'solve'] * 3
    for attempt in attempts(rig):
        assert 'review' not in attempt
        assert attempt['quality_checks']['correctness'] == {
            'status': 'not_evaluated', 'issues': [issue]}
        assert attempt['quality_checks']['difficulty']['status'] == 'mismatch'
        assert set(attempt['feedback']['issues']) == {issue, 'difficulty_mismatch'}
    repair = next(c['repair_feedback'] for c in rig.wire.contracts if 'repair_feedback' in c)
    assert 'solver' not in repair and 'tool_results' not in repair
    assert SOLVER_ANSWER not in dumps(repair) and SOLVER_EXPLANATION not in dumps(repair)
    assert rig.text_call_count() == 6 and not rig.contents()


def test_new_native_tasks_review_correctness_before_difficulty_calibration(
        agent_case, monkeypatch, offline_harness):
    override_solver(monkeypatch, assessed_difficulty='medium', answer=SOLVER_ANSWER)
    rig = agent_case(count=1, settings_options={'agent_runtime': 'native', 'agent_max_repairs': 0})
    assert rig.run()['status'] == 'succeeded'
    attempt = attempts(rig)[0]
    assert [c['task'] for c in rig.wire.contracts] == ['agent_author', 'agent_solve', 'agent_review'] * 3
    assert 'review' in attempt and 'quality_checks' in attempt
    assert attempt['feedback']['issues'] == ['difficulty_mismatch']
    assert SOLVER_ANSWER not in dumps(attempt['feedback'])
    assert rig.evidence()['difficulty_acceptance']['status'] == 'adjusted'
    assert 'harness' not in rig.evidence()['configuration']
    assert offline_harness == [] and rig.text_call_count() == 9


def test_harness_publishes_only_when_correctness_and_difficulty_both_pass(
        agent_case, offline_harness):
    rig = agent_case(count=1, settings_options={'agent_runtime': 'deepseek_harness'})
    assert rig.run()['status'] == 'succeeded'
    attempt = attempts(rig)[0]
    assert attempt['status'] == 'passed'
    assert attempt['quality_checks']['difficulty'] == {
        'status': 'matched', 'target': 'hard', 'assessed': 'hard'}
    assert attempt['quality_checks']['correctness'] == {'status': 'passed', 'issues': []}
    assert len(rig.contents()) == 1 and rig.text_call_count() == 3


def test_invalid_review_does_not_become_a_correctness_failure_label(
        agent_case, monkeypatch, offline_harness):
    override_solver(monkeypatch, assessed_difficulty='medium')
    rig = agent_case(count=1, wire_options={'malformed_phase': 'agent_review'},
                     settings_options={'agent_runtime': 'deepseek_harness'})
    assert rig.run()['status'] == 'failed'
    attempt = attempts(rig)[0]
    assert attempt['quality_checks']['correctness'] == {
        'status': 'not_evaluated', 'issues': ['invalid_review_output']}
    assert rig.evidence()['agent']['status'] == 'protocol_failure'
    assert rig.text_call_count() == 3 and rig.wire.author_counts == {1: 1}
    assert not rig.contents()


@pytest.mark.parametrize('revision_change', ['new_revision', 'missing_saved_revision'])
def test_resume_rejects_changed_quality_revision_without_new_calls_or_evidence_rewrite(
        agent_case, monkeypatch, offline_harness, revision_change):
    rig = agent_case(count=2, wire_options={'fail_phase': ('agent_review', 2)},
                     settings_options={'agent_runtime': 'deepseek_harness'})
    assert rig.run()['status'] == 'failed'
    evidence = rig.evidence()
    assert evidence['agent']['slots'][0]['status'] == 'passed'
    assert evidence['configuration']['harness']['quality_revision'] == generation_agent.HARNESS_QUALITY_REVISION
    if revision_change == 'new_revision':
        monkeypatch.setattr(generation_agent, 'HARNESS_QUALITY_REVISION', 'fixture-future-quality-revision')
    else:
        del evidence['configuration']['harness']['quality_revision']
        rig.store.save_job_evidence(rig.job_id, evidence)
    authorize_resume(rig.store, rig.job_id)
    before = rig.evidence()
    call_count = rig.text_call_count()
    phases_before = offline_harness.copy()
    assert rig.run()['status'] == 'failed'
    assert '配置已改变' in rig.job()['error']
    assert rig.evidence() == before
    assert rig.text_call_count() == call_count and offline_harness == phases_before
    assert len(rig.retrieval_calls) == 1 and not rig.contents()


def test_same_quality_revision_resume_reuses_completed_review_and_passed_slot(
        agent_case, offline_harness):
    rig = agent_case(count=2, wire_options={'fail_phase': ('agent_review', 2)},
                     settings_options={'agent_runtime': 'deepseek_harness'})
    assert rig.run()['status'] == 'failed'
    before = rig.evidence()
    assert before['agent']['slots'][1]['attempts'][0]['quality_checks']['correctness']['status'] == 'not_evaluated'
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded'
    after = rig.evidence()
    assert after['configuration'] == before['configuration']
    assert after['agent']['slots'][0] == before['agent']['slots'][0]
    assert after['agent']['slots'][1]['attempts'][0]['quality_checks']['correctness']['status'] == 'passed'
    assert offline_harness == ['author', 'solve', 'review', 'author', 'solve', 'review', 'review']
    assert rig.text_call_count() == 7 and len(rig.retrieval_calls) == 1
    assert len(rig.contents()) == 1
