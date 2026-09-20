"""Author schema alignment and safe repair feedback; no live model calls."""
from copy import deepcopy
import json

import httpx
import pytest
from pydantic import ValidationError

from app.author_contract import (author_asset_schema, author_repair_contract,
                                 parse_author_asset, schema_validation_diagnostic)
from app.difficulty import AssetRuleError
from app.models import LearningAsset
from test_workflow import done, request, rig, seed  # noqa: F401


def asset(**question_changes):
    return {
        'title': 'Course review', 'evidence_sufficient': True, 'sections': [],
        'questions': [{
            'slot_id': 'q1', 'difficulty': 'medium',
            'difficulty_design': {'cognitive_process': 'apply', 'concepts': ['Retrieval'],
                                  'expected_steps': ['Apply the supplied rule']},
            'kind': 'mcq', 'stem': 'What does retrieval supply?',
            'options': ['Course evidence', 'A proof of correctness', 'Model training', 'Teacher approval'],
            'answer': 'A', 'explanation': 'Retrieved sources support a draft, which still needs review.',
            'difficulty_reason': 'Apply the evidence rule.', 'citation_ids': ['source-1'],
            **question_changes,
        }],
    }


def diagnostic(value):
    with pytest.raises((ValidationError, AssetRuleError)) as caught:
        parse_author_asset(value)
    if isinstance(caught.value, AssetRuleError):
        return {'rule': caught.value.rule, **caught.value.details}
    return schema_validation_diagnostic(caught.value, author_asset_schema())


def test_author_schema_exposes_existing_question_kind_constraints_and_keeps_storage_compatible():
    schema = author_asset_schema()
    question = schema['$defs']['Question']
    mcq, short_answer = question['allOf']
    assert mcq['if'] == {'properties': {'kind': {'const': 'mcq'}}, 'required': ['kind']}
    assert mcq['then']['properties']['options'] == {
        'type': 'array', 'minItems': 4, 'maxItems': 4, 'uniqueItems': True}
    assert mcq['then']['properties']['answer']['enum'] == ['A', 'B', 'C', 'D']
    assert mcq['then']['required'] == ['options']
    assert short_answer['if']['properties']['kind']['const'] == 'short_answer'
    assert short_answer['then']['properties']['options']['maxItems'] == 0
    assert LearningAsset.model_validate(asset()).questions[0].answer == 'A'
    assert LearningAsset.model_validate(asset(kind='short_answer', options=[], answer='A course source.'))
    # Describing the author contract neither changes storage optional fields nor
    # mutates a schema later used by an independent question author.
    assert 'allOf' not in LearningAsset.model_json_schema()['$defs']['Question']
    question['allOf'][0]['then']['properties']['options']['maxItems'] = 99
    assert author_asset_schema()['$defs']['Question']['allOf'][0]['then']['properties']['options']['maxItems'] == 4


@pytest.mark.parametrize('changes,code,field,expected,actual', [
    ({'options': ['one', 'two', 'three', 'four', 'five', 'six']}, 'mcq_option_count', 'options',
     {'min_length': 4, 'max_length': 4, 'unique_items': True}, 6),
    ({'options': ['one', 'two', 'three']}, 'mcq_option_count', 'options',
     {'min_length': 4, 'max_length': 4, 'unique_items': True}, 3),
    ({'options': ['one', 'two', 'three', 'one']}, 'mcq_option_unique', 'options',
     {'min_length': 4, 'max_length': 4, 'unique_items': True}, None),
    ({'answer': '(B)'}, 'mcq_answer_key', 'answer', {'allowed_values': ['A', 'B', 'C', 'D']}, None),
    ({'kind': 'short_answer', 'options': ['one']}, 'short_answer_options', 'options',
     {'max_length': 0}, 1),
])
def test_question_validation_explains_exact_fields_without_changing_candidates(changes, code, field, expected, actual):
    value = asset(**changes)
    original = deepcopy(value)
    error, = diagnostic(value)['fields']
    assert error == {'loc': ['questions', 0, field], 'type': code, 'expected': expected,
                     **({'actual_length': actual} if actual is not None else {})}
    assert value == original


