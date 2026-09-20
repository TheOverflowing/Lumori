"""Offline retrieval metrics and paired comparisons, using only the standard library.

Rankings are ``{query_id: [document_id, ...]}`` in decreasing relevance order.
Unlisted qrels are treated as nonrelevant. Missing rankings remain in their query
denominators as failures; unanswerable queries never enter retrieval-recall means.
This module never upgrades the supplied annotation-review status.
"""
from collections import defaultdict
from math import fsum, log2
import random


DATASET_SCHEMA = 'rag-benchmark-v1'
LABEL_STATUSES = ('assistant_authored_unreviewed', 'human_reviewed',
                  'public_human_relevance', 'public_human_qa_derived', 'community_duplicate_labels')
ANNOTATION_BOUNDARIES = {
    'assistant_authored_unreviewed': '助手自编且尚未人工复核的标注，仅支持探索性比较，不能视为正式 gold 数据。',
    'human_reviewed': '数据声明经过人工复核；本运行不核实复核者身份、专业背景或标注是否穷尽，也不自动认定为教师审核。',
    'public_human_relevance': '使用公开数据原有的人工相关性标注；适用范围限于该来源的任务、候选池与标注覆盖，不自动等同于课程教师审核。',
    'public_human_qa_derived': '相关性与证据组由公开人工问答数据的支持关系转换得到；这不是穷尽的人工检索 qrels，也不表示教师逐一复核了候选片段。',
    'community_duplicate_labels': '使用社区重复问题关系作为匹配标签；重复判断不等同于课程证据相关性、事实验证或教师审核。',
}
INFERENCE_SCOPES = {
    'assistant_authored_unreviewed': 'Exploratory only: assistant-authored unreviewed labels do not support formal significance claims.',
    'human_reviewed': 'Descriptive paired comparison; annotation review does not establish representative sampling or formal significance.',
    'public_human_relevance': 'Descriptive paired comparison on public human relevance judgments within their original task and judgment coverage; not automatic course or teacher validation.',
    'public_human_qa_derived': 'Exploratory comparison using passage support derived from public human QA annotations, not exhaustive human retrieval qrels; alternate relevant passages may be unlabeled.',
    'community_duplicate_labels': 'Descriptive matching comparison using community duplicate labels; duplicate relations do not establish pedagogical evidence relevance or factual correctness.',
}
METRIC_NAMES = ('recall', 'precision', 'hit', 'mrr', 'ndcg', 'all_evidence', 'evidence_coverage')
GROUP_FIELDS = ('split', 'category', 'language')
DEFINITIONS = {
    'recall@k': 'Retrieved relevant documents / all documents in qrels; not required-evidence coverage.',
    'precision@k': 'Retrieved relevant documents / k, including when fewer than k results are returned.',
    'hit@k': '1 when at least one relevant document occurs in the first k results, otherwise 0.',
    'mrr@k': 'Reciprocal rank of the first relevant result within k, otherwise 0.',
    'ndcg@k': 'DCG / ideal DCG at k, using gain 2**grade - 1 and discount log2(rank + 1).',
    'all_evidence@k': '1 when every required evidence group has at least one result within k, otherwise 0.',
    'evidence_coverage@k': 'Required evidence groups with at least one result within k / all required groups.',
    'required_evidence_groups': 'Alternatives within a group are interchangeable. Absent groups default to one singleton per relevant document.',
    'empty_return_rate': 'Explicitly supplied empty rankings / all unanswerable queries. Missing rankings are failures, not successful abstentions.',
    'macro_averaging': 'Equal weight per answerable query, including zero scores for missing rankings; no unanswerable queries in retrieval metrics.',
}


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{path} must be nonempty text.')


