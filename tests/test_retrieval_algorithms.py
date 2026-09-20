"""Hand-constructed algorithm checks, separate from any retrieval evaluation set."""
import copy
import json
import math

import numpy as np
import pytest

from app.retrieval import (BM25_B, BM25_K1, RETRIEVAL_VERSION, STRATEGIES, TOKENIZER_VERSION,
                           bm25_scores, normalize_vectors, rank_chunks, retrieval_config, tokenize)


def chunk(identifier, text, vector, **metadata):
    return {'id': identifier, 'text': text, 'vector': vector, **metadata}


def ids(results):
    return [result['id'] for result in results]


def test_bounded_lexical_bonus_preserves_clear_semantic_advantage():
    def vector(cosine):
        return [cosine, math.sqrt(1 - cosine ** 2)]
    chunks = [chunk('english', 'transaction rollback', vector(.8)),
              chunk('lexical', '事务 事务 事务', vector(.65))]
    results = rank_chunks(chunks, '事务', [1, 0], strategy='hybrid_dense_lexical_v1')
    assert ids(results) == ['english', 'lexical']
    assert results[0]['score'] == pytest.approx(.8)
    assert results[1]['retrieval']['lexical_bonus'] == pytest.approx(.05)
    assert results[1]['retrieval']['selection_score'] == pytest.approx(.7)
    assert results[1]['retrieval']['mmr_relevance'] is None


def test_bounded_lexical_bonus_can_resolve_close_semantic_matches():
    chunks = [chunk('general', 'memory allocation', [.7, math.sqrt(1 - .7 ** 2)]),
              chunk('exact', 'malloc', [.69, math.sqrt(1 - .69 ** 2)])]
    results = rank_chunks(chunks, 'malloc', [1, 0], strategy='hybrid_dense_lexical_v1')
    assert ids(results) == ['exact', 'general']
    assert results[0]['score'] == pytest.approx(.69)  # Public score remains cosine.
    assert results[0]['retrieval']['selection_score'] == pytest.approx(.74)


def test_bounded_lexical_zero_match_matches_dense_and_records_fallback():
    chunks = [chunk('a', 'cache', [.8, .6]), chunk('b', 'thread', [.6, .8])]
    results = rank_chunks(chunks, '事务', [1, 0], strategy='hybrid_dense_lexical_v1')
    assert ids(results) == ids(rank_chunks(chunks, '事务', [1, 0]))
    assert all(r['retrieval']['effective_strategy'] == 'dense_v1' for r in results)
    assert all(r['retrieval']['lexical_bonus'] is None for r in results)


def test_dense_preserves_cosine_ranking_metadata_and_input():
    chunks = [chunk('a', '甲', [3, 4], page=2, document_name='课程.md'),
              chunk('b', '乙', [-3, 4]), chunk('c', '丙', [0, 2])]
    original = copy.deepcopy(chunks)
    results = rank_chunks(chunks, '', [1, 0], top_k=3)
    assert ids(results) == ['a', 'c', 'b']
    assert [result['score'] for result in results] == pytest.approx([.6, 0., -.6])
    assert chunks == original
    assert results[0]['page'] == 2 and results[0]['document_name'] == '课程.md'
    assert all('vector' not in result for result in results)
    assert [result['retrieval']['dense_rank'] for result in results] == [1, 2, 3]
    assert all(result['retrieval']['fusion_score'] is None for result in results)


def test_dense_matches_previous_non_tied_normalization_and_sort():
    vectors = np.array([[1e308, 1e307, 1e306], [1, 2, 4], [-2, 5, 1], [7, -1, .5]])
    query = np.array([[3., 1., -2.]])

    def old_normalized(values):
        matrix = np.asarray(values, dtype=float)
        matrix = matrix / np.max(np.abs(matrix), axis=1)[:, None]
        return matrix / np.linalg.norm(matrix, axis=1)[:, None]

    old_scores = np.clip(old_normalized(vectors) @ old_normalized(query)[0], -1., 1.)
    expected = np.argsort(-old_scores)
    results = rank_chunks([chunk(str(i), '', vector) for i, vector in enumerate(vectors.tolist())],
                          '', query[0].tolist(), top_k=4)
    assert ids(results) == [str(index) for index in expected]
    assert [result['score'] for result in results] == pytest.approx(old_scores[expected])