@pytest.mark.parametrize('field,count,maximum', [('concepts', 7, 6), ('expected_steps', 9, 8)])
def test_nested_design_lengths_report_the_real_bound_and_observed_length(field, count, maximum):
    value = asset()
    value['questions'][0]['difficulty_design'][field] = [f'PRIVATE_GENERATED_TEXT_{i}' for i in range(count)]
    error, = diagnostic(value)['fields']
    assert error == {
        'loc': ['questions', 0, 'difficulty_design', field], 'type': 'too_long',
        'expected': {'min_length': 1, 'max_length': maximum}, 'actual_length': count,
    }
    assert 'PRIVATE_GENERATED_TEXT' not in json.dumps(error)


def test_diagnostic_does_not_echo_unknown_fields_values_exception_messages_or_context():
    marker = 'PRIVATE_PROVIDER_TEXT_DO_NOT_ECHO'
    value = asset()
    value[marker] = marker
    value['title'] = {'secret': marker}
    value['questions'][0]['difficulty_design']['title'] = marker  # Known elsewhere, invalid here.
    result = diagnostic(value)
    assert {'loc': ['title'], 'type': 'string_type'} in result['fields']
    assert {'loc': ['unknown_field'], 'type': 'extra_forbidden'} in result['fields']
    assert {'loc': ['questions', 0, 'difficulty_design', 'unknown_field'],
            'type': 'extra_forbidden'} in result['fields']
    assert marker not in json.dumps(result)

    class UntrustedCustomError:
        def errors(self, **kwargs):
            assert kwargs == {'include_input': False, 'include_context': True, 'include_url': False}
            return [{'loc': ('questions', 0, 'answer'), 'type': marker, 'msg': marker,
                     'ctx': {'actual_length': marker}, 'input': marker}] * 25

    result = schema_validation_diagnostic(UntrustedCustomError(), author_asset_schema())
    assert len(result['fields']) == 20
    assert result['fields'][0] == {'loc': ['questions', 0, 'answer'], 'type': 'validation_error'}
    assert marker not in json.dumps(result)


def test_repair_contract_requires_verified_answers_and_consolidation_instead_of_truncation():
    contract = author_repair_contract()
    assert 'exactly four different options' in contract['mcq_options']
    assert 'do not automatically truncate' in contract['mcq_options']
    assert 'verified correct option' in contract['mcq_answer']
    assert '1 to 6' in contract['difficulty_design.concepts']
    assert '1 to 8' in contract['difficulty_design.expected_steps']
    assert 'preserving every fact' in contract['difficulty_design.concepts']


@pytest.mark.parametrize('answer', ['(F)', '[F]', 'F.', 'f'])
def test_compatibility_preserves_six_options_seven_concepts_and_verified_answer_position(answer):
    value = asset(options=[f'Option {i}' for i in range(6)], answer=answer)
    value['questions'][0]['difficulty_design']['concepts'] = [f'Concept {i}' for i in range(7)]
    original = deepcopy(value)
    candidate, audit = parse_author_asset(value, relaxed=True)
    question = candidate.questions[0]
    assert question.options == original['questions'][0]['options']
    assert question.difficulty_design.concepts == original['questions'][0]['difficulty_design']['concepts']
    assert question.answer == 'F'
    assert value == original
    assert audit['mode'] == 'compatible'
    assert audit['transformations'] == [{'path': ['questions', 0, 'answer'], 'rule': 'bare_option_letter'}]
    assert [item['actual_length'] for item in audit['relaxed_constraints']] == [6, 7]
    # Stored/reviewed/exported assets must remain readable after compatibility.
    assert LearningAsset.model_validate(candidate.model_dump()) == candidate


