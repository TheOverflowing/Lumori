"""Author HTTP output cannot opt into the host's question-local assembly mode.

All providers are isolated fixtures. These tests exercise generation, rejection,
repair and publication boundaries; they do not measure model judgment quality.
"""
import copy
import json

import pytest

from app.models import GenerateRequest, LearningAsset
from app.pipeline import validate_asset
from test_flexible_agent import flexible_case, flexible_wire  # noqa: F401
from test_generation_agent import AgentWire, SOURCE, agent_case  # noqa: F401
from test_harness_quality_gates import offline_harness  # noqa: F401
from test_workflow import apply_difficulty_fixture, difficulty_judge_fixture


@pytest.fixture
def native_scope_wire(monkeypatch):
    """Use the real legacy Pipeline and HTTP ledger, replacing provider output."""
    options = {'scopes': [None], 'shared_sections': 1, 'author_calls': 0}

    def respond(self, request):
        messages = json.loads(request.content)['messages']
        contract = json.loads(messages[1]['content'])
        self.contracts.append(copy.deepcopy(contract))
        self.messages.append(copy.deepcopy(messages))
        if contract.get('task') == 'difficulty_assessment':
            return self.response(difficulty_judge_fixture(contract, options['levels']))
        assert 'task' not in contract
        assert request.url.path.endswith('/chat/completions')
        position = options['author_calls']
        options['author_calls'] += 1
        scope = options['scopes'][min(position, len(options['scopes']) - 1)]
        count = contract['question_count']
        section_count = count if scope == 'per_question' else options['shared_sections']
        value = {
            'title': 'Scoped author transport fixture', 'evidence_sufficient': True,
            'sections': [
                {'heading': f'Context {i + 1}', 'text': SOURCE['text'], 'citation_ids': [SOURCE['id']]}
                for i in range(section_count)],
            'questions': [
                {'kind': 'short_answer', 'stem': f'Exercise {i + 1}: justify an index recovery plan under isolation constraints.',
                 'options': [], 'answer': 'Use the supplied course evidence and verify the recovered index.',
                 'explanation': 'Balance recovery, course isolation, and verification constraints.',
                 'difficulty_reason': 'Compare constrained recovery choices.', 'citation_ids': [SOURCE['id']]}
                for i in range(count)],
        }
        if scope is not None:
            value['section_scope'] = scope
        options['levels'] = apply_difficulty_fixture(value, contract)
        return self.response(value)

    monkeypatch.setattr(AgentWire, '__call__', respond)
    return options


def native_case(agent_case, **kwargs):
    return agent_case(settings_options={'generation_workflow': 'legacy_v3'}, **kwargs)


def assert_author_schema(contract, section_limit):
    schema = contract['schema']
    assert schema['properties']['section_scope']['const'] == 'shared'
    assert schema['properties']['sections']['maxItems'] == section_limit


@pytest.mark.parametrize('count', [1, 11])
def test_native_author_cannot_claim_host_scope_even_with_ordered_complete_questions(
        agent_case, native_scope_wire, count):
    native_scope_wire['scopes'] = ['per_question']
    rig = native_case(agent_case, count=count)
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    attempts = rig.evidence()['attempts']
    assert len(attempts) == 3
    assert all(a['status'] == 'invalid_output' for a in attempts)
    assert all(a['validation']['rule'] == 'author_section_scope_must_be_shared' for a in attempts)
    assert all('assessment' not in a for a in attempts)
    assert rig.text_call_count() == 3
    # These are otherwise storage-valid host assets; failure is the author
    # boundary, not a slot/section count mismatch or a malformed fixture.
    for attempt in attempts:
        candidate = LearningAsset.model_validate(attempt['response'])
        assert len(candidate.questions) == len(candidate.sections) == count
        assert candidate.section_scope == 'per_question'
    for contract in rig.wire.contracts:
        assert_author_schema(contract, 10)


@pytest.mark.parametrize('scope', [None, 'shared'])
def test_native_author_keeps_shared_default_and_fifty_question_capacity(
        agent_case, native_scope_wire, scope):
    native_scope_wire.update(scopes=[scope], shared_sections=10)
    rig = native_case(agent_case, count=50)
    assert rig.run()['status'] == 'succeeded', rig.job()
    assert rig.asset()['section_scope'] == 'shared'
    assert len(rig.asset()['sections']) == 10
    assert len(rig.asset()['questions']) == 50
    assert_author_schema(rig.wire.contracts[0], 10)
    assert rig.wire.contracts[0]['schema']['properties']['questions']['maxItems'] == 50
    assert rig.text_call_count() == 2