def test_dense_runtime_does_not_include_unused_lexical_work(monkeypatch):
    def forbidden(*args):
        pytest.fail('Dense baseline must not run BM25 merely to fill metadata')

    monkeypatch.setattr('app.retrieval.bm25_scores', forbidden)
    result = rank_chunks([chunk('a', 'cache', [1])], 'cache', [1])[0]
    assert result['retrieval']['lexical_score'] is None
    assert result['retrieval']['lexical_rank'] is None


@pytest.mark.parametrize('strategy', STRATEGIES)
def test_ties_preserve_input_order_and_output_is_json_safe(strategy):
    chunks = [chunk('z', 'cache', [1, 0]), chunk('a', 'cache', [1, 0]), chunk('m', 'cache', [1, 0])]
    results = rank_chunks(chunks, 'cache', [1, 0], strategy=strategy, top_k=3)
    assert ids(results) == ['z', 'a', 'm']
    assert json.loads(json.dumps(results, allow_nan=False)) == results


def test_tokenizer_has_exact_versioned_chinese_and_code_behavior():
    assert tokenize('中文 KV_Cache C++ ＡＰＩ v4.1flash /api/run') == [
        '中', '文', '中文', 'kv_cache', 'kv', 'cache', 'c++', 'c', 'api',
        'v4.1flash', 'v4', '1flash', 'api/run', 'api', 'run',
    ]
    assert tokenize('检索！缓存') == ['检', '索', '检索', '缓', '存', '缓存']
    assert tokenize(' \\ \n 😀？！') == []
    assert tokenize('A') == ['a']
    assert tokenize('难') == ['难']
    assert TOKENIZER_VERSION == 'nfkc-casefold-cjk-unigram-bigram-code-v1'


def test_bm25_has_hand_calculated_positive_idf_and_term_frequency():
    # All documents have length 2, so length normalization is exactly 1.
    texts = ['cache cache', 'cache other', 'other other']
    inverse_frequency = math.log(1 + (3 - 2 + .5) / (2 + .5))
    expected = [inverse_frequency * 2 * 2.2 / 3.2, inverse_frequency, 0.0]
    assert bm25_scores(texts, 'cache') == pytest.approx(expected)
    assert bm25_scores(texts, 'cache cache cache') == pytest.approx(expected)
    results = rank_chunks([chunk(str(i), text, [1, 0]) for i, text in enumerate(texts)],
                          'cache', [0, 1], strategy='bm25_v1', top_k=3)
    assert ids(results) == ['0', '1']  # Zero lexical scores never enter the list.
    assert [result['retrieval']['lexical_score'] for result in results] == pytest.approx(expected[:2])
    assert [result['score'] for result in results] == [0.0, 0.0]  # Still cosine, never BM25.
    assert results[0]['retrieval']['selection_score_semantics'] == 'bm25_score'


def test_bm25_length_normalization_favors_concise_matching_document():
    scores = bm25_scores(['cache', 'cache filler filler filler', ''], 'cache')
    assert scores[0] > scores[1] > scores[2] == 0


def test_chinese_bigrams_and_code_components_are_searchable():
    texts = ['缓存策略使用 KV_CACHE', '检索算法使用 BM25', '随机说明']
    assert bm25_scores(texts, '缓存')[0] > 0
    assert bm25_scores(texts, '缓存')[1:] == [0, 0]
    assert bm25_scores(texts, 'kv cache')[0] > 0
    assert bm25_scores(texts, 'bm25')[1] > 0