@pytest.mark.parametrize('options,answer', [
    (['Only one'], 'A'), (['one', 'one'], 'A'), (['one', ' '], 'A'),
    (['one', 'two'], '(C)'), (['one', 'two'], 'A or B'), (['one', 'two'], 'Answer: B'),
    ([str(i) for i in range(27)], 'A'),
])
def test_compatibility_does_not_invent_an_answer_or_discard_invalid_options(options, answer):
    value = asset(options=options, answer=answer)
    original = deepcopy(value)
    with pytest.raises(ValidationError):
        parse_author_asset(value, relaxed=True)
    assert value == original


@pytest.mark.parametrize('count', [2, 3, 4, 6, 26])
def test_relaxed_schema_and_model_keep_only_letters_for_existing_options(count):
    schema = author_asset_schema(relaxed=True)
    conditional = schema['$defs']['Question']['allOf']
    assert 'maxItems' not in schema['$defs']['DifficultyDesign']['properties']['concepts']
    assert conditional[0]['then']['properties']['options'] == {
        'type': 'array', 'minItems': 2, 'maxItems': 26, 'uniqueItems': True}
    if count < 26:
        branch = next(item for item in conditional[2:] if item['if']['properties']['options']['minItems'] == count)
        assert branch['then']['properties']['answer']['enum'] == [chr(65 + i) for i in range(count)]
    candidate, _ = parse_author_asset(asset(options=[str(i) for i in range(count)], answer=chr(64 + count)), relaxed=True)
    assert candidate.questions[0].answer == chr(64 + count)


@pytest.mark.parametrize('rejected_by_review', [False, True])
def test_legacy_compatibility_reuses_third_raw_candidate_and_still_checks_quality(rig, monkeypatch, rejected_by_review):
    client, app, wire = rig
    course, _ = seed(client)
    original = type(wire).__call__
    authors, reviews = [], []

    def always_six_options(self, http_request):
        response = original(self, http_request)
        if not http_request.url.path.endswith('/chat/completions'):
            return response
        messages = json.loads(http_request.content)['messages']
        contract = json.loads(messages[1]['content'])
        envelope = response.json()
        value = json.loads(envelope['choices'][0]['message']['content'])
        if contract.get('task') == 'difficulty_assessment':
            reviews.append(contract)
            if rejected_by_review:
                value['items'][0]['answerable_from_sources'] = False
        else:
            authors.append(contract)
            question = value['questions'][0]
            question['options'].extend(['Fifth', 'Sixth'])
            question['answer'] = '(A)'
            question['difficulty_design']['concepts'] = [f'Concept {i}' for i in range(7)]
        envelope['choices'][0]['message']['content'] = json.dumps(value)
        return httpx.Response(200, json=envelope)

    monkeypatch.setattr(type(wire), '__call__', always_six_options)
    job = done(client, client.post('/api/generations', json=request(course)))
    assert len(authors) == (5 if rejected_by_review else 3)
    assert len(reviews) == (3 if rejected_by_review else 1)
    assert job['status'] == ('failed' if rejected_by_review else 'succeeded'), job
    assert len(reviews[0]['questions'][0]['options']) == 6
    evidence = json.loads(app.state.store.one('SELECT evidence FROM job_evidence WHERE job_id=?', (job['id'],))['evidence'])
    assert len(evidence['attempts']) == (5 if rejected_by_review else 3)
    assert all(item['response']['questions'][0]['answer'] == '(A)' for item in evidence['attempts'])
    compatibility, = evidence['format_compatibility']
    assert compatibility['source_attempt'] == 3
    assert compatibility['format_compatibility']['transformations']
    contents = client.get('/api/contents', params={'course_id': course}).json()
    assert len(contents) == (0 if rejected_by_review else 1)
    if not rejected_by_review:
        saved = client.get('/api/contents/' + job['result']['content_id']).json()
        question = saved['asset']['questions'][0]
        assert len(question['options']) == 6 and question['answer'] == 'A'
        assert len(question['difficulty_design']['concepts']) == 7
        assert saved['config']['format_compatibility']['mode'] == 'compatible'


