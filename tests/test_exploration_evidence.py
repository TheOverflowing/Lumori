"""Deterministic passage contracts; these tests do not assess factual relevance."""
import copy

import pytest

from app.auto_exploration import _coverage, _selection
from app.exploration_evidence import evidence_passages, resolve_coverage, resolve_selection


TEXT = 'Supervised learning uses labelled examples to learn how inputs relate to target outputs.'
OTHER = 'Unsupervised learning can discover patterns in data without supplied target labels.'
SOURCES = [{'id': 'source-a', 'text': TEXT + '\n\n' + OTHER}]


def coverage():
    return {'status': 'sufficient', 'requirements': [{'id': 'g1', 'need': 'Learning categories',
        'covered': True, 'supports': [{'passage_id': 'p1'}], 'queries': []}]}


def selection():
    return {'accept': True, 'reason_code': 'supports_gap',
            'supports': [{'gap_id': 'g1', 'passage_id': 'p2'}]}


def test_passages_are_stable_exact_contiguous_original_text_with_provenance():
    original = [{'id': 'a', 'text': ' \n' + TEXT + '  \n\n   ' + OTHER + '\n'},
                {'id': 'b', 'text': 'short\n\n' + TEXT}]
    passages = evidence_passages(original)
    assert passages == [
        {'id': 'p1', 'source_id': 'a', 'text': TEXT},
        {'id': 'p2', 'source_id': 'a', 'text': OTHER},
        {'id': 'p3', 'source_id': 'b', 'text': TEXT}]
    assert passages == evidence_passages(copy.deepcopy(original))
    by_id = {source['id']: source['text'] for source in original}
    assert all(passage['text'] in by_id[passage['source_id']] for passage in passages)


@pytest.mark.parametrize('length', [900, 901, 905, 1801, 24000, 100000])
def test_long_paragraphs_and_total_characters_are_bounded_without_invented_text(length):
    source = {'id': 'large', 'text': ('abcDEF0123_' * 10000)[:length]}
    passages = evidence_passages([source])
    assert all(12 <= len(passage['text']) <= 900 for passage in passages)
    assert sum(len(passage['text']) for passage in passages) <= 24000
    assert len(passages) <= 80
    assert all(passage['text'] in source['text'] for passage in passages)
    if length <= 24000:
        assert ''.join(passage['text'] for passage in passages) == source['text']


def test_many_paragraphs_stop_at_passage_limit():
    passages = evidence_passages([{'id': 'many', 'text': '\n\n'.join(f'Exact paragraph number {i}.' for i in range(150))}])
    assert len(passages) == 80
    assert [passage['id'] for passage in passages] == [f'p{i}' for i in range(1, 81)]


def test_standalone_markdown_heading_is_not_offered_as_explanatory_evidence():
    source = {'id': 'section', 'text': '### Supervised Learning\n\n' + TEXT + '\n\n### Reinforcement Learning'}
    passages = evidence_passages([source])
    assert passages == [{'id': 'p1', 'source_id': 'section', 'text': TEXT}]
    combined = dict(source, text='### Supervised Learning\n' + TEXT)
    assert evidence_passages([combined])[0]['text'] == combined['text']


def test_invalid_and_duplicate_source_identifiers_do_not_create_ambiguous_passages():
    assert evidence_passages(None) == []
    passages = evidence_passages([None, {'id': [], 'text': TEXT}, {'id': 'x', 'text': TEXT},
        {'id': 'x', 'text': OTHER}, {'id': 'y', 'text': None}])
    assert passages == [{'id': 'p1', 'source_id': 'x', 'text': TEXT}]


def test_controller_resolves_coverage_to_actual_text_then_existing_validator_accepts():
    passages, value = evidence_passages(SOURCES), coverage()
    untouched = copy.deepcopy(value)
    resolved = resolve_coverage(value, passages, [])
    assert resolved['requirements'][0]['supports'] == [{'source_id': 'p1', 'quote': TEXT}]
    assert _coverage(resolved, passages) == resolved
    assert value == untouched
    assert passages[0]['source_id'] == 'source-a'


def test_controller_resolves_selection_then_existing_validator_accepts():
    passages, value = evidence_passages(SOURCES), selection()
    untouched = copy.deepcopy(value)
    resolved = resolve_selection(value, passages)
    assert resolved['supports'] == [{'gap_id': 'g1', 'quote': OTHER}]
    assert _selection(resolved, [{'id': 'g1'}], SOURCES[0]['text']) == resolved
    assert value == untouched


def test_covered_requirement_can_omit_empty_queries_without_mutating_response():
    passages, value = evidence_passages(SOURCES), coverage()
    del value['requirements'][0]['queries']
    resolved = resolve_coverage(value, passages, [])
    assert resolved['requirements'][0]['queries'] == []
    assert 'queries' not in value['requirements'][0]
    assert _coverage(resolved, passages)['status'] == 'sufficient'


def test_omitted_queries_do_not_invent_missing_knowledge_search_or_evidence():
    passages, value = evidence_passages(SOURCES), coverage()
    value['status'] = 'missing_knowledge'
    value['requirements'][0].update(covered=False, supports=[])
    del value['requirements'][0]['queries']
    assert resolve_coverage(value, passages, []) is None
    value = coverage()
    del value['requirements'][0]['queries']
    value['requirements'][0]['supports'] = []
    assert _coverage(resolve_coverage(value, passages, []), passages) is None


@pytest.mark.parametrize('queries', [None, 'empty'])
def test_explicit_invalid_queries_are_never_silently_discarded(queries):
    passages, value = evidence_passages(SOURCES), coverage()
    value['requirements'][0]['queries'] = queries
    resolved = resolve_coverage(value, passages, [])
    assert resolved is None or _coverage(resolved, passages) is None


