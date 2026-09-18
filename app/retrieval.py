"""Deterministic local retrieval baselines; never invokes a provider.

The parameters are predefined experiment candidates, not tuned on an evaluation
set. All strategies preserve the original cosine in ``score``. The separate
``retrieval`` object records what actually determined selection.
"""
from collections import Counter
import math
import re
import unicodedata

import numpy as np


RETRIEVAL_VERSION = 'retrieval-v2'
TOKENIZER_VERSION = 'nfkc-casefold-cjk-unigram-bigram-code-v1'
STRATEGIES = ('dense_v1', 'bm25_v1', 'hybrid_rrf_v1', 'hybrid_mmr_v1', 'hybrid_dense_lexical_v1')
BM25_K1 = 1.2
BM25_B = 0.75
DEFAULT_CANDIDATE_K = 20
DEFAULT_RRF_K = 60
DEFAULT_MMR_LAMBDA = 0.7
LEXICAL_BONUS_CAP = 0.05
METADATA_ENCODING_VERSION = 'source_heading_keywords_unique_tokens_v1'

_CJK = r'\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f'
_TOKEN = re.compile(rf'[{_CJK}]+|[a-z0-9_]+(?:[./:\-][a-z0-9_]+)*(?:\+\+|#)?')
_CODE_PART = re.compile(r'[a-z0-9]+')


def tokenize(text):
    """NFKC/casefold, CJK character unigrams + adjacent bigrams, code terms.

    ASCII words/numbers and dotted, slashed, colon, hyphen, underscore, C++/C#
    terms retain their complete token plus distinct component tokens. No stop
    words, stemming, synonyms, or document/query-specific instructions are used.
    """
    if not isinstance(text, str):
        raise ValueError('检索文本必须是字符串。')
    tokens = []
    for match in _TOKEN.finditer(unicodedata.normalize('NFKC', text).casefold()):
        word = match.group()
        if re.fullmatch(rf'[{_CJK}]+', word):
            tokens.extend(word)
            tokens.extend(word[index:index + 2] for index in range(len(word) - 1))
        else:
            tokens.append(word)
            tokens.extend(part for part in dict.fromkeys(_CODE_PART.findall(word)) if part != word)
    return tokens


def normalize_vectors(values):
    """Validate nonzero finite real vectors and return stable unit rows.

    Scaling each row before its norm retains the existing dense baseline's
    overflow protection (including valid values close to 1e308).
    """
    try:
        if any(isinstance(value, (bool, np.bool_)) for row in values for value in row):
            raise ValueError()
        matrix = np.asarray(values)
        if matrix.ndim != 2 or min(matrix.shape) < 1 or matrix.dtype.kind not in 'iuf':
            raise ValueError()
        matrix = matrix.astype(float)
        if not np.isfinite(matrix).all():
            raise ValueError()
        scales = np.max(np.abs(matrix), axis=1)
        if (scales == 0).any():
            raise ValueError()
        scaled = matrix / scales[:, None]
        return scaled / np.linalg.norm(scaled, axis=1)[:, None]
    except (TypeError, ValueError, OverflowError):
        raise ValueError('检索向量必须为维度一致、非零、有限实数的二维数组。') from None