def test_rrf_scores_match_reciprocal_rank_sum():
    chunks = [chunk('dense-only', 'unrelated', [1, 0]),
              chunk('both-first-lexical', 'cache cache', [.8, .6]),
              chunk('both-second-lexical', 'cache other', [0, 1])]
    results = rank_chunks(chunks, 'cache', [1, 0], strategy='hybrid_rrf_v1', top_k=3)
    assert ids(results) == ['both-first-lexical', 'both-second-lexical', 'dense-only']
    assert [result['retrieval']['fusion_score'] for result in results] == pytest.approx([
        1 / 62 + 1 / 61, 1 / 63 + 1 / 62, 1 / 61,
    ])
    assert [result['score'] for result in results] == pytest.approx([.8, 0., 1.])
    assert results[0]['retrieval']['dense_rank'] == 2
    assert results[0]['retrieval']['lexical_rank'] == 1


def test_fusion_combines_complementary_candidates_outside_other_route():
    chunks = [chunk('dense-only', 'semantic paraphrase', [1, 0]),
              chunk('dense-runner-up', 'different', [.8, .6]),
              chunk('lexical-only', 'exact_symbol', [-1, 0])]
    dense = rank_chunks(chunks, 'exact_symbol', [1, 0], top_k=2, candidate_k=2)
    hybrid = rank_chunks(chunks, 'exact_symbol', [1, 0], strategy='hybrid_rrf_v1',
                         top_k=2, candidate_k=2)
    assert ids(dense) == ['dense-only', 'dense-runner-up']
    assert ids(hybrid) == ['dense-only', 'lexical-only']
    assert hybrid[1]['retrieval']['dense_rank'] == 3
    assert hybrid[1]['retrieval']['fusion_score'] == pytest.approx(1 / 61)


def test_mmr_suppresses_duplicate_embedding_in_favor_of_relevant_alternative():
    chunks = [chunk('first', 'cache', [1, 0]), chunk('duplicate', 'cache', [1, 0]),
              chunk('alternative', 'cache', [.8, .6])]
    fused = rank_chunks(chunks, 'cache', [1, 0], strategy='hybrid_rrf_v1', top_k=2)
    diverse = rank_chunks(chunks, 'cache', [1, 0], strategy='hybrid_mmr_v1', top_k=2)
    assert ids(fused) == ['first', 'duplicate']
    assert ids(diverse) == ['first', 'alternative']
    # Alternative has both ranks 3; max fused score belongs to rank-1 first.
    expected_relevance = (2 / 63) / (2 / 61)
    assert diverse[1]['retrieval']['mmr_relevance'] == pytest.approx(expected_relevance)
    assert diverse[1]['retrieval']['mmr_redundancy'] == pytest.approx(.8)
    assert diverse[1]['retrieval']['selection_score'] == pytest.approx(.7 * expected_relevance - .3 * .8)


def test_mmr_lambda_one_uses_only_fusion_relevance():
    chunks = [chunk('first', 'cache', [1, 0]), chunk('duplicate', 'cache', [1, 0]),
              chunk('alternative', 'cache', [.8, .6])]
    assert ids(rank_chunks(chunks, 'cache', [1, 0], strategy='hybrid_mmr_v1',
                           top_k=2, mmr_lambda=1)) == ['first', 'duplicate']


def test_mmr_does_not_reward_negative_cosine_redundancy():
    chunks = [chunk('positive', 'cache', [1, 0]), chunk('opposite', 'cache', [-1, 0])]
    results = rank_chunks(chunks, 'cache', [1, 0], strategy='hybrid_mmr_v1', top_k=2)
    assert results[1]['retrieval']['mmr_redundancy'] == 0.0


@pytest.mark.parametrize('query', ['', '?! 😀', 'absentword'])
@pytest.mark.parametrize('strategy', ['hybrid_rrf_v1', 'hybrid_mmr_v1'])
def test_no_positive_lexical_matches_falls_back_to_original_dense(query, strategy):
    chunks = [chunk('first', 'cache', [1, 0]), chunk('second', 'retrieval', [0, 1])]
    results = rank_chunks(chunks, query, [0, 1], strategy=strategy, top_k=2)
    assert ids(results) == ['second', 'first']
    assert [result['score'] for result in results] == [1.0, 0.0]
    for result in results:
        assert result['retrieval']['strategy'] == strategy
        assert result['retrieval']['effective_strategy'] == 'dense_v1'
        assert result['retrieval']['fallback_reason'] == 'no_positive_lexical_matches'
        assert result['retrieval']['fusion_score'] is None
        assert result['retrieval']['selection_score_semantics'] == 'query_chunk_cosine'


