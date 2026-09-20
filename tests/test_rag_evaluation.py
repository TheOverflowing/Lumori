"""Hand-computed retrieval scores and reproducible paired comparisons."""
from copy import deepcopy
import json
from math import log2

import pytest

from app.rag_evaluation import compare_runs, evaluate_run, validate_dataset


@pytest.fixture
def dataset():
    return {
        'schema_version': 'rag-benchmark-v1', 'dataset_id': 'hand-computed-v1',
        'label_status': 'assistant_authored_unreviewed',
        'documents': [{'id': key, 'text': f'Source {key}.'} for key in ('a', 'b', 'c', 'd')],
        'queries': [
            {'id': 'multi', 'text': 'Explain both concepts.', 'split': 'dev', 'category': 'multi_evidence',
             'language': 'zh', 'answerable': True, 'relevance': {'a': 2, 'b': 1}},
            {'id': 'single', 'text': 'Define the term.', 'split': 'test', 'category': 'definition',
             'language': 'en', 'answerable': True, 'relevance': {'c': 2}},
            {'id': 'absent', 'text': 'What is outside this course?', 'split': 'test', 'category': 'unanswerable',
             'language': 'en', 'answerable': False, 'relevance': {}},
        ],
    }


def by_query(result):
    return {row['query_id']: row for row in result['per_query']}


def test_multiple_gold_graded_ndcg_and_short_list_are_hand_computable(dataset):
    result = evaluate_run(dataset, {'multi': ['d', 'b', 'a'], 'single': ['c'], 'absent': []}, ks=(1, 3, 5))
    multi = by_query(result)['multi']['metrics']
    assert multi['recall@1'] == 0
    assert multi['precision@1'] == 0
    assert multi['hit@1'] == 0
    assert multi['mrr@1'] == 0
    assert multi['ndcg@1'] == 0
    assert multi['all_evidence@1'] == 0
    assert multi['evidence_coverage@1'] == 0
    assert multi['recall@3'] == 1
    assert multi['precision@3'] == pytest.approx(2 / 3)
    assert multi['hit@3'] == 1
    assert multi['mrr@3'] == .5
    assert multi['ndcg@3'] == pytest.approx((1 / log2(3) + 3 / log2(4)) / (3 + 1 / log2(3)))
    assert multi['all_evidence@3'] == 1
    assert multi['evidence_coverage@3'] == 1
    assert multi['precision@5'] == .4  # Three returned results still divide by requested k=5.
    assert by_query(result)['single']['metrics']['precision@5'] == .2
    assert result['overall']['answerable_metrics']['precision@5'] == pytest.approx(.3)


def test_recall_and_all_evidence_do_not_confuse_one_hit_with_complete_evidence(dataset):
    result = evaluate_run(dataset, {'multi': ['a']}, ks=(1,))
    row = by_query(result)['multi']
    assert row['metrics']['recall@1'] == .5
    assert row['metrics']['hit@1'] == 1
    assert row['metrics']['all_evidence@1'] == 0
    assert row['metrics']['evidence_coverage@1'] == .5
    assert row['required_evidence_groups'] == [['a'], ['b']]
    assert row['evidence_groups_source'] == 'qrels_singletons'


def test_alternative_sources_cover_one_required_group_without_retrieving_every_qrel(dataset):
    dataset['queries'][0]['relevance'] = {'a': 2, 'b': 2, 'c': 1}
    dataset['queries'][0]['required_evidence_groups'] = [['a', 'b'], ['c']]
    row = by_query(evaluate_run(dataset, {'multi': ['b', 'c']}, ks=(1, 2)))['multi']
    assert row['metrics']['recall@1'] == pytest.approx(1 / 3)
    assert row['metrics']['evidence_coverage@1'] == .5
    assert row['metrics']['all_evidence@1'] == 0
    assert row['metrics']['recall@2'] == pytest.approx(2 / 3)
    assert row['metrics']['evidence_coverage@2'] == 1
    assert row['metrics']['all_evidence@2'] == 1
    assert row['evidence_groups_source'] == 'explicit'


