"""Job-private candidate indexes, before evidence selection and course promotion.

Embeddings use the ordinary provider ledger. Candidates never become documents
or searchable course data here. Only complete, unchanged same-job indexes can be
reused; an interrupted attempt is not silently repeated.
"""
from collections import OrderedDict
import hashlib
import json
import re

from .document_storage import parsed_chunks
from .rag_candidates import query_input
from .rag_runtime import document_input, encoding_configuration
from .retrieval import normalize_vectors, rank_chunks
from .store import dumps, now


VERSION = 'candidate_index_gap_retrieval_v2'
NEIGHBOR_POLICY = 'same_section_next_then_previous_v1'
EMBED_BATCH_SIZE = 16
MAX_SOURCE_CHUNKS = 100
MAX_GAPS = 6
GAP_TOP_K = 4
CANDIDATE_K = 20
MAX_SELECTED_CHUNKS = 24
MAX_PASSAGES = 48
MAX_PASSAGE_CHARS = 900
MAX_TOTAL_PASSAGE_CHARS = 24000


class StagingError(ValueError):
    def __init__(self, code):
        self.code = code
        messages = {'source_too_broad': '候选资料超过临时索引的片段上限。',
            'invalid_vectors': '候选资料的嵌入向量数量或维度无效。',
            'stage_interrupted': '候选资料临时索引曾中断，请新建任务。',
            'stage_configuration_changed': '临时索引配置已变化，请新建任务。',
            'invalid_stage': '候选资料临时索引记录无效，请新建任务。',
            'invalid_source_coordinates': '候选资料的索引片段与原文坐标不一致。',
            'invalid_gaps': '候选资料检索需要有效的公开知识需求。',
            'invalid_source': '候选资料不满足临时索引要求。'}
        super().__init__(messages.get(code, messages['invalid_stage']))


def configuration():
    return {'version': VERSION, 'batch_size': EMBED_BATCH_SIZE, 'max_chunks': MAX_SOURCE_CHUNKS,
        'max_gaps': MAX_GAPS, 'strategy': 'hybrid_rrf_v1', 'top_k_per_gap': GAP_TOP_K,
        'candidate_k': CANDIDATE_K, 'max_selected_chunks': MAX_SELECTED_CHUNKS,
        'neighbor_policy': NEIGHBOR_POLICY, 'neighbor_radius': 1,
        'max_passages': MAX_PASSAGES, 'max_passage_chars': MAX_PASSAGE_CHARS,
        'max_total_passage_chars': MAX_TOTAL_PASSAGE_CHARS}


def _scope(pipeline, request, job_id):
    job = pipeline.store.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    try:
        payload = json.loads(job['payload']) if job else None
    except (TypeError, ValueError):
        payload = None
    if (not job or job['kind'] != 'generate' or not isinstance(payload, dict)
            or payload.get('course_id') != request.course_id
            or not pipeline.store.one('SELECT id FROM courses WHERE id=?', (request.course_id,))):
        raise StagingError('invalid_stage')
    pipeline.authorize_job(job, payload)
    pipeline.authorize_job(job, request.model_dump())


def _queries(gaps, pipeline):
    if not isinstance(gaps, list) or not 1 <= len(gaps) <= MAX_GAPS:
        raise StagingError('invalid_gaps')
    result, seen = [], set()
    keys = [getattr(getattr(pipeline.settings, capability, None), 'api_key', '')
            for capability in ('text', 'embedding', 'vision', 'rerank', 'speech', 'image')]
    keys.append(getattr(pipeline.settings, 'exploration_search_api_key', ''))
    for gap in gaps:
        if not isinstance(gap, dict): raise StagingError('invalid_gaps')
        identity, need, queries = gap.get('id'), gap.get('need'), gap.get('queries', [])
        if (not isinstance(identity, str) or not identity or len(identity) > 64 or identity in seen
                or not isinstance(need, str) or not 2 <= len(need.strip()) <= 300
                or not isinstance(queries, list) or len(queries) > 2):
            raise StagingError('invalid_gaps')
        pieces = [need.strip()]
        for query in queries:
            text = query.get('query') if isinstance(query, dict) else None
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 240:
                raise StagingError('invalid_gaps')
            pieces.append(text.strip())
        public = ' | '.join(dict.fromkeys(pieces))
        if (re.search(r'https?://|www\.|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b(?:sk-|bearer\s|api[_ -]?key\s*[:=])', public, re.I)
                or any(ord(char) < 32 for char in public) or any(key and key in public for key in keys)):
            raise StagingError('invalid_gaps')
        result.append({'gap_id': identity, 'query': public}); seen.add(identity)
    return result