def test_native_shared_author_still_rejects_eleven_sections(agent_case, native_scope_wire):
    native_scope_wire.update(scopes=['shared'], shared_sections=11)
    rig = native_case(agent_case, count=11)
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    assert all(a['validation']['rule'] == 'schema_validation' for a in rig.evidence()['attempts'])
    assert rig.text_call_count() == 3


def test_native_scope_rejection_can_be_repaired_without_publishing_first_candidate(
        agent_case, native_scope_wire):
    native_scope_wire['scopes'] = ['per_question', 'shared']
    rig = native_case(agent_case, count=11)
    assert rig.run()['status'] == 'succeeded', rig.job()
    attempts = rig.evidence()['attempts']
    assert [a['status'] for a in attempts] == ['invalid_output', 'valid']
    assert attempts[0]['validation']['rule'] == 'author_section_scope_must_be_shared'
    assert len(attempts[0]['response']['sections']) == 11
    assert len(rig.contents()) == 1
    assert rig.asset()['section_scope'] == 'shared'
    assert len(rig.asset()['sections']) == 1
    assert len(rig.asset()['questions']) == 11
    assert rig.text_call_count() == 3
    for contract in rig.wire.contracts:
        if contract.get('task') != 'difficulty_assessment':
            assert_author_schema(contract, 10)


@pytest.mark.parametrize('orchestration', ['sequential_v1', 'supervisor_v2'])
def test_single_question_author_cannot_claim_host_scope(
        agent_case, offline_harness, flexible_wire, orchestration):
    flexible_wire['author_mutation'] = lambda value, contract: value.update(section_scope='per_question')
    rig = flexible_case(agent_case, count=1, settings_options={'agent_orchestration': orchestration})
    assert rig.run()['status'] == 'failed'
    assert not rig.contents()
    slot = rig.evidence()['agent']['slots'][0]
    assert len(slot['attempts']) == 3
    attempt = slot['attempts'][0]
    assert attempt['status'] == 'rejected'
    assert attempt['feedback']['issues'] == ['author_contract_invalid']
    assert attempt['feedback']['validation']['rule'] == 'author_section_scope_must_be_shared'
    assert 'solve' not in attempt and 'review' not in attempt
    assert 'accepted_asset' not in slot
    expected_phases = ['author'] * 3 if orchestration == 'sequential_v1' else ['plan'] + ['author'] * 3
    assert offline_harness == expected_phases
    assert rig.text_call_count() == len(expected_phases)
    author = next(c for c in rig.wire.contracts if c['task'] == 'agent_author')
    assert_author_schema(author, 2)


def test_host_can_still_publish_eleven_scoped_bundles_from_shared_author_outputs(
        agent_case, offline_harness, flexible_wire):
    rig = flexible_case(agent_case, count=11)
    assert rig.run()['status'] == 'succeeded', rig.job()
    asset = rig.asset()
    slots = rig.evidence()['agent']['slots']
    assert asset['section_scope'] == 'per_question'
    assert len(asset['sections']) == len(asset['questions']) == 11
    assert all(s['accepted_asset']['section_scope'] == 'shared' for s in slots)
    assert asset['questions'] == [s['accepted_asset']['questions'][0] for s in slots]
    for number, (section, slot) in enumerate(zip(asset['sections'], slots), 1):
        assert section['heading'] == f'Question {number} · Supporting material'
        for original in slot['accepted_asset']['sections']:
            assert original['heading'] + '\n\n' + original['text'] in section['text']
    for contract in rig.wire.contracts:
        if contract['task'] == 'agent_author':
            assert_author_schema(contract, 2)
    assert offline_harness == ['plan', 'plan'] + ['author', 'solve', 'review'] * 11
    assert rig.text_call_count() == 35
    # The default validation boundary remains suitable for host assembly and
    # saved edits; only author_output=True excludes the host-owned scope.
    request = GenerateRequest.model_validate(json.loads(rig.job()['payload']))
    candidate = LearningAsset.model_validate(asset)
    validate_asset(candidate, request, rig.evidence()['sources'], enforce_difficulty=True)
    with pytest.raises(ValueError) as error:
        validate_asset(candidate, request, rig.evidence()['sources'], enforce_difficulty=True, author_output=True)
    assert error.value.rule == 'author_section_scope_must_be_shared'
