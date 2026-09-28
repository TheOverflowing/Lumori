import asyncio
from copy import deepcopy
import json

import httpx
import pytest
from pydantic import ValidationError

from app.assessment_contracts import validate_requirement_checks
from app.generation_agent import FlexibleReview
from app.harness_evidence import build_catalog, prepare_messages, resolve_selection
from app.harness_runner import phase_instructions, run_harness_phase
from app.providers import ProviderOutputError
from test_harness_runner import completion, integration, make_agent


QUESTION = {'kind': 'short_answer', 'stem': 'Compare both accounts and state what remains unknown.',
            'answer': 'Both accounts support participation; neither establishes exclusion.',
            'explanation': 'An omitted fact is unknown, not proof that it happened.',
            'citation_ids': ['chunk-1']}
BRIEF = {'slot_id': 'q1', 'difficulty': 'hard', 'kind': 'short_answer',
         'focus': 'Bounded interpretation', 'learning_goal': 'Compare evidence and its limits.',
         'answer_policy': 'multiple_defensible', 'source_ids': ['chunk-1'],
         'requirements': [{'id': 'r1', 'kind': 'reasoning', 'description': 'State the evidentiary limits.',
                           'source_ids': ['chunk-1']}]}


def messages():
    return [{'role': 'system', 'content': 'Audit the candidate and copy exact evidence.'},
            {'role': 'user', 'content': json.dumps({'task': 'agent_review', 'question': QUESTION,
             'independent_solution': {'answer': 'PRIVATE SOLVER MUST NOT BE SELECTABLE'},
             'schema': FlexibleReview.model_json_schema()})}]


def response(catalog):
    ids = {value['field']: key for key, value in catalog.spans.items()}
    return {'answer_correct': True, 'explanation_correct': True, 'source_supported': True,
            'ambiguity_free': True, 'tool_inputs_match_question': True, 'calculations_verified': True,
            'distinct_from_previous': True, 'confidence': 'high', 'issues': [], 'feedback': 'Fixture audit.',
            'requirement_checks': [{'id': 'r1', 'status': 'met', 'stem_evidence_id': ids['stem'],
                'answer_evidence_id': ids['answer'], 'explanation': 'Preset semantic judgment.',
                'source_ids': ['chunk-1']}]}


def test_selection_roundtrip_retains_quality_and_source_validation():
    original = messages()
    wire, catalog = prepare_messages(original)
    assert original == messages()
    assert 'PRIVATE SOLVER' not in json.dumps(catalog.public())
    schema = json.loads(wire[1]['content'])['schema']['$defs']['RequirementCheck']
    assert 'answer_evidence' not in schema['properties']
    assert 'answer_evidence_id' in schema['required']
    raw = response(catalog)
    resolved = resolve_selection(raw, catalog)
    parsed = FlexibleReview.model_validate(resolved)
    assert validate_requirement_checks([c.model_dump() for c in parsed.requirement_checks],
                                       BRIEF, QUESTION, {'chunk-1'}) == []
    assert raw['requirement_checks'][0].get('answer_evidence') is None
    resolved['requirement_checks'][0]['source_ids'] = ['foreign']
    with pytest.raises(ValueError, match='scope'):
        validate_requirement_checks(resolved['requirement_checks'], BRIEF, QUESTION, {'chunk-1'})


@pytest.mark.parametrize('bad', ['unknown', 7, None, 'stem_id'])
def test_unknown_and_wrong_field_references_fail_closed(bad):
    catalog = build_catalog(QUESTION)
    raw = response(catalog)
    if bad == 'stem_id':
        bad = next(k for k, v in catalog.spans.items() if v['field'] == 'stem')
    raw['requirement_checks'][0]['answer_evidence_id'] = bad
    with pytest.raises(ProviderOutputError):
        resolve_selection(raw, catalog)


def test_negative_verdict_is_preserved_and_empty_met_is_rejected():
    catalog = build_catalog(QUESTION)
    raw = response(catalog)
    check = raw['requirement_checks'][0]
    check.update(status='missing', answer_evidence_id='')
    raw['answer_correct'] = False
    parsed = FlexibleReview.model_validate(resolve_selection(raw, catalog))
    assert parsed.answer_correct is False
    assert validate_requirement_checks([c.model_dump() for c in parsed.requirement_checks],
                                       BRIEF, QUESTION, {'chunk-1'}) == ['requirement_coverage:r1:missing']
    check['status'] = 'met'
    with pytest.raises(ValidationError):
        FlexibleReview.model_validate(resolve_selection(raw, catalog))


def test_spans_are_exact_bounded_and_bound_to_candidate_hash():
    q = deepcopy(QUESTION)
    q['answer'] = '甲乙丙丁戊己庚辛壬癸' * 103 + '\n' + 'A literal long English paragraph. ' * 80
    catalog = build_catalog(q)
    for span in catalog.spans.values():
        assert 8 <= len(span['text']) <= 400
        assert span['text'] == q[span['field']][span['start']:span['end']]
    assert build_catalog(QUESTION).candidate_sha256 != catalog.candidate_sha256


def test_plain_roles_and_extra_fields_are_not_silently_modified():
    m = messages()
    c = json.loads(m[1]['content']); c['task'] = 'agent_solve'
    m[1]['content'] = json.dumps(c)
    assert prepare_messages(m) == (m, None)
    catalog = build_catalog(QUESTION)
    raw = response(catalog)
    raw['requirement_checks'][0]['answer_evidence'] = 'forged text'
    with pytest.raises(ProviderOutputError):
        resolve_selection(raw, catalog)


@integration
def test_real_harness_restores_selected_evidence_before_host_validation(tmp_path):
    catalog = build_catalog(QUESTION)
    seen = []
    def wire(request):
        body = json.loads(request.content)
        contract = json.loads(next(m['content'] for m in body['messages'] if m['role'] == 'user'))
        assert contract['candidate_evidence_catalog'] == catalog.public()
        seen.append(contract)
        return httpx.Response(200, json=completion(json.dumps(response(catalog))))
    agent = make_agent(tmp_path, wire)
    report = {}
    async def scenario():
        try:
            value = await run_harness_phase(agent, messages(), 'review', report)
            FlexibleReview.model_validate(value)
            assert value['requirement_checks'][0]['answer_evidence'] == QUESTION['answer']
        finally:
            await agent.pipeline.providers.close()
    asyncio.run(scenario())
    assert len(seen) == 1
    assert 'answer_evidence_id' in report['evidence_selection']['raw_checks'][0]
    assert report['status'] == 'completed'