def _vectors(values, count, dimension=None):
    try:
        matrix = normalize_vectors(values)
        if matrix.shape[0] != count or dimension is not None and matrix.shape[1] != dimension:
            raise ValueError()
        # Keep original provider embeddings for later formal-index promotion.
        return [[float(value) for value in row] for row in values]
    except (ValueError, TypeError, OverflowError):
        raise StagingError('invalid_vectors') from None


def _bounds(chunk, pages):
    metadata = chunk.get('metadata', {})
    start = metadata.get('original_page_char_start', metadata.get('page_char_start'))
    end = metadata.get('original_page_char_end', metadata.get('page_char_end'))
    page = chunk.get('page')
    if (type(page) is not int or not 1 <= page <= len(pages)
            or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(pages[page - 1])
            or pages[page - 1][start:end] != chunk.get('text')):
        raise StagingError('invalid_source_coordinates')
    return start, end


def _result(state, *, cache_hit=False):
    return {key: state[key] for key in ('source_sha256', 'chunking_configuration', 'embedding_signature',
                                       'chunks', 'vectors', 'passages')} | {
        'audit': dict(state['audit'], cache_hit=cache_hit,
                      new_embedding_calls=0 if cache_hit else state['audit']['embedding_calls'])}


def _with_neighbors(chunks, seeds, gap_ids):
    """Preserve all seeds, then fairly add next/previous leaves in their section.

    Coordinates and section metadata are source-derived. A missing section,
    another heading, or another page forbids expansion; no text is synthesized.
    """
    def span(row):
        meta = row.get('metadata', {})
        return (meta.get('original_page_char_start', meta.get('page_char_start')),
                meta.get('original_page_char_end', meta.get('page_char_end')))

    def section(row):
        meta = row.get('metadata', {})
        start, end = span(row)
        left, right, headings = meta.get('section_start'), meta.get('section_end'), meta.get('heading_path')
        if (any(type(value) is not int for value in (start, end, left, right))
                or not 0 <= left <= start < end <= right
                or not isinstance(headings, list) or any(not isinstance(heading, str) for heading in headings)):
            return None
        return (row['page'], left, right, tuple(headings))

    ordered = sorted(chunks, key=lambda row: (row['page'], *span(row)))
    positions = {row['id']: index for index, row in enumerate(ordered)}
    selected = OrderedDict((row['id'], row) for row in seeds)
    inherited = {identity: list(values) for identity, values in gap_ids.items()}
    neighbors = []
    # One forward neighbor for each seed has priority over every backward
    # neighbor. Early seeds cannot consume two spare slots before later seeds.
    for direction in (1, -1):
        for seed in seeds:
            signature = section(seed)
            index = positions.get(seed['id'])
            if signature is None or index is None or not 0 <= index + direction < len(ordered):
                continue
            neighbor = ordered[index + direction]
            if section(neighbor) != signature:
                continue
            identity = neighbor['id']
            if identity not in selected and len(selected) >= MAX_SELECTED_CHUNKS:
                continue
            if identity not in selected:
                selected[identity] = neighbor
                neighbors.append(identity)
            inherited[identity] = list(dict.fromkeys(inherited.get(identity, []) + gap_ids.get(seed['id'], [])))
    return list(selected.values()), inherited, {'neighbor_policy': NEIGHBOR_POLICY,
        'seed_chunk_ids': list(dict.fromkeys(row['id'] for row in seeds)), 'seed_chunk_count': len(seeds),
        'neighbor_chunk_ids': neighbors, 'neighbor_chunk_count': len(neighbors)}