@pytest.mark.parametrize('eventually_valid', [True, False])
def test_compatible_format_repairs_continue_past_six_but_obey_the_existing_call_budget(rig, monkeypatch, eventually_valid):
    client, app, wire = rig
    course, _ = seed(client)
    if not eventually_valid:
        app.state.settings.max_daily_calls = 8
    original = type(wire).__call__
    authors = []

    def format_repair(self, http_request):
        response = original(self, http_request)
        if not http_request.url.path.endswith('/chat/completions'):
            return response
        messages = json.loads(http_request.content)['messages']
        contract = json.loads(messages[1]['content'])
        if contract.get('task') == 'difficulty_assessment':
            return response
        authors.append(contract)
        if not eventually_valid or len(authors) < 7:
            envelope = response.json()
            value = json.loads(envelope['choices'][0]['message']['content'])
            value['title'] = {'PRIVATE_PROVIDER_TEXT': 'An invalid title type'}
            envelope['choices'][0]['message']['content'] = json.dumps(value)
            return httpx.Response(200, json=envelope)
        return response

    monkeypatch.setattr(type(wire), '__call__', format_repair)
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == ('succeeded' if eventually_valid else 'failed'), job
    assert len(authors) > 3
    if eventually_valid:
        assert len(authors) == 7
    else:
        assert len(app.state.store.all('SELECT id FROM calls')) == 8
        assert client.get('/api/contents', params={'course_id': course}).json() == []
    for contract in authors[3:]:
        question_schema = contract['schema']['$defs']['Question']
        assert question_schema['properties']['options']['maxItems'] == 26
        assert {'slot_id', 'difficulty', 'difficulty_design'} <= set(question_schema['required'])
        assert contract['schema']['properties']['questions']['maxItems'] == 1
        assert 'maxItems' not in contract['schema']['$defs']['DifficultyDesign']['properties']['concepts']
        assert contract['author_repair_contract'] == author_repair_contract(relaxed=True)


@pytest.mark.parametrize('defect,expected_type', [
    ('options', 'mcq_option_count'), ('answer', 'mcq_answer_key'), ('concepts', 'too_long'),
])
def test_legacy_author_gets_actionable_feedback_then_publishes_only_repaired_asset(rig, monkeypatch, defect, expected_type):
    client, app, wire = rig
    course, _ = seed(client)
    original = type(wire).__call__
    authors = []

    def invalid_then_valid(self, http_request):
        response = original(self, http_request)
        if not http_request.url.path.endswith('/chat/completions'):
            return response
        messages = json.loads(http_request.content)['messages']
        contract = json.loads(messages[1]['content'])
        if contract.get('task') == 'difficulty_assessment':
            return response
        authors.append(messages)
        if len(authors) == 1:
            envelope = response.json()
            value = json.loads(envelope['choices'][0]['message']['content'])
            question = value['questions'][0]
            if defect == 'options': question['options'].extend(['Fifth', 'Sixth'])
            elif defect == 'answer': question['answer'] = '(A)'
            else: question['difficulty_design']['concepts'] = [f'Concept {i}' for i in range(7)]
            envelope['choices'][0]['message']['content'] = json.dumps(value)
            return httpx.Response(200, json=envelope)
        return response

    monkeypatch.setattr(type(wire), '__call__', invalid_then_valid)
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'succeeded', job
    assert len(authors) == 2
    repair = json.loads(authors[1][-1]['content'].split('\n', 1)[1])
    error, = repair['validation']['fields']
    assert error['type'] == expected_type
    assert 'expected' in error
    assert repair['required_constraints']['author_fields'] == author_repair_contract()
    original_contract = json.loads(authors[0][1]['content'])
    assert original_contract['author_repair_contract'] == author_repair_contract()
    assert original_contract['schema']['$defs']['Question']['allOf']
    contents = client.get('/api/contents', params={'course_id': course}).json()
    assert len(contents) == 1
    saved = client.get('/api/contents/' + job['result']['content_id']).json()['asset']['questions'][0]
    assert len(saved['options']) == 4 and saved['answer'] == 'A'
    assert len(saved['difficulty_design']['concepts']) <= 6