def test_missing_answerable_query_is_scored_zero_and_kept_in_every_denominator(dataset):
    result = evaluate_run(dataset, {'multi': ['a', 'b'], 'absent': []}, ks=(3,))
    missing = by_query(result)['single']
    assert missing['ranking_status'] == 'missing'
    assert all(value == 0 for value in missing['metrics'].values())
    assert result['overall']['answerable_metrics']['recall@3'] == .5
    assert result['overall']['denominators'] == {
        'queries': 3, 'answerable_queries': 2, 'unanswerable_queries': 1,
        'provided_rankings': 2, 'missing_rankings': 1,
        'provided_answerable_rankings': 1, 'provided_unanswerable_rankings': 1,
    }
    assert result['overall']['missing_query_ids'] == ['single']
    assert result['overall']['ranking_coverage'] == pytest.approx(2 / 3)
    for field, value in [('split', 'test'), ('category', 'definition'), ('language', 'en')]:
        group = result['groups'][field][value]
        assert group['denominators']['answerable_queries'] == 1
        assert group['answerable_metrics']['recall@3'] == 0


def test_unanswerable_empty_nonempty_and_missing_are_separate_from_answerable_metrics(dataset):
    for ranking, expected_empty, expected_nonempty, expected_missing in [
        ({'absent': []}, 1, 0, 0), ({'absent': ['d']}, 0, 1, 0), ({}, 0, 0, 1),
    ]:
        result = evaluate_run(dataset, {'multi': ['a', 'b'], 'single': ['c'], **ranking}, ks=(3,))
        unanswered = by_query(result)['absent']
        assert unanswered['metrics'] is None
        assert result['overall']['answerable_metrics']['recall@3'] == 1
        assert result['overall']['unanswerable'] == {
            'denominator': 1, 'empty_return_count': expected_empty, 'empty_return_rate': expected_empty,
            'nonempty_return_count': expected_nonempty, 'missing_ranking_count': expected_missing,
        }
        assert result['groups']['category']['unanswerable']['answerable_metrics']['recall@3'] is None


def test_macro_means_are_per_query_not_weighted_by_number_of_relevant_documents(dataset):
    result = evaluate_run(dataset, {'multi': ['a'], 'single': ['c'], 'absent': []}, ks=(1,))
    assert result['overall']['answerable_metrics']['recall@1'] == .75  # mean(1/2, 1), not 2/3
    assert result['groups']['split']['dev']['answerable_metrics']['recall@1'] == .5
    assert result['groups']['split']['test']['answerable_metrics']['recall@1'] == 1


def test_comparison_pairs_the_full_query_set_even_with_disjoint_run_coverage(dataset):
    result = compare_runs(dataset, {'multi': ['a'], 'absent': []}, {'single': ['c']}, k=1, bootstrap_samples=400)
    summary = result['overall']
    assert summary['denominators'] == {
        'paired_answerable_queries': 2, 'baseline_provided_rankings': 1,
        'candidate_provided_rankings': 1, 'both_provided_rankings': 0,
    }
    recall = summary['metrics']['recall@1']
    assert recall['baseline_mean'] == .25
    assert recall['candidate_mean'] == .5
    assert recall['mean_difference'] == .25
    assert recall['difference_pp'] == 25
    assert recall['relative_improvement_pct'] == 100
    assert recall['bootstrap_ci95'] == [-.5, 1]
    assert result['coverage']['baseline_missing_query_ids'] == ['single']
    assert result['coverage']['candidate_missing_query_ids'] == ['multi', 'absent']
    assert result['unanswerable']['baseline']['empty_return_rate'] == 1
    assert result['unanswerable']['candidate']['empty_return_rate'] == 0
    delta = {row['query_id']: row for row in result['per_query_deltas']}
    assert delta['multi']['delta']['recall@1'] == -.5
    assert delta['single']['delta']['recall@1'] == 1
    assert delta['absent']['delta'] is None


