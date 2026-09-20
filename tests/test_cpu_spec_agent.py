"""Controller integration uses preset judgments; no paid model or Harness call."""
from copy import deepcopy
import asyncio
import json

import pytest

import app.harness_runner as harness_runner
from app.cpu_problem_spec import compile_cpu_spec
from app.generation_agent import GenerationAgent, authorize_resume
from app.models import GenerateRequest
from app.store import dumps
from test_generation_agent import AgentWire, SOURCE, agent_case


SPEC = {
    'processes': [{'id': 'P1', 'arrival': 0, 'burst': 6},
                  {'id': 'P2', 'arrival': 1, 'burst': 2},
                  {'id': 'P3', 'arrival': 2, 'burst': 4}],
    'scenarios': [
        {'id': 'R', 'policy': 'rr', 'quantum': 2, 'switch_cost': 0,
         'boundary': 'after', 'during_switch': 'tail', 'early_finish_free': False},
        {'id': 'S', 'policy': 'stcf', 'switch_cost': 0}],
    'remaining_queries': [{'scenario_id': 'S', 'process_id': 'P1', 'time': 3}],
    'selection': {'metric': 'mean_turnaround', 'direction': 'min', 'constraints': [
        {'metric': 'mean_response', 'operator': 'le', 'threshold': {'numerator': 2, 'denominator': 1}}]},
}


@pytest.fixture
def structured_wire(monkeypatch):
    control = {'proposal': {'evidence_sufficient': True, 'spec': deepcopy(SPEC), 'citation_ids': [SOURCE['id']]},
               'solver_mutation': None, 'level': 'hard', 'phases': []}
    monkeypatch.setattr(harness_runner, 'verify_runtime', lambda settings: None)

    async def phase(agent, messages, phase, report):
        control['phases'].append(phase)
        report['fixture'] = 'no-harness-process'
        return await agent.pipeline.providers.generate(messages, agent.job_id)

    monkeypatch.setattr(harness_runner, 'run_harness_phase', phase)

    def respond(self, request):
        body = json.loads(request.content)
        contract = json.loads(body['messages'][1]['content'])
        self.contracts.append(deepcopy(contract))
        self.messages.append(deepcopy(body['messages']))
        if contract['task'] == 'agent_author_cpu_spec':
            return self.response(control['proposal'])
        if contract['task'] == 'agent_solve':
            compiled = compile_cpu_spec(control['proposal']['spec'], language='en', difficulty='hard', source_ids=[SOURCE['id']])
            requests = [deepcopy(item['request']) for item in compiled['evidence']['tool_results']]
            if control['solver_mutation']:
                requests = control['solver_mutation'](requests)
            return self.response({'answerable': True, 'ambiguity_free': True,
                'assessed_difficulty': control['level'], 'confidence': 'high',
                'answer': 'UNTRUSTED_SOLVER_NUMBER_999', 'explanation': 'UNTRUSTED_SOLVER_PROSE',
                'requires_calculation': True, 'tool_requests': requests})
        assert contract['task'] == 'agent_review'
        return self.response({'answer_correct': True, 'explanation_correct': True, 'source_supported': True,
            'ambiguity_free': True, 'tool_inputs_match_question': True, 'calculations_verified': True,
            'distinct_from_previous': True, 'confidence': 'high', 'issues': [], 'feedback': 'Preset passing review.'})

    monkeypatch.setattr(AgentWire, '__call__', respond)
    return control


def rig_for(agent_case, **overrides):
    return agent_case(count=1, settings_options={'agent_runtime': 'deepseek_harness',
        'agent_question_spec': 'cpu_schedule_v1', 'agent_max_repairs': 0, **overrides})


def test_compiled_answer_and_full_calculations_reach_published_draft(agent_case, structured_wire):
    rig = rig_for(agent_case)
    assert rig.run()['status'] == 'succeeded', rig.job()
    slot = rig.evidence()['agent']['slots'][0]
    attempt = slot['attempts'][0]
    assert slot['verification'] == 'spec_compilation_and_model_review'
    assert len(attempt['tool_results']) == 2
    assert attempt['tool_results'] == attempt['compiled_spec']['tool_results']
    assert 'UNTRUSTED_SOLVER' not in dumps(rig.asset())
    assert rig.asset()['questions'][0]['stem'] == compile_cpu_spec(
        SPEC, language='en', difficulty='hard', source_ids=[SOURCE['id']])['question']['stem']
    solve = rig.wire.contracts[1]
    assert set(solve['question']) == {'kind', 'stem', 'options'}
    assert 'compiled_spec' not in solve and 'target_difficulty' not in solve
    review = rig.wire.contracts[2]
    assert review['rendering_provenance']['spec_sha256'] == attempt['compiled_spec']['spec_sha256']
    assert rig.text_call_count() == 3