async def stage_and_retrieve(pipeline, request, job_id, parsed, fetched, gaps, operation):
    _scope(pipeline, request, job_id)
    raw = fetched.get('raw') if isinstance(fetched, dict) else None
    if not isinstance(raw, bytes) or not raw or fetched.get('storage_policy') != 'open_license':
        raise StagingError('invalid_source')
    queries = _queries(gaps, pipeline)
    source_sha = hashlib.sha256(raw).hexdigest()
    chunking = pipeline.chunking_configuration()
    signature = pipeline.embedding_signature()
    encoding = encoding_configuration(pipeline.settings)
    report = parsed.report()
    pages = [page['text'] for page in report['pages']]
    maximum = min(MAX_SOURCE_CHUNKS, pipeline.settings.max_chunks,
                  getattr(pipeline.settings, 'exploration_max_source_chunks', MAX_SOURCE_CHUNKS))
    try:
        chunks = parsed_chunks(report, chunking, source_sha, maximum)
    except ValueError:
        raise StagingError('source_too_broad') from None
    if not chunks: raise StagingError('invalid_source')
    for chunk in chunks: _bounds(chunk, pages)
    snapshot = {'policy': configuration(), 'course_id': request.course_id, 'source_sha256': source_sha,
        'chunking_configuration': chunking, 'embedding_signature': signature,
        'document_encoding': encoding['document_encoding'], 'query_encoding': encoding['query_encoding'],
        'chunks_sha256': hashlib.sha256(dumps(chunks).encode()).hexdigest(), 'max_source_chunks': maximum}
    fingerprint = hashlib.sha256(dumps(snapshot).encode()).hexdigest()
    store = pipeline.store

    def save(state):
        state['updated_at'] = now()
        store.execute('UPDATE exploration_candidate_indexes SET state=? WHERE job_id=? AND candidate_sha256=?',
                      (dumps(state), job_id, source_sha))

    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT state FROM exploration_candidate_indexes WHERE job_id=? AND candidate_sha256=?',
                              (job_id, source_sha)).fetchone()
        if previous:
            try:
                state = json.loads(previous['state'])
            except (ValueError, TypeError):
                raise StagingError('invalid_stage') from None
            if not isinstance(state, dict): raise StagingError('invalid_stage')
            if state.get('status') != 'complete': raise StagingError('stage_interrupted')
            if state.get('fingerprint') != fingerprint or state.get('configuration') != snapshot:
                raise StagingError('stage_configuration_changed')
            if (state.get('chunks') != chunks or not isinstance(state.get('audit'), dict)
                    or state.get('source_sha256') != source_sha or state.get('chunking_configuration') != chunking
                    or state.get('embedding_signature') != signature):
                raise StagingError('invalid_stage')
            vectors = _vectors(state.get('vectors'), len(chunks))
            if state.get('queries') == queries:
                if not isinstance(state.get('passages'), list): raise StagingError('invalid_stage')
                for passage in state['passages']:
                    if (not isinstance(passage, dict) or passage.get('chunk_id') not in {row['id'] for row in chunks}
                            or not isinstance(passage.get('text'), str)):
                        raise StagingError('invalid_stage')
                    _bounds({'page': passage.get('page'), 'text': passage['text'], 'metadata': {
                        'page_char_start': passage.get('start_char'), 'page_char_end': passage.get('end_char')}}, pages)
                return _result(state, cache_hit=True)
            # Only a completed unchanged document index may support another
            # requirement query; incomplete attempts never replay paid calls.
            state.update(status='querying', queries=queries, passages=[])
            state['audit']['query_embedding_calls'] = 0
            state['audit']['document_embedding_calls'] = 0
            state['audit']['embedding_calls'] = 0
            state['audit']['document_index_reused'] = True
            db.execute('UPDATE exploration_candidate_indexes SET state=? WHERE job_id=? AND candidate_sha256=?',
                       (dumps(state), job_id, source_sha))
        else:
            vectors = []
            state = {'status': 'indexing', 'fingerprint': fingerprint, 'configuration': snapshot,
                'source_sha256': source_sha, 'chunking_configuration': chunking, 'embedding_signature': signature,
                'chunks': chunks, 'vectors': [], 'report': report, 'queries': queries, 'passages': [],
                'audit': {'version': VERSION, 'chunk_count': len(chunks), 'document_embedding_calls': 0,
                          'query_embedding_calls': 0, 'embedding_calls': 0, 'cumulative_embedding_calls': 0,
                          'document_index_reused': False}, 'created_at': now(), 'updated_at': now()}
            db.execute('INSERT INTO exploration_candidate_indexes VALUES(?,?,?)', (job_id, source_sha, dumps(state)))

    def unchanged():
        _scope(pipeline, request, job_id)
        if pipeline.chunking_configuration() != chunking or pipeline.embedding_signature() != signature:
            raise StagingError('stage_configuration_changed')

    async def embed(texts, kind):
        unchanged()
        state['status'] = 'indexing' if kind == 'document_embedding_calls' else 'querying'
        state['audit'][kind] += 1; state['audit']['embedding_calls'] += 1
        state['audit']['cumulative_embedding_calls'] += 1
        save(state)
        answer = await operation('indexing_candidate' if kind == 'document_embedding_calls' else 'retrieving_candidate',
                                 lambda: pipeline.providers.embed(texts, job_id))
        unchanged()
        return answer

    try:
        if not vectors:
            for start in range(0, len(chunks), EMBED_BATCH_SIZE):
                batch = chunks[start:start + EMBED_BATCH_SIZE]
                inputs = [document_input(row, parsed.name, encoding['document_encoding']) for row in batch]
                answer = await embed(inputs, 'document_embedding_calls')
                vectors.extend(_vectors(answer, len(batch), len(vectors[0]) if vectors else None))
                state['vectors'] = vectors; save(state)
        query_vectors = _vectors(await embed([query_input(row['query'], encoding['query_encoding'] == 'cs_instruction_v1')
                                              for row in queries], 'query_embedding_calls'), len(queries), len(vectors[0]))
        ranked_chunks = [row | {'vector': vector} for row, vector in zip(chunks, vectors)]
        rankings = [rank_chunks(ranked_chunks, query['query'], vector, strategy='hybrid_rrf_v1',
                               top_k=GAP_TOP_K, candidate_k=CANDIDATE_K)
                    for query, vector in zip(queries, query_vectors)]
        chosen, gap_ids = OrderedDict(), {}
        for query, ranking in zip(queries, rankings):
            for row in ranking: gap_ids.setdefault(row['id'], []).append(query['gap_id'])
        for position in range(GAP_TOP_K):
            for ranking in rankings:
                if position < len(ranking): chosen.setdefault(ranking[position]['id'], ranking[position])
        seeds = list(chosen.values())[:MAX_SELECTED_CHUNKS]
        selected, gap_ids, neighbor_audit = _with_neighbors(chunks, seeds, gap_ids)
        queues = []
        for row in selected:
            start, end = _bounds(row, pages)
            queues.append([{'text': pages[row['page'] - 1][left:min(left + MAX_PASSAGE_CHARS, end)],
                'page': row['page'], 'start_char': left, 'end_char': min(left + MAX_PASSAGE_CHARS, end),
                'chunk_id': row['id'], 'gap_ids': gap_ids[row['id']], 'metadata': row['metadata']}
                for left in range(start, end, MAX_PASSAGE_CHARS)])
        passages, remaining = [], MAX_TOTAL_PASSAGE_CHARS
        while any(queues) and len(passages) < MAX_PASSAGES and remaining >= 12:
            for queue in queues:
                if not queue or len(passages) >= MAX_PASSAGES or remaining < 12: continue
                passage = queue.pop(0)
                if len(passage['text']) > remaining:
                    passage['text'] = passage['text'][:remaining]
                    passage['end_char'] = passage['start_char'] + len(passage['text'])
                if len(passage['text']) < 12: continue
                passage['id'] = 'p' + str(len(passages) + 1)
                passages.append(passage); remaining -= len(passage['text'])
        unchanged()
        state.update(status='complete', vectors=vectors, passages=passages)
        state['audit'].update(**neighbor_audit, selected_chunk_count=len(selected), passage_count=len(passages),
            passage_characters=MAX_TOTAL_PASSAGE_CHARS - remaining,
            per_gap=[{'gap_id': query['gap_id'], 'query': query['query'],
                      'retrieved_chunk_ids': [row['id'] for row in ranking]}
                     for query, ranking in zip(queries, rankings)],
            query_vectors_sha256=hashlib.sha256(dumps(query_vectors).encode()).hexdigest(),
            limitation='Retrieved passages require evidence selection; ranking is not semantic support verification.')
        save(state)
        return _result(state)
    except BaseException:
        state['status'] = 'interrupted'
        save(state)
        raise


def retire_stages(store, job_id):
    """Remove temporary document/vector payloads while retaining bounded audits."""
    for row in store.all('SELECT candidate_sha256,state FROM exploration_candidate_indexes WHERE job_id=?', (job_id,)):
        try:
            old = json.loads(row['state'])
            if not isinstance(old, dict): old = {}
        except (ValueError, TypeError):
            old = {}
        if old.get('status') == 'retired':
            continue
        retired = {key: old[key] for key in ('configuration', 'fingerprint', 'audit', 'created_at') if key in old}
        retired.update(status='retired', previous_status=old.get('status'), source_sha256=row['candidate_sha256'], updated_at=now())
        store.execute('UPDATE exploration_candidate_indexes SET state=? WHERE job_id=? AND candidate_sha256=?',
                      (dumps(retired), job_id, row['candidate_sha256']))
