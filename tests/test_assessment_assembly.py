"""Question-local assembly preserves reviewed material and source-label scope."""
from copy import deepcopy

import pytest

from app.assessment_assembly import SECTION_SEPARATOR, assemble_question_sections
from app.models import Section


def piece(index, *sections):
    return {'questions': [{'slot_id': f'q{index}', 'stem': 'Compare Source A and Source B.',
                           'answer': 'An already reviewed answer.', 'explanation': 'An already reviewed explanation.'}],
            'sections': list(sections)}


def section(heading, text, *citations):
    return {'heading': heading, 'text': text, 'citation_ids': list(citations)}


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_conflicting_source_labels_stay_in_separate_question_bundles(language):
    pieces = [piece(1, section('Source A', 'Curator account.', 'curator'),
                       section('Source B', 'Interpretive method.', 'method')),
              piece(2, section('Source A', 'Curator account.', 'curator'),
                       section('Source B', 'Volunteer account.', 'volunteer'))]
    original = deepcopy(pieces)
    result = assemble_question_sections(pieces, language=language,
        allowed_sources_by_slot={'q1': ['curator', 'method'], 'q2': ['curator', 'volunteer']})
    assert pieces == original
    assert result[0]['text'] == 'Source A\n\nCurator account.' + SECTION_SEPARATOR + 'Source B\n\nInterpretive method.'
    assert result[1]['text'] == 'Source A\n\nCurator account.' + SECTION_SEPARATOR + 'Source B\n\nVolunteer account.'
    assert 'Volunteer account.' not in result[0]['text']
    assert 'Interpretive method.' not in result[1]['text']
    assert [row['citation_ids'] for row in result] == [['curator', 'method'], ['curator', 'volunteer']]
    assert result[0]['heading'] == ('第1题 · 资料' if language == 'zh' else 'Question 1 · Supporting material')
    assert result[1]['heading'] == ('第2题 · 资料' if language == 'zh' else 'Question 2 · Supporting material')
    # Existing Section readers can consume each bundle without a new field.
    assert [Section.model_validate(row).model_dump() for row in result] == result


@pytest.mark.parametrize('section_count', [2, 10])
def test_fifty_questions_keep_every_original_section_without_truncation(section_count):
    pieces = [piece(index, *[section(f'Source {j}', f'Unique reviewed text Q{index}/S{j}', f'source-{j}')
                             for j in range(section_count)]) for index in range(1, 51)]
    original = deepcopy(pieces)
    scope = {f'q{index}': [f'source-{j}' for j in range(section_count)] for index in range(1, 51)}
    result = assemble_question_sections(pieces, language='en', allowed_sources_by_slot=scope)
    assert len(result) == 50 and pieces == original
    assert sum(len(row['text'].split(SECTION_SEPARATOR)) for row in result) == 50 * section_count
    for index, row in enumerate(result, 1):
        assert row['text'].split(SECTION_SEPARATOR) == [s['heading'] + '\n\n' + s['text'] for s in pieces[index-1]['sections']]
    assert f'Unique reviewed text Q50/S{section_count-1}' in result[-1]['text']


def test_citations_preserve_first_appearance_and_do_not_expose_ids_in_visible_text():
    original = piece(1, section('Source A', 'First text.', 'private-chunk-b', 'private-chunk-a'),
                        section('Source B', 'Second text.', 'private-chunk-a', 'private-chunk-c'))
    result = assemble_question_sections([original], language='en', allowed_sources_by_slot={
        'q1': ['private-chunk-a', 'private-chunk-b', 'private-chunk-c']})
    assert result[0]['citation_ids'] == ['private-chunk-b', 'private-chunk-a', 'private-chunk-c']
    assert 'private-chunk-' not in result[0]['heading'] + result[0]['text']
    result[0]['citation_ids'].append('new-result-only')
    assert original['sections'][0]['citation_ids'] == ['private-chunk-b', 'private-chunk-a']