def test_explicit_valid_queries_are_preserved_for_existing_semantic_validator():
    passages, value = evidence_passages(SOURCES), coverage()
    value['requirements'][0]['queries'] = [{'query': 'extra search', 'language': 'en'}]
    resolved = resolve_coverage(value, passages, [])
    assert resolved['requirements'][0]['queries'] == value['requirements'][0]['queries']


@pytest.mark.parametrize('bad_id', ['p999', 'source-a', '', [], None, {'id': 'p1'}])
def test_unknown_or_malformed_passage_ids_never_become_evidence(bad_id):
    passages = evidence_passages(SOURCES)
    value = coverage()
    value['requirements'][0]['supports'][0]['passage_id'] = bad_id
    assert resolve_coverage(value, passages) is None
    value = selection()
    value['supports'][0]['passage_id'] = bad_id
    assert resolve_selection(value, passages) is None


def test_model_cannot_supply_fabricated_text_alongside_passage_id():
    passages = evidence_passages(SOURCES)
    value = coverage()
    value['requirements'][0]['supports'][0]['quote'] = 'This statement does not occur in the source.'
    assert resolve_coverage(value, passages) is None
    value = selection()
    value['supports'][0]['quote'] = 'A claim written by the model rather than the controller.'
    assert resolve_selection(value, passages) is None


def test_mixed_or_duplicate_support_formats_are_rejected():
    passages = evidence_passages(SOURCES)
    value = coverage()
    value['requirements'][0]['supports'].append({'source_id': 'source-a', 'quote': TEXT})
    assert resolve_coverage(value, passages) is None
    value = selection()
    value['supports'].append({'gap_id': 'g1', 'quote': OTHER})
    assert resolve_selection(value, passages) is None
    value = coverage()
    value['requirements'][0]['supports'] *= 2
    assert resolve_coverage(value, passages) is None
    value = selection()
    value['supports'] *= 2
    assert resolve_selection(value, passages) is None


def test_duplicate_passage_ids_and_invalid_passage_text_are_rejected():
    passages = evidence_passages(SOURCES)
    assert resolve_coverage(coverage(), passages + [dict(passages[0], text=OTHER)]) is None
    assert resolve_selection(selection(), passages + [dict(passages[0], text=OTHER)]) is None
    for text in ('short', 'x' * 901, None):
        changed = copy.deepcopy(passages)
        changed[0]['text'] = text
        assert resolve_coverage(coverage(), changed) is None


def test_missing_need_is_only_filled_from_the_same_frozen_requirement():
    passages = evidence_passages(SOURCES)
    frozen = [{'id': 'g1', 'need': 'Original fixed objective'}]
    value = coverage()
    del value['requirements'][0]['need']
    assert resolve_coverage(value, passages) is None
    assert resolve_coverage(value, passages, [{'id': 'g2', 'need': 'Different objective'}]) is None
    resolved = resolve_coverage(value, passages, frozen)
    assert resolved['requirements'][0]['need'] == 'Original fixed objective'
    assert 'need' not in value['requirements'][0]
    value['requirements'][0]['need'] = 'Explicitly changed objective'
    assert resolve_coverage(value, passages, frozen)['requirements'][0]['need'] == 'Explicitly changed objective'
    assert frozen == [{'id': 'g1', 'need': 'Original fixed objective'}]


def test_historical_quote_contracts_are_preserved_without_mutating_originals():
    value = coverage()
    value['requirements'][0]['supports'] = [{'source_id': 'source-a', 'quote': TEXT}]
    resolved = resolve_coverage(value, [], [])
    assert resolved == value
    assert _coverage(resolved, SOURCES) == resolved
    resolved['requirements'][0]['supports'][0]['quote'] = 'modified copy'
    assert value['requirements'][0]['supports'][0]['quote'] == TEXT
    value = selection()
    value['supports'] = [{'gap_id': 'g1', 'quote': OTHER}]
    resolved = resolve_selection(value, [])
    assert resolved == value
    resolved['supports'].clear()
    assert value['supports'] == [{'gap_id': 'g1', 'quote': OTHER}]


@pytest.mark.parametrize('value', [None, [], 'text', {'reasoning': 'unexpected'},
    {'status': [], 'requirements': []}, {'status': 'sufficient', 'requirements': [None]}])
def test_malformed_untrusted_responses_return_none_without_raising(value):
    assert resolve_coverage(value, evidence_passages(SOURCES), []) is None
    assert resolve_selection(value, evidence_passages(SOURCES)) is None


def test_support_caps_and_unknown_fields_are_not_silently_truncated_or_retained():
    passages = evidence_passages(SOURCES)
    value = coverage()
    value['requirements'][0]['supports'] *= 5
    assert resolve_coverage(value, passages) is None
    value = selection()
    value['supports'] *= 7
    assert resolve_selection(value, passages) is None
    value = coverage()
    value['requirements'][0]['reasoning'] = 'not part of the contract'
    assert resolve_coverage(value, passages) is None
    value = selection()
    value['reasoning'] = 'not part of the contract'
    assert resolve_selection(value, passages) is None


@pytest.mark.parametrize('count', [7, 12, 13])
def test_selection_support_bound_allows_multiple_passages_per_gap(count):
    passages = [{'id': f'p{i}', 'source_id': 'candidate', 'text': f'Actual source evidence paragraph number {i}.'}
                for i in range(1, count + 1)]
    value = {'accept': True, 'reason_code': 'supports_gap',
             'supports': [{'gap_id': 'g1', 'passage_id': p['id']} for p in passages]}
    resolved = resolve_selection(value, passages)
    if count > 12:
        assert resolved is None
    else:
        assert [item['quote'] for item in resolved['supports']] == [p['text'] for p in passages]