def validate_dataset(dataset):
    """Validate the v1 benchmark schema without mutating or relabeling it."""
    if not isinstance(dataset, dict):
        raise ValueError('dataset must be an object.')
    if dataset.get('schema_version') != DATASET_SCHEMA:
        raise ValueError(f'schema_version must be {DATASET_SCHEMA}.')
    _text(dataset.get('dataset_id'), 'dataset_id')
    if dataset.get('label_status') not in LABEL_STATUSES:
        raise ValueError('label_status must explicitly state its supported annotation-review status.')
    if 'evaluation_scope' in dataset:
        _text(dataset['evaluation_scope'], 'evaluation_scope')
    documents = dataset.get('documents')
    queries = dataset.get('queries')
    if not isinstance(documents, list) or not documents:
        raise ValueError('documents must be a nonempty list.')
    if not isinstance(queries, list) or not queries:
        raise ValueError('queries must be a nonempty list.')
    document_ids = set()
    for index, document in enumerate(documents):
        path = f'documents[{index}]'
        if not isinstance(document, dict):
            raise ValueError(f'{path} must be an object.')
        for field in ('id', 'text'):
            _text(document.get(field), f'{path}.{field}')
        if document['id'] in document_ids:
            raise ValueError(f'Duplicate document id: {document["id"]}.')
        document_ids.add(document['id'])
    query_ids = set()
    family_splits = {}
    for index, query in enumerate(queries):
        path = f'queries[{index}]'
        if not isinstance(query, dict):
            raise ValueError(f'{path} must be an object.')
        for field in ('id', 'text', 'category', 'language'):
            _text(query.get(field), f'{path}.{field}')
        if query['id'] in query_ids:
            raise ValueError(f'Duplicate query id: {query["id"]}.')
        query_ids.add(query['id'])
        if query.get('split') not in ('dev', 'test'):
            raise ValueError(f'{path}.split must be dev or test.')
        if 'family_id' in query:
            family = query['family_id']
            _text(family, f'{path}.family_id')
            if family in family_splits and family_splits[family] != query['split']:
                raise ValueError(f'Query family {family} must not cross dev and test splits.')
            family_splits[family] = query['split']
        if type(query.get('answerable')) is not bool:
            raise ValueError(f'{path}.answerable must be a boolean.')
        relevance = query.get('relevance')
        if not isinstance(relevance, dict):
            raise ValueError(f'{path}.relevance must be an object.')
        if bool(relevance) != query['answerable']:
            raise ValueError(f'{path}: answerable queries need qrels; unanswerable queries must have none.')
        for document_id, grade in relevance.items():
            _text(document_id, f'{path}.relevance document id')
            if document_id not in document_ids:
                raise ValueError(f'{path}.relevance references unknown document: {document_id}.')
            if type(grade) is not int or grade not in (1, 2):
                raise ValueError(f'{path}.relevance grades must be strict integers 1 or 2, not booleans.')
        if 'required_evidence_groups' in query:
            groups = query['required_evidence_groups']
            if not isinstance(groups, list):
                raise ValueError(f'{path}.required_evidence_groups must be a list.')
            if bool(groups) != query['answerable']:
                raise ValueError(f'{path}: answerable evidence groups must be nonempty; unanswerable groups must be empty.')
            for group in groups:
                if not isinstance(group, list) or not group:
                    raise ValueError(f'{path}: each required evidence group must be a nonempty list.')
                seen = set()
                for document_id in group:
                    _text(document_id, f'{path}.required_evidence_groups document id')
                    if document_id not in relevance:
                        raise ValueError(f'{path}: evidence group document {document_id} must belong to relevance.')
                    if document_id in seen:
                        raise ValueError(f'{path}: duplicate document within an evidence group: {document_id}.')
                    seen.add(document_id)


def _validate_ks(ks):
    if not isinstance(ks, (tuple, list)) or not ks:
        raise ValueError('ks must be a nonempty tuple or list of positive integers.')
    if any(type(k) is not int or k < 1 for k in ks) or len(set(ks)) != len(ks):
        raise ValueError('ks must contain distinct positive integers, not booleans.')
    return list(ks)


def _validate_rankings(dataset, rankings):
    if not isinstance(rankings, dict):
        raise ValueError('rankings_by_query must be an object of query ids to document-id lists.')
    query_ids = {query['id'] for query in dataset['queries']}
    document_ids = {document['id'] for document in dataset['documents']}
    for query_id, ranking in rankings.items():
        _text(query_id, 'ranking query id')
        if query_id not in query_ids:
            raise ValueError(f'Ranking references unknown query: {query_id}.')
        if not isinstance(ranking, list):
            raise ValueError(f'Ranking for {query_id} must be a list of document ids.')
        seen = set()
        for document_id in ranking:
            _text(document_id, f'Ranking for {query_id} document id')
            if document_id not in document_ids:
                raise ValueError(f'Ranking for {query_id} references unknown document: {document_id}.')
            if document_id in seen:
                raise ValueError(f'Ranking for {query_id} contains duplicate document: {document_id}.')
            seen.add(document_id)