def retrieval_config(*, strategy='dense_v1', top_k=5,
                     candidate_k=DEFAULT_CANDIDATE_K, rrf_k=DEFAULT_RRF_K,
                     mmr_lambda=DEFAULT_MMR_LAMBDA, metadata_weight=0.0):
    """Validate parameters and expose a serializable, fully specified snapshot."""
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ValueError('未知检索策略。')
    def finite_number(value):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False

    if any(type(value) is not int or value < 1 or not finite_number(value)
           for value in (top_k, candidate_k, rrf_k)):
        raise ValueError('检索 top_k、candidate_k 和 rrf_k 必须是正整数。')
    if candidate_k < top_k:
        raise ValueError('候选片段数 candidate_k 不得小于 top_k。')
    if (not finite_number(mmr_lambda)
            or not 0 <= mmr_lambda <= 1):
        raise ValueError('MMR 权重必须是 0 到 1 之间的有限实数。')
    if not finite_number(metadata_weight) or not 0 <= metadata_weight <= 1:
        raise ValueError('元数据权重必须是 0 到 1 之间的有限实数。')
    if metadata_weight > 0 and strategy != 'hybrid_dense_lexical_v1':
        raise ValueError('元数据词汇加分只支持 hybrid_dense_lexical_v1 策略。')
    config = {
        'version': RETRIEVAL_VERSION, 'strategy': strategy,
        'tokenizer_version': TOKENIZER_VERSION,
        'top_k': top_k, 'candidate_k': candidate_k, 'rrf_k': rrf_k,
        'mmr_lambda': float(mmr_lambda), 'bm25_k1': BM25_K1, 'bm25_b': BM25_B,
        'query_term_frequency': 'binary',
        'candidate_pool': 'top_candidate_k_per_route_then_top_candidate_k_fused_for_mmr',
        'mmr_relevance': 'rrf_score_divided_by_max_candidate_rrf_score',
        'mmr_redundancy': 'maximum_nonnegative_cosine_to_already_selected_chunks',
        'dense_lexical_bonus_cap': LEXICAL_BONUS_CAP,
        'dense_lexical_score': 'cosine_plus_capped_max_normalized_bm25',
        'tie_break': 'input_order',
        'no_lexical_matches': 'dense_fallback_for_hybrid_empty_for_bm25',
        'score_semantics': 'original_query_chunk_cosine',
    }
    # An explicit zero is exactly the frozen v2 configuration and behavior.
    if metadata_weight > 0:
        config.update({
            'metadata_weight': float(metadata_weight),
            'metadata_encoding_version': METADATA_ENCODING_VERSION,
            'metadata_blending': 'convex_body_and_metadata_max_normalized_bm25_within_existing_bonus_cap',
            'metadata_no_matches': 'effective_metadata_weight_zero_preserves_body_bonus',
            'metadata_term_frequency': 'binary_across_heading_path_and_keywords',
        })
    return config


def bm25_scores(texts, query):
    """Positive-IDF BM25, k1=1.2, b=.75; query terms counted once.

    idf(t) = log(1 + (N - df(t) + .5) / (df(t) + .5)).
    Empty/untokenizable documents remain part of corpus N and average length.
    """
    query_terms = set(tokenize(query))
    documents = [Counter(tokenize(text)) for text in texts]
    return _bm25_token_scores(documents, query_terms)


def _bm25_token_scores(documents, query_terms):
    """Shared arithmetic, also allowing metadata to have binary term counts."""
    scores = [0.0] * len(documents)
    if not documents or not query_terms:
        return scores
    lengths = [sum(document.values()) for document in documents]
    average_length = sum(lengths) / len(documents)
    if average_length == 0:
        return scores
    frequencies = Counter(term for document in documents for term in document)
    # Sorted terms also make floating-point accumulation independent of hash seed.
    for term in sorted(query_terms):
        frequency = frequencies[term]
        if not frequency:
            continue
        inverse_frequency = math.log1p((len(documents) - frequency + .5) / (frequency + .5))
        for index, document in enumerate(documents):
            count = document[term]
            if count:
                denominator = count + BM25_K1 * (1 - BM25_B + BM25_B * lengths[index] / average_length)
                scores[index] += inverse_frequency * count * (BM25_K1 + 1) / denominator
    return scores


def _metadata_terms(chunk):
    """Only source-derived heading/keyword fields; duplicate terms count once.

    Extraction and provenance verification belong to the ingestion stage. This
    ranker neither generates keywords nor treats keyword hits as evidence of
    semantic sufficiency. Other metadata fields never enter lexical scoring.
    """
    metadata = chunk.get('metadata')
    if metadata is None:
        return Counter()
    if not isinstance(metadata, dict):
        raise ValueError('片段 metadata 必须是对象或空值。')
    terms = set()
    for field in ('heading_path', 'keywords'):
        values = metadata.get(field)
        if values is None:
            continue
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError('metadata.heading_path 和 metadata.keywords 必须是字符串或字符串列表。')
        for value in values:
            terms.update(tokenize(value))
    return Counter({term: 1 for term in sorted(terms)})


