"""Format accommodation must preserve material and still obtain a quality review."""
from copy import deepcopy
import json

import pytest

from app.generation_agent import authorize_resume, progress
from app.store import dumps
from test_generation_agent import AgentWire, agent_case  # noqa: F401
from test_harness_quality_gates import offline_harness  # noqa: F401


def allow_mixed(rig):
    payload = json.loads(rig.job()['payload']) | {'question_type': 'mixed'}
    rig.store.execute('UPDATE jobs SET payload=? WHERE id=?', (dumps(payload), rig.job_id))
    return rig


def malformed_format(monkeypatch, defect, *, position=2, first_n=None):
    original = AgentWire.__call__

    def respond(self, request):
        response = original(self, request)
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        where, _ = self.identity(contract)
        if (response.status_code != 200 or contract['task'] != 'agent_author' or where != position or
                (first_n is not None and self.stage_counts[('agent_author', where)] > first_n)):
            return response
        raw = json.loads(response.json()['choices'][0]['message']['content'])
        question = raw['questions'][0]
        if defect in ('options', 'both'):
            question.update(kind='mcq', options=['One', 'Two', 'Three', 'Four', 'Five', 'Six'], answer='(B)')
        if defect in ('concepts', 'both'):
            question['difficulty_design']['concepts'] = [f'Concept {i}' for i in range(7)]
        return self.response(raw)

    monkeypatch.setattr(AgentWire, '__call__', respond)


@pytest.mark.parametrize('runtime', ['native', 'deepseek_harness'])
@pytest.mark.parametrize('defect', ['options', 'concepts', 'both'])
def test_three_strict_attempts_then_lossless_local_replay_and_real_review(
        agent_case, monkeypatch, offline_harness, runtime, defect):
    malformed_format(monkeypatch, defect)
    rig = allow_mixed(agent_case(count=3, settings_options={'agent_runtime': runtime}))
    assert rig.run()['status'] == 'succeeded', rig.job()
    slots = rig.evidence()['agent']['slots']
    first, second, third = slots
    assert len(first['attempts']) == len(third['attempts']) == 1
    assert len(second['attempts']) == 4
    assert all(a['feedback']['issues'] == ['author_contract_invalid'] for a in second['attempts'][:3])
    compatible = second['attempts'][3]
    assert compatible['author']['origin'] == 'format_compatibility_replay'
    assert compatible['author']['original_attempt'] == 3
    assert compatible['format_compatibility']['mode'] == 'compatible'
    assert compatible['quality_checks']['correctness']['status'] == 'passed'
    assert compatible['solve']['response'] and compatible['review']['response']
    question = second['accepted_asset']['questions'][0]
    original = second['attempts'][2]['author']['response']['questions'][0]
    assert question['stem'] == original['stem']
    assert question['explanation'] == original['explanation']
    if defect in ('options', 'both'):
        assert question['options'] == original['options'] and len(question['options']) == 6
        assert question['answer'] == 'B' and original['answer'] == '(B)'
        review = next(c for c in rig.wire.contracts if c['task'] == 'agent_review' and 'Fixture 02.' in c['question']['stem'])
        assert review['question']['options'] == question['options'] and review['question']['answer'] == 'B'
    if defect in ('concepts', 'both'):
        assert question['difficulty_design']['concepts'] == original['difficulty_design']['concepts']
        assert len(question['difficulty_design']['concepts']) == 7
    assert rig.wire.stage_counts[('agent_author', 2)] == 3
    assert rig.wire.stage_counts[('agent_solve', 2)] == rig.wire.stage_counts[('agent_review', 2)] == 1
    assert rig.text_call_count() == 11


def test_third_normal_repair_can_succeed_without_compatibility(agent_case, monkeypatch):
    malformed_format(monkeypatch, 'both', first_n=2)
    rig = allow_mixed(agent_case(count=2))
    assert rig.run()['status'] == 'succeeded'
    attempts = rig.evidence()['agent']['slots'][1]['attempts']
    assert len(attempts) == 3 and attempts[-1]['status'] == 'passed'
    assert not any('format_compatibility' in a for a in attempts)


def test_compatible_format_never_overrides_wrong_answer_and_preserves_prior_work(agent_case, monkeypatch):
    malformed_format(monkeypatch, 'both')
    rig = allow_mixed(agent_case(count=3, wire_options={'review_fail_at': 2}))
    assert rig.run()['status'] == 'failed'
    state = rig.evidence()['agent']
    assert state['status'] == 'quality_failed' and not rig.contents()
    assert state['slots'][0]['status'] == 'passed' and state['slots'][2]['status'] == 'pending'
    attempts = state['slots'][1]['attempts']
    assert len(attempts) == 6  # Three strict format attempts, three real content failures.
    assert all(a['quality_checks']['correctness']['status'] == 'failed' for a in attempts[3:])
    assert progress(rig.evidence())['resume_kind'] == 'repair'