@pytest.mark.parametrize('strategy', STRATEGIES)
def test_empty_corpus_and_empty_text_are_defined(strategy):
    assert rank_chunks([], '', [1], strategy=strategy) == []
    result = rank_chunks([chunk('a', '', [1])], '', [1], strategy=strategy)
    assert ids(result) == ([] if strategy == 'bm25_v1' else ['a'])
    assert bm25_scores([], 'query') == []
    assert bm25_scores(['', ' '], 'query') == [0., 0.]


@pytest.mark.parametrize('values', [
    [], [[]], [[0, 0]], [[float('nan'), 1]], [[float('inf'), 1]], [[float('-inf'), 1]],
    [[1, 2], [1]], [['1', '2']], [[True, 1.]], [[1 + 1j]], [1, 2], None,
    [[10**500, 1]],
])
def test_normalization_rejects_invalid_and_coerced_vectors(values):
    with pytest.raises(ValueError, match='向量'):
        normalize_vectors(values)


def test_large_finite_and_subnormal_vectors_normalize_without_overflow():
    result = normalize_vectors([[1e308, 1e308], [5e-324, 5e-324]])
    assert np.isfinite(result).all()
    assert result.tolist() == pytest.approx(np.array([[1 / math.sqrt(2)] * 2] * 2))


@pytest.mark.parametrize('parameters', [
    {'strategy': 'unknown'}, {'strategy': []}, {'top_k': 0}, {'top_k': True},
    {'top_k': 1.5}, {'top_k': '5'}, {'candidate_k': 0}, {'candidate_k': 2, 'top_k': 3},
    {'rrf_k': 0}, {'rrf_k': 1.5}, {'rrf_k': 10**500},
    {'mmr_lambda': -1}, {'mmr_lambda': 1.01}, {'mmr_lambda': float('nan')},
    {'mmr_lambda': float('inf')}, {'mmr_lambda': True}, {'mmr_lambda': '0.7'},
    {'mmr_lambda': 10**500},
])
def test_invalid_parameters_fail_before_ranking(parameters):
    with pytest.raises(ValueError):
        rank_chunks([chunk('a', 'cache', [1, 0])], 'cache', [1, 0], **parameters)


@pytest.mark.parametrize('chunks, query, query_vector', [
    ([chunk('a', 'x', [1, 0]), chunk('a', 'y', [0, 1])], 'x', [1, 0]),
    ([chunk('', 'x', [1, 0])], 'x', [1, 0]),
    ([chunk('a', 123, [1, 0])], 'x', [1, 0]),
    ([{'id': 'a', 'text': 'x'}], 'x', [1, 0]),
    ([chunk('a', 'x', [1, 0])], 'x', [1]),
    ([chunk('a', 'x', [1, 0])], 'x', [0, 0]),
    ([chunk('a', 'x', [1, 0])], None, [1, 0]),
    (None, 'x', [1, 0]),
])
def test_bad_chunk_identity_shape_or_query_is_rejected(chunks, query, query_vector):
    with pytest.raises(ValueError):
        rank_chunks(chunks, query, query_vector)


def test_configuration_snapshot_identifies_fixed_parameters_and_score_rules():
    config = retrieval_config(strategy='hybrid_mmr_v1', top_k=10)
    assert config['version'] == RETRIEVAL_VERSION
    assert config['tokenizer_version'] == TOKENIZER_VERSION
    assert config['candidate_k'] == 20 and config['rrf_k'] == 60
    assert config['bm25_k1'] == BM25_K1 == 1.2 and config['bm25_b'] == BM25_B == .75
    assert config['mmr_lambda'] == .7 and config['top_k'] == 10
    assert config['tie_break'] == 'input_order'
    assert config['score_semantics'] == 'original_query_chunk_cosine'
    assert json.loads(json.dumps(config, allow_nan=False)) == config