def rank_chunks(chunks, query, query_vector, *, strategy='dense_v1', top_k=5,
                candidate_k=DEFAULT_CANDIDATE_K, rrf_k=DEFAULT_RRF_K,
                mmr_lambda=DEFAULT_MMR_LAMBDA, metadata_weight=0.0):
    """Rank cached chunks, preserving metadata and dropping the vector payload.

    Required chunk fields: unique nonempty ``id``, string ``text``, ``vector``.
    Dense/lexical ranks are one-based ranks over the full eligible corpus (only
    positive BM25 scores receive a lexical rank). The dense baseline skips BM25
    entirely and records null lexical scores, preserving its real runtime cost.
    Fusion only uses each route's
    first candidate_k entries; MMR then examines the best candidate_k fused
    entries. Missing ranks/fusion values are null, never fabricated zeros.

    Optional metadata weighting is restricted to the dense-plus-capped-lexical
    strategy. It blends independently normalized body and source-metadata BM25;
    their total cosine bonus remains <= .05. Zero does not read metadata or add
    diagnostics, preserving frozen experiments exactly. Original text, metadata,
    embeddings, and the cosine exposed in score are never changed.
    """
    config = retrieval_config(strategy=strategy, top_k=top_k, candidate_k=candidate_k,
                              rrf_k=rrf_k, mmr_lambda=mmr_lambda, metadata_weight=metadata_weight)
    if not isinstance(chunks, list) or not isinstance(query, str):
        raise ValueError('检索片段必须是列表，查询必须是字符串。')
    query_matrix = normalize_vectors([query_vector])
    ids = set()
    for chunk in chunks:
        if (not isinstance(chunk, dict) or not isinstance(chunk.get('id'), str)
                or not chunk['id'].strip() or not isinstance(chunk.get('text'), str)
                or 'vector' not in chunk):
            raise ValueError('检索片段必须包含有效 id、text 和 vector。')
        if chunk['id'] in ids:
            raise ValueError('检索片段 id 必须唯一。')
        ids.add(chunk['id'])
    if not chunks:
        return []
    vectors = normalize_vectors([chunk['vector'] for chunk in chunks])
    if vectors.shape[1] != query_matrix.shape[1]:
        raise ValueError('查询向量与资料向量维度不一致。')
    dense = np.clip(vectors @ query_matrix[0], -1., 1.).tolist()
    dense_order = sorted(range(len(chunks)), key=lambda index: (-dense[index], index))
    dense_ranks = {index: rank for rank, index in enumerate(dense_order, 1)}
    if strategy == 'dense_v1':
        lexical = [None] * len(chunks)
        lexical_order = []
    else:
        lexical = bm25_scores([chunk['text'] for chunk in chunks], query)
        lexical_order = sorted((index for index in range(len(chunks)) if lexical[index] > 0),
                               key=lambda index: (-lexical[index], index))
    lexical_ranks = {index: rank for rank, index in enumerate(lexical_order, 1)}
    candidate_lexical_order = lexical_order
    metadata_diagnostics = {}
    if metadata_weight > 0:
        metadata_scores = _bm25_token_scores([_metadata_terms(chunk) for chunk in chunks], set(tokenize(query)))
        body_max, metadata_max = max(lexical), max(metadata_scores)
        body_normalized = [score / body_max if body_max else 0.0 for score in lexical]
        metadata_normalized = [score / metadata_max if metadata_max else 0.0 for score in metadata_scores]
        effective_metadata_weight = float(metadata_weight) if metadata_max > 0 else 0.0
        body_contribution = [(1 - effective_metadata_weight) * score for score in body_normalized]
        metadata_contribution = [effective_metadata_weight * score for score in metadata_normalized]
        combined_lexical = [body + metadata for body, metadata in zip(body_contribution, metadata_contribution)]
        candidate_lexical_order = sorted((index for index, score in enumerate(combined_lexical) if score > 0),
                                         key=lambda index: (-combined_lexical[index], index))
        combined_ranks = {index: rank for rank, index in enumerate(candidate_lexical_order, 1)}
        metadata_diagnostics = {index: {
            'metadata_encoding_version': METADATA_ENCODING_VERSION,
            'metadata_weight': float(metadata_weight),
            'effective_metadata_weight': effective_metadata_weight,
            'metadata_lexical_score': metadata_scores[index],
            'normalized_metadata_lexical_score': metadata_normalized[index],
            'normalized_body_lexical_score': body_normalized[index],
            'body_lexical_bonus': LEXICAL_BONUS_CAP * body_contribution[index],
            'metadata_lexical_bonus': LEXICAL_BONUS_CAP * metadata_contribution[index],
            'combined_lexical_rank': combined_ranks.get(index),
            'combined_lexical_bonus': LEXICAL_BONUS_CAP * combined_lexical[index],
        } for index in range(len(chunks))}
    fusion = {}
    selection_scores = {}
    relevance = {}
    redundancies = {}
    effective_strategy = strategy
    fallback_reason = None

    if strategy == 'dense_v1':
        selected = dense_order[:top_k]
        selection_scores = {index: dense[index] for index in selected}
    elif strategy == 'bm25_v1':
        selected = lexical_order[:top_k]
        selection_scores = {index: lexical[index] for index in selected}
    elif not candidate_lexical_order:
        selected = dense_order[:top_k]
        selection_scores = {index: dense[index] for index in selected}
        effective_strategy = 'dense_v1'
        fallback_reason = 'no_positive_lexical_matches'
    elif strategy == 'hybrid_dense_lexical_v1':
        # Unlike rank fusion, a second route cannot override an arbitrary
        # semantic gap. A cosine advantage > .05 survives any lexical bonus.
        candidates = set(dense_order[:candidate_k]) | set(candidate_lexical_order[:candidate_k])
        if metadata_weight > 0:
            relevance = {index: combined_lexical[index] for index in candidates}
        else:
            lexical_max = max(lexical)
            relevance = {index: lexical[index] / lexical_max for index in candidates}
        scores = {index: dense[index] + LEXICAL_BONUS_CAP * relevance[index] for index in candidates}
        selected = sorted(candidates, key=lambda index: (-scores[index], index))[:top_k]
        selection_scores = {index: scores[index] for index in selected}
    else:
        for ranking in (dense_order[:candidate_k], lexical_order[:candidate_k]):
            for rank, index in enumerate(ranking, 1):
                fusion[index] = fusion.get(index, 0.0) + 1 / (rrf_k + rank)
        fused_order = sorted(fusion, key=lambda index: (-fusion[index], index))
        if strategy == 'hybrid_rrf_v1':
            selected = fused_order[:top_k]
            selection_scores = {index: fusion[index] for index in selected}
        else:
            candidates = fused_order[:candidate_k]
            largest_fusion = max(fusion[index] for index in candidates)
            relevance = {index: fusion[index] / largest_fusion for index in candidates}
            selected = []
            while candidates and len(selected) < top_k:
                candidate_redundancy = {
                    index: (max(0.0, float(np.max(np.clip(vectors[selected] @ vectors[index], -1., 1.))))
                            if selected else 0.0)
                    for index in candidates
                }
                candidate_scores = {index: mmr_lambda * relevance[index] -
                                    (1 - mmr_lambda) * candidate_redundancy[index]
                                    for index in candidates}
                chosen = max(candidates, key=lambda index: (candidate_scores[index], -index))
                selected.append(chosen)
                candidates.remove(chosen)
                selection_scores[chosen] = candidate_scores[chosen]
                redundancies[chosen] = candidate_redundancy[chosen]

    score_semantics = {
        'dense_v1': 'query_chunk_cosine', 'bm25_v1': 'bm25_score',
        'hybrid_rrf_v1': 'reciprocal_rank_fusion_score',
        'hybrid_mmr_v1': 'lambda_normalized_fusion_minus_one_minus_lambda_redundancy',
        'hybrid_dense_lexical_v1': 'query_chunk_cosine_plus_capped_lexical_bonus',
    }
    return [
        {key: value for key, value in chunks[index].items() if key not in ('vector', 'score', 'retrieval')} |
        {'score': dense[index], 'retrieval': {
            'version': config['version'], 'tokenizer_version': TOKENIZER_VERSION,
            'strategy': strategy, 'effective_strategy': effective_strategy,
            'fallback_reason': fallback_reason, 'dense_score': dense[index],
            'lexical_score': lexical[index], 'dense_rank': dense_ranks[index],
            'lexical_rank': lexical_ranks.get(index), 'fusion_score': fusion.get(index),
            'selection_rank': rank, 'selection_score': selection_scores[index],
            'selection_score_semantics': score_semantics[effective_strategy],
            'mmr_relevance': relevance.get(index) if strategy == 'hybrid_mmr_v1' else None,
            'mmr_redundancy': redundancies.get(index),
            'normalized_lexical_score': relevance.get(index) if strategy == 'hybrid_dense_lexical_v1' else None,
            'lexical_bonus': LEXICAL_BONUS_CAP * relevance[index] if strategy == 'hybrid_dense_lexical_v1' and index in relevance else None,
        } | metadata_diagnostics.get(index, {})}
        for rank, index in enumerate(selected, 1)
    ]