def test_zero_baseline_relative_improvement_is_null_with_constant_paired_interval(dataset):
    result = compare_runs(dataset, {}, {'single': ['c']}, k=1, bootstrap_samples=30)
    test_split = result['groups']['split']['test']
    recall = test_split['metrics']['recall@1']
    assert test_split['denominators']['paired_answerable_queries'] == 1
    assert recall['mean_difference'] == 1
    assert recall['difference_pp'] == 100
    assert recall['relative_improvement_pct'] is None
    assert recall['bootstrap_ci95'] == [1, 1]
    assert recall['bootstrap_ci95_pp'] == [100, 100]


def test_fixed_seed_reproduces_comparison_without_mutating_inputs_or_upgrading_labels(dataset):
    baseline = {'multi': ['b'], 'single': ['d'], 'absent': []}
    candidate = {'multi': ['a'], 'single': ['c'], 'absent': ['d']}
    before = deepcopy((dataset, baseline, candidate))
    left = compare_runs(dataset, baseline, candidate, k=2, bootstrap_samples=80, seed=12)
    right = compare_runs(dataset, baseline, candidate, k=2, bootstrap_samples=80, seed=12)
    assert left == right
    assert (dataset, baseline, candidate) == before
    assert left['label_status'] == 'assistant_authored_unreviewed'
    assert 'Exploratory only' in left['method']['inference_scope']
    json.dumps(left, allow_nan=False)
    json.dumps(evaluate_run(dataset, baseline), allow_nan=False)
    dataset['label_status'] = 'human_reviewed'
    assert evaluate_run(dataset, baseline)['label_status'] == 'human_reviewed'


def test_all_unanswerable_dataset_has_null_retrieval_metrics_and_no_empty_bootstrap(dataset):
    dataset['queries'] = [dataset['queries'][2]]
    result = compare_runs(dataset, {'absent': []}, {}, bootstrap_samples=3)
    assert result['overall']['denominators']['paired_answerable_queries'] == 0
    assert all(value is None for value in result['overall']['metrics']['recall@5'].values())
    assert result['unanswerable']['baseline']['empty_return_rate'] == 1
    assert result['unanswerable']['candidate']['empty_return_rate'] == 0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('ranking', [
    {'unknown-query': []}, {'multi': ['unknown-document']}, {'multi': ['a', 'a']},
    {'multi': 'a'}, {'multi': [True]}, {'multi': [{'id': 'a'}]}, {1: []}, [],
])
def test_invalid_ranking_identifiers_or_shapes_reject_the_entire_run(dataset, ranking):
    with pytest.raises(ValueError):
        evaluate_run(dataset, ranking)
    with pytest.raises(ValueError):
        compare_runs(dataset, {}, ranking)


@pytest.mark.parametrize('grade', [True, False, 0, 3, -1, 1.0, '1', None])
def test_qrel_grades_require_strict_one_or_two(dataset, grade):
    dataset['queries'][0]['relevance']['a'] = grade
    with pytest.raises(ValueError, match='strict integers'):
        validate_dataset(dataset)


@pytest.mark.parametrize('mutation', [
    lambda data: data.update(schema_version='other'),
    lambda data: data.update(dataset_id=' '),
    lambda data: data.update(label_status='approved_by_model'),
    lambda data: data.update(documents=[]),
    lambda data: data.update(queries=[]),
    lambda data: data['documents'].append(deepcopy(data['documents'][0])),
    lambda data: data['queries'].append(deepcopy(data['queries'][0])),
    lambda data: data['documents'][0].update(text=''),
    lambda data: data['queries'][0].update(text=' '),
    lambda data: data['queries'][0].update(category=''),
    lambda data: data['queries'][0].update(language=''),
    lambda data: data['queries'][0].update(split='train'),
    lambda data: data['queries'][0].pop('split'),
    lambda data: data['queries'][0].update(answerable=1),
    lambda data: data['queries'][0].update(relevance={}),
    lambda data: data['queries'][2].update(relevance={'a': 1}),
    lambda data: data['queries'][0].update(relevance={'unknown': 2}),
])
def test_invalid_dataset_contracts_are_rejected(dataset, mutation):
    mutation(dataset)
    with pytest.raises(ValueError):
        validate_dataset(dataset)