def _query_result(query, rankings, ks):
    provided = query['id'] in rankings
    ranking = list(rankings.get(query['id'], []))
    groups = query.get('required_evidence_groups', [[document_id] for document_id in query['relevance']])
    result = {
        'query_id': query['id'],
        **{field: query[field] for field in GROUP_FIELDS},
        'answerable': query['answerable'],
        'ranking_status': 'provided' if provided else 'missing',
        'returned_document_ids': ranking,
        'relevant_document_count': len(query['relevance']),
        'required_evidence_groups': [list(group) for group in groups],
        'evidence_groups_source': 'explicit' if 'required_evidence_groups' in query else 'qrels_singletons',
        'metrics': None,
        'empty_return': None if query['answerable'] else provided and not ranking,
    }
    if not query['answerable']:
        return result
    relevance = query['relevance']
    ideal_grades = sorted(relevance.values(), reverse=True)
    metrics = {}
    for k in ks:
        top = ranking[:k]
        hits = sum(document_id in relevance for document_id in top)
        first_hit = next((rank for rank, document_id in enumerate(top, 1) if document_id in relevance), None)
        dcg = fsum((2 ** relevance.get(document_id, 0) - 1) / log2(rank + 1)
                   for rank, document_id in enumerate(top, 1))
        ideal = fsum((2 ** grade - 1) / log2(rank + 1) for rank, grade in enumerate(ideal_grades[:k], 1))
        covered = sum(bool(set(group).intersection(top)) for group in groups)
        values = (hits / len(relevance), hits / k, float(hits > 0),
                  1 / first_hit if first_hit else 0.0, dcg / ideal,
                  float(covered == len(groups)), covered / len(groups))
        metrics.update({f'{name}@{k}': value for name, value in zip(METRIC_NAMES, values)})
    result['metrics'] = metrics
    return result


def _mean(values):
    return fsum(values) / len(values) if values else None


def _summary(rows, ks):
    answerable = [row for row in rows if row['answerable']]
    unanswerable = [row for row in rows if not row['answerable']]
    missing = [row['query_id'] for row in rows if row['ranking_status'] == 'missing']
    provided_answerable = sum(row['ranking_status'] == 'provided' for row in answerable)
    provided_unanswerable = sum(row['ranking_status'] == 'provided' for row in unanswerable)
    return {
        'denominators': {
            'queries': len(rows), 'answerable_queries': len(answerable), 'unanswerable_queries': len(unanswerable),
            'provided_rankings': len(rows) - len(missing), 'missing_rankings': len(missing),
            'provided_answerable_rankings': provided_answerable, 'provided_unanswerable_rankings': provided_unanswerable,
        },
        'missing_query_ids': missing,
        'ranking_coverage': (len(rows) - len(missing)) / len(rows) if rows else None,
        'answerable_metrics': {
            f'{metric}@{k}': _mean([row['metrics'][f'{metric}@{k}'] for row in answerable])
            for k in ks for metric in METRIC_NAMES
        },
        'unanswerable': {
            'denominator': len(unanswerable),
            'empty_return_count': sum(row['empty_return'] for row in unanswerable),
            'empty_return_rate': _mean([float(row['empty_return']) for row in unanswerable]),
            'nonempty_return_count': sum(bool(row['returned_document_ids']) for row in unanswerable),
            'missing_ranking_count': len(unanswerable) - provided_unanswerable,
        },
    }


def _grouped(rows):
    for field in GROUP_FIELDS:
        grouped = defaultdict(list)
        for row in rows:
            grouped[row[field]].append(row)
        yield field, grouped


def evaluate_run(dataset, rankings_by_query, ks=(1, 3, 5, 10)):
    """Return per-query scores and macro averages with explicit denominators.

Missing query rankings receive zero retrieval scores (or failed unanswerable
abstention). Unknown or duplicated ranking identifiers instead reject the run.
"""
    validate_dataset(dataset)
    ks = _validate_ks(ks)
    _validate_rankings(dataset, rankings_by_query)
    rows = [_query_result(query, rankings_by_query, ks) for query in dataset['queries']]
    return {
        'schema_version': 'rag-evaluation-v1', 'dataset_id': dataset['dataset_id'],
        'label_status': dataset['label_status'], 'ks': ks, 'definitions': dict(DEFINITIONS),
        'evaluation_scope': dataset.get('evaluation_scope'),
        'annotation_boundary': ANNOTATION_BOUNDARIES[dataset['label_status']],
        'overall': _summary(rows, ks),
        'groups': {field: {value: _summary(group, ks) for value, group in grouped.items()}
                   for field, grouped in _grouped(rows)},
        'per_query': rows,
    }