def test_original_whitespace_headings_and_duplicate_sections_remain_verbatim():
    repeated = section('  Source B  ', '\n  Quoted material.\n', 'source')
    pieces = [piece(1, deepcopy(repeated), deepcopy(repeated)), piece(2, deepcopy(repeated))]
    before = deepcopy(pieces)
    result = assemble_question_sections(pieces, language='zh', allowed_sources_by_slot={'q1': ['source'], 'q2': ['source']})
    block = repeated['heading'] + '\n\n' + repeated['text']
    assert result[0]['text'] == block + SECTION_SEPARATOR + block
    assert result[1]['text'] == block
    assert pieces == before


def test_foreign_reference_is_rejected_even_if_available_to_another_worker():
    pieces = [piece(1, section('A', 'Wrong reference.', 'source-for-q2')),
              piece(2, section('B', 'Other reference.', 'source-for-q2'))]
    with pytest.raises(ValueError, match='own worker'):
        assemble_question_sections(pieces, language='en', allowed_sources_by_slot={
            'q1': ['source-for-q1'], 'q2': ['source-for-q2']})


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_order', 'extra_question'])
def test_question_scope_requires_exact_single_question_order(mutation):
    pieces = [piece(1, section('A', 'Text one.', 'source')), piece(2, section('B', 'Text two.', 'source'))]
    if mutation == 'missing':
        del pieces[1]['questions'][0]['slot_id']
    elif mutation == 'duplicate':
        pieces[1]['questions'][0]['slot_id'] = 'q1'
    elif mutation == 'wrong_order':
        pieces.reverse()
    else:
        pieces[0]['questions'].append(deepcopy(pieces[1]['questions'][0]))
    before = deepcopy(pieces)
    with pytest.raises(ValueError):
        assemble_question_sections(pieces, language='en', allowed_sources_by_slot={'q1': ['source'], 'q2': ['source']})
    assert pieces == before


@pytest.mark.parametrize('count', [0, 11])
def test_empty_or_excessive_original_sections_fail_instead_of_dropping_context(count):
    pieces = [piece(1, *[section('A', 'Text.', 'source') for _ in range(count)])]
    with pytest.raises(ValueError, match='1–10 original sections'):
        assemble_question_sections(pieces, language='en', allowed_sources_by_slot={'q1': ['source']})


@pytest.mark.parametrize('count', [0, 51])
def test_empty_or_excessive_question_sets_are_rejected(count):
    pieces = [piece(i, section('A', 'Text.', 'source')) for i in range(1, count+1)]
    with pytest.raises(ValueError, match='1–50'):
        assemble_question_sections(pieces, language='en', allowed_sources_by_slot={f'q{i}': ['source'] for i in range(1, count+1)})


@pytest.mark.parametrize('scope', [{}, {'q1': []}, {'q1': ['source'] * 2},
                                   {'q1': [f'source-{i}' for i in range(11)]},
                                   {'q1': ['source'], 'q2': ['source']}, {'q1': [' source']}])
def test_missing_extra_duplicate_or_overlarge_worker_scopes_fail(scope):
    with pytest.raises(ValueError):
        assemble_question_sections([piece(1, section('A', 'Text.', 'source'))], language='en', allowed_sources_by_slot=scope)


@pytest.mark.parametrize('bad_section', [
    section('', 'Text.', 'source'), section('A', '   ', 'source'),
    section('A', 'Text.'), section('A', 'Text.', 'unknown'),
    {'heading': 'A', 'text': 'Text.', 'citation_ids': 'source'},
])
def test_invalid_original_sections_fail_explicitly(bad_section):
    with pytest.raises(ValueError):
        assemble_question_sections([piece(1, bad_section)], language='en', allowed_sources_by_slot={'q1': ['source']})


def test_unknown_language_cannot_silently_choose_a_presentation():
    with pytest.raises(ValueError, match='language'):
        assemble_question_sections([piece(1, section('A', 'Text.', 'source'))], language='other', allowed_sources_by_slot={'q1': ['source']})
