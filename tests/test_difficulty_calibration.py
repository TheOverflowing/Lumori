"""Offline policy regression checks; no claims about model/learner calibration."""
import json

import pytest

from app.generation_agent import authorize_resume
from app.store import dumps
from test_generation_agent import AgentWire, agent_case
from test_difficulty_pipeline import run_case
from test_harness_quality_gates import offline_harness


def sequence_solver(monkeypatch, levels, *, position=1):
    original = AgentWire.__call__

    def respond(self, request):
        response = original(self, request)
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        if response.status_code == 200 and contract['task'] == 'agent_solve':
            where, _ = self.identity(contract)
            if where == position:
                n = self.stage_counts[('agent_solve', where)]
                body = json.loads(response.json()['choices'][0]['message']['content'])
                return self.response(body | {'assessed_difficulty': levels[min(n-1, len(levels)-1)]})
        return response

    monkeypatch.setattr(AgentWire, '__call__', respond)


def test_legacy_can_pass_strictly_on_third_round(run_case):
    job, evidence, content, wire = run_case(mode='repair_twice')
    assert job['status'] == 'succeeded'
    assert len(wire.assessments) == len(wire.generations) == 3
    decision = content['config']['difficulty_acceptance']
    assert decision['strict_passed'] is True and decision['status'] == 'strict'
    assert decision['rounds'] == decision['accepted_attempt'] == 3
    assert decision['acceptance_reason'] == 'target_matched'


@pytest.mark.parametrize('runtime', ['native', 'deepseek_harness'])
@pytest.mark.parametrize('levels,expected_status,expected_attempt', [
    (['hard'], 'strict', 1),
    (['easy', 'medium', 'hard'], 'strict', 3),
    (['medium', 'easy', 'easy'], 'adjusted', 1),
    (['easy', 'easy', 'easy'], 'adjusted', 3),
])
def test_agent_three_rounds_then_nearest_reviewed_candidate_and_remaining_slots(
        agent_case, monkeypatch, offline_harness, runtime, levels, expected_status, expected_attempt):
    sequence_solver(monkeypatch, levels)
    rig = agent_case(count=2, settings_options={'agent_runtime': runtime})
    assert rig.run()['status'] == 'succeeded', rig.job()
    state = rig.evidence()['agent']
    first, second = state['slots']
    assert first['status'] == second['status'] == 'passed'
    assert second['difficulty_acceptance']['strict_passed'] is True
    assert len(second['attempts']) == 1
    decision = first['difficulty_acceptance']
    assert decision['status'] == expected_status
    assert decision['accepted_attempt'] == expected_attempt
    assert decision['rounds'] == (1 if levels == ['hard'] else 3)
    for attempt in first['attempts']:
        assert attempt['quality_checks']['correctness'] == {'status': 'passed', 'issues': []}
        assert attempt.get('review', {}).get('response')
    config = json.loads(rig.contents()[0]['config'])
    assert config['difficulty_acceptance']['status'] == expected_status
    assert config['difficulty_acceptance']['correctness_relaxed'] is False
    assert config['difficulty_acceptance']['items'][0]['target_difficulty'] == 'hard'
    assert rig.asset()['questions'][0]['difficulty'] == 'hard'  # Still the requested design target.
    assert rig.text_call_count() == (6 if levels == ['hard'] else 12)


@pytest.mark.parametrize('blocker', [
    {'answer_correct': False}, {'source_supported': False}, {'ambiguity_free': False},
    {'confidence': 'low'}, {'explanation_correct': False},
])
def test_difficulty_mismatch_does_not_relax_other_quality_gates(agent_case, monkeypatch, blocker):
    sequence_solver(monkeypatch, ['medium'])
    rig = agent_case(count=1, wire_options={'review_override': blocker})
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    state = rig.evidence()['agent']
    assert state['status'] == 'quality_failed'
    assert len(state['slots'][0]['attempts']) == 3  # The current normal repair budget.
    assert not any(a.get('difficulty_candidate') for a in state['slots'][0]['attempts'])


def test_later_bad_answer_does_not_replace_prior_reviewed_candidate(agent_case, monkeypatch):
    sequence_solver(monkeypatch, ['medium'])
    original = AgentWire.__call__

    def fail_later(self, request):
        response = original(self, request)
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        if response.status_code == 200 and contract['task'] == 'agent_review' and self.stage_counts[('agent_review', 1)] > 1:
            body = json.loads(response.json()['choices'][0]['message']['content'])
            return self.response(body | {'answer_correct': False})
        return response

    monkeypatch.setattr(AgentWire, '__call__', fail_later)
    rig = agent_case(count=1)
    assert rig.run()['status'] == 'succeeded'
    slot = rig.evidence()['agent']['slots'][0]
    assert slot['difficulty_acceptance']['accepted_attempt'] == 1
    assert len(slot['attempts']) == 3
    assert slot['attempts'][1]['quality_checks']['correctness']['status'] == 'failed'
    assert slot['accepted_asset'] == slot['attempts'][0]['difficulty_candidate']['asset']


def test_interrupted_third_round_resume_reuses_prior_calls_and_candidate_pool(agent_case, monkeypatch):
    sequence_solver(monkeypatch, ['medium'])
    original = AgentWire.__call__

    def interrupt_once(self, request):
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        if contract['task'] == 'agent_review' and self.stage_counts[('agent_author', 1)] == 3 and not getattr(self, '_interrupted', False):
            self._interrupted = True
            import httpx
            return httpx.Response(429, text='fixture unavailable')
        return original(self, request)

    monkeypatch.setattr(AgentWire, '__call__', interrupt_once)
    rig = agent_case(count=1)
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'provider_error'
    assert not rig.contents()
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded'
    slot = rig.evidence()['agent']['slots'][0]
    assert slot['difficulty_acceptance']['rounds'] == 3
    assert rig.wire.stage_counts[('agent_author', 1)] == 3
    assert rig.wire.stage_counts[('agent_solve', 1)] == 3
    assert rig.text_call_count() == 10


def test_legacy_checkpoint_preserves_old_gate_and_attempt_budget(agent_case, monkeypatch):
    rig = agent_case(count=1, wire_options={'fail_phase': ('agent_solve', 1)})
    assert rig.run()['status'] == 'failed'
    saved = rig.evidence()
    saved['configuration'].pop('difficulty_policy')
    saved['configuration'].pop('repair_policy')
    rig.store.execute('UPDATE job_evidence SET evidence=? WHERE job_id=?', (dumps(saved), rig.job_id))
    sequence_solver(monkeypatch, ['medium'])
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'failed'
    state = rig.evidence()['agent']
    assert state['status'] == 'quality_failed'
    assert len(state['slots'][0]['attempts']) == 2
    assert not any('review' in attempt for attempt in state['slots'][0]['attempts'])
    assert 'difficulty_policy' not in rig.evidence()['configuration']
    assert 'repair_policy' not in rig.evidence()['configuration']


def test_difficulty_extra_rounds_never_exceed_explicit_call_budget(agent_case, monkeypatch):
    sequence_solver(monkeypatch, ['medium'])
    rig = agent_case(count=1, settings_options={'agent_max_calls': 7})
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'call_limit'
    assert not rig.contents()
    assert rig.text_call_count() == 6
    assert len(rig.evidence()['agent']['slots'][0]['attempts']) == 3