def _percentile(sorted_values, fraction):
    position = (len(sorted_values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _paired_summary(rows, k, bootstrap_samples, seed):
    rows = [row for row in rows if row['answerable']]
    metric_keys = [f'{name}@{k}' for name in METRIC_NAMES]
    # Sample paired query indexes once per replicate, shared across all metrics.
    # Using the full query denominator prevents missing runs from disappearing.
    samples = {key: [] for key in metric_keys}
    if rows:
        rng = random.Random(seed)
        for _ in range(bootstrap_samples):
            indexes = [rng.randrange(len(rows)) for _ in rows]
            for key in metric_keys:
                samples[key].append(fsum(rows[index]['delta'][key] for index in indexes) / len(rows))
    metrics = {}
    for key in metric_keys:
        baseline = _mean([row['baseline'][key] for row in rows])
        candidate = _mean([row['candidate'][key] for row in rows])
        difference = _mean([row['delta'][key] for row in rows])
        sorted_samples = sorted(samples[key])
        interval = [_percentile(sorted_samples, .025), _percentile(sorted_samples, .975)] if rows else None
        metrics[key] = {
            'baseline_mean': baseline, 'candidate_mean': candidate, 'mean_difference': difference,
            'difference_pp': difference * 100 if difference is not None else None,
            'relative_improvement_pct': difference / baseline * 100 if baseline else None,
            'bootstrap_ci95': interval,
            'bootstrap_ci95_pp': [bound * 100 for bound in interval] if interval is not None else None,
        }
    return {
        'denominators': {
            'paired_answerable_queries': len(rows),
            'baseline_provided_rankings': sum(not row['baseline_missing'] for row in rows),
            'candidate_provided_rankings': sum(not row['candidate_missing'] for row in rows),
            'both_provided_rankings': sum(not row['baseline_missing'] and not row['candidate_missing'] for row in rows),
        },
        'query_ids': [row['query_id'] for row in rows], 'metrics': metrics,
    }


def compare_runs(dataset, baseline_rankings, candidate_rankings, k=5, bootstrap_samples=2000, seed=20260912):
    """Compare systems on the same answerable query set using paired bootstrap.

Intervals are query-level percentile bootstrap intervals, not hypothesis tests.
Synthetic/unreviewed annotations support exploratory comparisons only. Even a
human-reviewed dataset needs a defensible sampling design for generalization.
"""
    _validate_ks([k])
    if type(bootstrap_samples) is not int or bootstrap_samples < 1:
        raise ValueError('bootstrap_samples must be a positive integer, not a boolean.')
    if type(seed) is not int:
        raise ValueError('seed must be an integer, not a boolean.')
    baseline = evaluate_run(dataset, baseline_rankings, ks=[k])
    candidate = evaluate_run(dataset, candidate_rankings, ks=[k])
    rows = []
    for left, right in zip(baseline['per_query'], candidate['per_query']):
        row = {
            'query_id': left['query_id'], **{field: left[field] for field in GROUP_FIELDS},
            'answerable': left['answerable'],
            'baseline_missing': left['ranking_status'] == 'missing',
            'candidate_missing': right['ranking_status'] == 'missing',
            'baseline': left['metrics'], 'candidate': right['metrics'],
            'delta': {key: right['metrics'][key] - value for key, value in left['metrics'].items()} if left['answerable'] else None,
            'baseline_empty_return': left['empty_return'], 'candidate_empty_return': right['empty_return'],
        }
        rows.append(row)
    return {
        'schema_version': 'rag-comparison-v1', 'dataset_id': dataset['dataset_id'],
        'label_status': dataset['label_status'], 'k': k,
        'evaluation_scope': dataset.get('evaluation_scope'),
        'annotation_boundary': ANNOTATION_BOUNDARIES[dataset['label_status']],
        'method': {
            'paired_unit': 'query', 'bootstrap_samples': bootstrap_samples, 'seed': seed,
            'confidence_level': .95, 'ci_method': 'paired percentile bootstrap with linear interpolation',
            'inference_scope': INFERENCE_SCOPES[dataset['label_status']],
            'missing_rankings': 'Retained in the full paired answerable-query set as zero scores.',
            'groups': 'Groups are reported independently; dev and test are not interchangeable evaluation splits.',
        },
        'definitions': dict(DEFINITIONS),
        'coverage': {
            'queries': len(rows),
            'baseline_provided_rankings': baseline['overall']['denominators']['provided_rankings'],
            'candidate_provided_rankings': candidate['overall']['denominators']['provided_rankings'],
            'baseline_missing_query_ids': baseline['overall']['missing_query_ids'],
            'candidate_missing_query_ids': candidate['overall']['missing_query_ids'],
        },
        'overall': _paired_summary(rows, k, bootstrap_samples, seed),
        'groups': {field: {value: _paired_summary(group, k, bootstrap_samples, seed) for value, group in grouped.items()}
                   for field, grouped in _grouped(rows)},
        'unanswerable': {'baseline': baseline['overall']['unanswerable'], 'candidate': candidate['overall']['unanswerable']},
        'per_query_deltas': rows,
    }