@pytest.mark.parametrize('groups', [[], [[]], [['unknown']], [['c']], [['a', 'a']], ['a'], None])
def test_invalid_required_evidence_groups_are_rejected(dataset, groups):
    dataset['queries'][0]['required_evidence_groups'] = groups
    with pytest.raises(ValueError):
        validate_dataset(dataset)


def test_unanswerable_groups_must_be_empty_and_cross_group_document_reuse_is_valid(dataset):
    dataset['queries'][0]['required_evidence_groups'] = [['a', 'b'], ['a']]
    dataset['queries'][2]['required_evidence_groups'] = []
    assert validate_dataset(dataset) is None
    dataset['queries'][2]['required_evidence_groups'] = [['a']]
    with pytest.raises(ValueError):
        validate_dataset(dataset)


@pytest.mark.parametrize('ks', [(), [], [0], [-1], [True], [1.0], ['1'], [1, 1], 1])
def test_invalid_cutoffs_are_rejected(dataset, ks):
    with pytest.raises(ValueError):
        evaluate_run(dataset, {}, ks=ks)


@pytest.mark.parametrize('kwargs', [{'k': True}, {'k': 0}, {'bootstrap_samples': 0}, {'bootstrap_samples': True}, {'bootstrap_samples': 1.5}, {'seed': True}, {'seed': '12'}])
def test_invalid_bootstrap_options_are_rejected(dataset, kwargs):
    with pytest.raises(ValueError):
        compare_runs(dataset, {}, {}, **kwargs)


def test_optional_query_families_cannot_leak_across_dev_and_test(dataset):
    assert validate_dataset(dataset) is None  # Old datasets need not supply families.
    dataset['queries'][0]['family_id'] = 'same-information-need'
    dataset['queries'][1]['family_id'] = 'same-information-need'
    with pytest.raises(ValueError, match='must not cross'):
        validate_dataset(dataset)
    dataset['queries'][1]['split'] = 'dev'
    assert validate_dataset(dataset) is None


@pytest.mark.parametrize('family_id', ['', ' ', None, 1])
def test_supplied_query_family_id_must_be_nonempty_text(dataset, family_id):
    dataset['queries'][0]['family_id'] = family_id
    with pytest.raises(ValueError, match='family_id'):
        validate_dataset(dataset)


@pytest.mark.parametrize('status,expected_boundary', [
    ('public_human_relevance', '公开数据原有的人工相关性'),
    ('public_human_qa_derived', '不是穷尽的人工检索 qrels'),
    ('community_duplicate_labels', '社区重复问题关系'),
])
def test_external_annotation_provenance_and_scope_survive_scoring_and_comparison(dataset, status, expected_boundary):
    dataset.update(label_status=status, evaluation_scope='Fixed external candidate subset; source judgments only.')
    before = deepcopy(dataset)
    ranking = {'multi': ['a', 'b'], 'single': ['c'], 'absent': []}
    scored = evaluate_run(dataset, ranking, ks=(5,))
    compared = compare_runs(dataset, ranking, ranking, k=5, bootstrap_samples=3)
    for result in (scored, compared):
        assert result['label_status'] == status
        assert result['evaluation_scope'] == dataset['evaluation_scope']
        assert expected_boundary in result['annotation_boundary']
    assert scored['overall']['answerable_metrics']['recall@5'] == 1
    assert compared['overall']['metrics']['recall@5']['mean_difference'] == 0
    assert 'assistant-authored' not in compared['method']['inference_scope']
    if status == 'public_human_qa_derived':
        assert 'not exhaustive human retrieval qrels' in compared['method']['inference_scope']
    assert dataset == before


@pytest.mark.parametrize('scope', ['', ' ', None, 1, True, {}, []])
def test_optional_evaluation_scope_requires_nonempty_text_when_supplied(dataset, scope):
    dataset['evaluation_scope'] = scope
    with pytest.raises(ValueError, match='evaluation_scope'):
        validate_dataset(dataset)