@pytest.mark.parametrize('mutation', [
    lambda requests: requests[:1],
    lambda requests: [requests[0], requests[0]],
    lambda requests: [{**requests[0], 'input': {**requests[0]['input'], 'boundary': 'before'}}, requests[1]],
])
def test_solver_cannot_drop_repeat_or_change_scenarios(agent_case, structured_wire, mutation):
    structured_wire['solver_mutation'] = mutation
    rig = rig_for(agent_case)
    assert rig.run()['status'] == 'failed'
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    assert 'spec_tool_inputs_mismatch' in attempt['feedback']['issues']
    assert attempt['quality_checks']['correctness']['status'] == 'not_evaluated'
    assert structured_wire['phases'] == ['author', 'solve'] * 3
    assert not rig.contents()


def test_process_order_and_explicit_stcf_default_do_not_cause_false_mismatch(agent_case, structured_wire):
    def reorder(requests):
        requests.reverse()
        for request in requests:
            request['input']['processes'].reverse()
            if request['input']['policy'] == 'stcf':
                request['input'].pop('switch_cost', None)
        return requests
    structured_wire['solver_mutation'] = reorder
    rig = rig_for(agent_case)
    assert rig.run()['status'] == 'succeeded', rig.job()


def test_structured_review_receives_the_complete_bounded_trace(agent_case, structured_wire):
    structured_wire['proposal']['spec'] = {
        'processes': [{'id': 'A', 'arrival': 0, 'burst': 24}, {'id': 'B', 'arrival': 0, 'burst': 24}],
        'scenarios': [{'id': 'R', 'policy': 'rr', 'quantum': 1, 'switch_cost': 0,
                       'boundary': 'before', 'during_switch': 'tail', 'early_finish_free': False}],
    }
    rig = rig_for(agent_case)
    assert rig.run()['status'] == 'succeeded', rig.job()
    review = next(c for c in rig.wire.contracts if c['task'] == 'agent_review')
    result = review['tool_results'][0]['result']
    assert len(result['timeline']) == 48
    assert 'timeline_omitted' not in result


@pytest.mark.parametrize('proposal', [
    {'evidence_sufficient': True, 'spec': SPEC, 'citation_ids': ['other-account-source']},
    {'evidence_sufficient': True, 'spec': {**SPEC, 'answer': 'injected wrong answer'}, 'citation_ids': [SOURCE['id']]},
    {'evidence_sufficient': False, 'spec': SPEC, 'citation_ids': []},
])
def test_untrusted_proposal_fields_or_sources_never_reach_solver(agent_case, structured_wire, proposal):
    structured_wire['proposal'] = deepcopy(proposal)
    rig = rig_for(agent_case)
    assert rig.run()['status'] == 'failed'
    assert structured_wire['phases'] == ['author'] * 3
    assert not rig.contents()


def test_structured_mode_is_explicit_and_not_available_in_native(agent_case, structured_wire):
    rig = rig_for(agent_case, agent_runtime='native')
    assert rig.run()['status'] == 'failed'
    assert rig.text_call_count() == 0
    assert not rig.retrieval_calls


def test_computation_does_not_hide_adjusted_difficulty(agent_case, structured_wire):
    structured_wire['level'] = 'medium'
    rig = rig_for(agent_case)
    assert rig.run()['status'] == 'succeeded'
    attempt = rig.evidence()['agent']['slots'][0]['attempts'][0]
    assert attempt['quality_checks']['correctness']['status'] == 'passed'
    assert attempt['quality_checks']['difficulty']['status'] == 'mismatch'
    assert structured_wire['phases'] == ['author', 'solve', 'review'] * 3
    assert rig.contents()
    assert rig.evidence()['difficulty_acceptance']['strict_passed'] is False
    assert rig.evidence()['difficulty_acceptance']['status'] == 'adjusted'


@pytest.mark.parametrize('change', ['mode', 'revision'])
def test_old_spec_checkpoints_cannot_mix_with_new_configuration(agent_case, structured_wire, monkeypatch, change):
    rig = rig_for(agent_case)
    request = GenerateRequest.model_validate_json(rig.job()['payload'])
    controller = GenerationAgent(rig.pipeline, request, rig.job_id)
    asyncio.run(controller.initialize())
    controller.state.update(status='provider_error', phase='author')
    controller.save()
    rig.store.execute("UPDATE jobs SET status='failed' WHERE id=?", (rig.job_id,))
    config = deepcopy(rig.evidence()['configuration'])
    authorize_resume(rig.store, rig.job_id)
    if change == 'mode':
        rig.settings.agent_question_spec = 'none'
    else:
        monkeypatch.setattr('app.cpu_problem_spec.CPU_SPEC_REVISION', 'different-compiler-revision')
    assert rig.run()['status'] == 'failed'
    assert '配置已改变' in rig.job()['error']
    assert rig.evidence()['configuration'] == config
    assert rig.text_call_count() == 0