def test_manual_content_repair_extends_only_failed_slot_and_keeps_call_ledger(agent_case):
    rig = agent_case(count=3, wire_options={'review_fail_at': 2})
    assert rig.run()['status'] == 'failed'
    before = deepcopy(rig.evidence())
    calls = rig.text_call_count()
    assert len(before['agent']['slots'][1]['attempts']) == 3
    authorize_resume(rig.store, rig.job_id)
    queued = rig.evidence()
    assert queued['configuration'] == before['configuration']
    assert queued['agent']['slots'][0] == before['agent']['slots'][0]
    assert queued['agent']['slots'][1]['attempts'] == before['agent']['slots'][1]['attempts']
    assert queued['agent']['slots'][1]['repair_extensions'][0]['start_attempt'] == 4
    assert rig.text_call_count() == calls
    rig.wire.review_fail_at = None
    assert rig.run()['status'] == 'succeeded', rig.job()
    after = rig.evidence()['agent']['slots']
    assert after[0] == before['agent']['slots'][0]
    assert len(after[1]['attempts']) == 4
    assert rig.text_call_count() == calls + 6
    with pytest.raises(ValueError):
        authorize_resume(rig.store, rig.job_id)


def test_format_replay_respects_lifetime_call_limit(agent_case, monkeypatch):
    malformed_format(monkeypatch, 'both', position=1)
    rig = allow_mixed(agent_case(count=1, settings_options={'agent_max_calls': 5}))
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'call_limit'
    assert not rig.contents()
    assert rig.text_call_count() == 4  # Embedding also consumes this task's ledger.


def test_resuming_compatible_review_does_not_repeat_authors_or_completed_solution(agent_case, monkeypatch):
    malformed_format(monkeypatch, 'both', position=1)
    rig = allow_mixed(agent_case(count=1, wire_options={'fail_phase': ('agent_review', 1)}))
    assert rig.run()['status'] == 'failed'
    assert rig.evidence()['agent']['status'] == 'provider_error'
    authorize_resume(rig.store, rig.job_id)
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.wire.stage_counts[('agent_author', 1)] == 3
    assert rig.wire.stage_counts[('agent_solve', 1)] == 1
    assert rig.wire.stage_counts[('agent_review', 1)] == 2


def test_format_repairs_can_continue_beyond_six_attempts_until_a_valid_response(agent_case, monkeypatch):
    original = AgentWire.__call__

    def respond(self, request):
        response = original(self, request)
        contract = json.loads(json.loads(request.content)['messages'][1]['content'])
        if contract['task'] != 'agent_author': return response
        raw = json.loads(response.json()['choices'][0]['message']['content'])
        raw['questions'][0].update(kind='mcq', options=['One', 'Two', 'Three', 'Four', 'Five', 'Six'],
            answer='(B) and (C)' if self.stage_counts[('agent_author', 1)] <= 6 else '(B)')
        return self.response(raw)

    monkeypatch.setattr(AgentWire, '__call__', respond)
    rig = allow_mixed(agent_case(count=1))
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.wire.stage_counts[('agent_author', 1)] == 7
    assert rig.text_call_count() == 9
    assert rig.asset()['questions'][0]['answer'] == 'B'
    assert len(rig.asset()['questions'][0]['options']) == 6
    assert all(c['schema']['$defs']['Question']['allOf'][0]['then']['properties']['options']['maxItems'] == 26
               for c in [c for c in rig.wire.contracts if c['task'] == 'agent_author'][3:])


def test_manual_retry_after_compatible_content_failures_keeps_prior_windows(agent_case, monkeypatch):
    malformed_format(monkeypatch, 'both')
    rig = allow_mixed(agent_case(count=2, wire_options={'review_fail_at': 2}))
    assert rig.run()['status'] == 'failed'
    before = deepcopy(rig.evidence()['agent']['slots'])
    authorize_resume(rig.store, rig.job_id)
    rig.wire.review_fail_at = None
    assert rig.run()['status'] == 'succeeded', rig.job()
    slots = rig.evidence()['agent']['slots']
    assert slots[0] == before[0]
    assert slots[1]['attempts'][:len(before[1]['attempts'])] == before[1]['attempts']
    assert slots[1]['accepted_asset']['questions'][0]['options'] == ['One','Two','Three','Four','Five','Six']
