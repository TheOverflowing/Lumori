"""Job-private candidate-index contracts with local deterministic embeddings."""
import asyncio
import hashlib
import json
import math

import pytest

from app import exploration_staging as staging
from app.config import Endpoint, Settings
from app.document_parsing import ParsedDocument, ParsedPage
from app.models import GenerateRequest
from app.pipeline import Pipeline
from app.store import Store, dumps, now


class Embeddings:
    def __init__(self, store, settings):
        self.store = store; self.settings = settings; self.calls = []; self.reply = None

    async def embed(self, texts, job_id):
        state = json.loads(self.store.one('SELECT state FROM exploration_candidate_indexes WHERE job_id=?', (job_id,))['state'])
        assert state['status'] in ('indexing', 'querying')
        assert not state['passages']
        assert self.store.one('SELECT COUNT(*) AS n FROM documents')['n'] == 0
        self.calls.append({'texts': texts, 'status': state['status'], 'job_id': job_id})
        identity = self.store.reserve_call(job_id, 'embedding', 'fixture-only', self.settings.max_daily_calls)
        self.store.execute("UPDATE calls SET status='succeeded' WHERE id=?", (identity,))
        if isinstance(self.reply, BaseException): raise self.reply
        if callable(self.reply): return self.reply(texts)
        return [[1. if f'topic{i}' in text else .001 for i in range(1, 7)] for text in texts]


@pytest.fixture
def rig(tmp_path):
    settings = Settings(data_dir=tmp_path, chunk_strategy='recursive_v1', chunk_max_chars=180,
                        chunk_overlap_chars=0, max_daily_calls=100)
    settings.embedding = Endpoint('https://fixture.invalid', 'fixture-key', 'fixture-model', '/embeddings')
    store = Store(tmp_path)
    for owner, course in [('user-a', 'course-a'), ('user-b', 'course-b')]:
        store.execute('INSERT INTO users VALUES(?,?,?,?,?)', (owner, owner + '@example.test', 'fixture-only', owner, now()))
        store.execute('INSERT INTO courses VALUES(?,?,?)', (course, course, now()))
        store.execute('INSERT INTO course_owners VALUES(?,?)', (course, owner))
    providers = Embeddings(store, settings)
    pipeline = Pipeline(settings, store, providers, enforce_account_ownership=True)
    request = GenerateRequest(course_id='course-a', topic='A broad educational introduction', request_key='stage-fixture-1', auto_explore=True)
    job, _ = store.job(request.request_key, 'generate', request.model_dump(), owner_id='user-a')
    operations = []

    async def operation(kind, call):
        operations.append(kind)
        return await call()

    return pipeline, request, job['id'], operation, operations


def document(text):
    return ParsedDocument('public-course.md', 'test_parser', [ParsedPage(1, text, method='native_text', status='good')])


def gaps(count=2):
    return [{'id': 'g' + str(i), 'need': 'Explain topic' + str(i),
             'queries': [{'query': 'topic' + str(i) + ' teaching examples', 'language': 'en'},
                         {'query': 'topic' + str(i) + ' 本科知识', 'language': 'zh'}]} for i in range(1, count + 1)]


def run(rig, parsed=None, requirements=None, **overrides):
    pipeline, request, job_id, operation, _ = rig
    parsed = parsed or document('topic1 categories describe supervised learning and unsupervised learning.\n\n'
                                'topic2 workflow includes training data evaluation and model deployment.')
    fetched = {'raw': '\n'.join(parsed.text_pages).encode(), 'storage_policy': 'open_license'}
    return asyncio.run(staging.stage_and_retrieve(pipeline, overrides.get('request', request),
        overrides.get('job_id', job_id), parsed, overrides.get('fetched', fetched), requirements or gaps(), operation))


def test_full_candidate_is_indexed_before_passages_are_selected_without_course_documents(rig):
    pipeline, _, job_id, _, operations = rig
    result = run(rig)
    assert operations == ['indexing_candidate', 'retrieving_candidate']
    assert [call['status'] for call in pipeline.providers.calls] == ['indexing', 'querying']
    assert len(pipeline.providers.calls[-1]['texts']) == 2
    assert result['passages'] and result['audit']['per_gap'][0]['gap_id'] == 'g1'
    assert len(result['chunks']) == len(result['vectors'])
    assert result['chunking_configuration'] == pipeline.chunking_configuration()
    assert result['embedding_signature'] == pipeline.embedding_signature()
    assert all(pipeline.store.one('SELECT COUNT(*) AS n FROM ' + table)['n'] == 0
               for table in ('documents', 'chunks', 'document_parsing', 'external_document_sources'))
    state = json.loads(pipeline.store.one('SELECT state FROM exploration_candidate_indexes WHERE job_id=?', (job_id,))['state'])
    assert state['status'] == 'complete' and state['report']['pages'][0]['text']
    assert result['audit']['embedding_calls'] == pipeline.store.one('SELECT COUNT(*) AS n FROM calls')['n']


def test_document_batches_are_bounded_and_all_gap_queries_share_one_call(rig):
    pipeline = rig[0]
    parsed = document('\n\n'.join(f'topic1 paragraph {i} contains specific course facts and learning examples. ' * 2 for i in range(35)))
    result = run(rig, parsed)
    docs = pipeline.providers.calls[:-1]
    assert len(docs) == math.ceil(len(result['chunks']) / 16) >= 3
    assert all(1 <= len(call['texts']) <= 16 for call in docs)
    assert pipeline.providers.calls[-1]['status'] == 'querying' and len(pipeline.providers.calls[-1]['texts']) == 2


def test_complete_same_job_cache_does_not_repeat_embedding_calls(rig):
    first = run(rig)
    count = len(rig[0].providers.calls)
    second = run(rig)
    assert len(rig[0].providers.calls) == count
    assert second['passages'] == first['passages'] and second['vectors'] == first['vectors']
    assert second['audit']['cache_hit'] and second['audit']['new_embedding_calls'] == 0


def test_new_requirement_queries_reuse_only_a_completed_document_index(rig):
    run(rig)
    count = len(rig[0].providers.calls)
    changed = [{'id': 'g1', 'need': 'Explain topic1 more deeply', 'queries': []}]
    result = run(rig, requirements=changed)
    assert len(rig[0].providers.calls) == count + 1
    assert result['audit']['document_index_reused'] and result['audit']['document_embedding_calls'] == 0
    assert result['audit']['cumulative_embedding_calls'] == count + 1


def test_staging_cache_is_never_shared_with_another_job_or_course(rig):
    pipeline = rig[0]
    first = run(rig)
    count = len(pipeline.providers.calls)
    request = rig[1].model_copy(update={'course_id': 'course-b', 'request_key': 'another-job'})
    job, _ = pipeline.store.job(request.request_key, 'generate', request.model_dump(), owner_id='user-b')
    second = run(rig, request=request, job_id=job['id'])
    assert second['source_sha256'] == first['source_sha256']
    assert len(pipeline.providers.calls) == 2 * count and not second['audit']['cache_hit']
    assert pipeline.store.one('SELECT COUNT(*) AS n FROM exploration_candidate_indexes')['n'] == 2


def test_wrong_course_request_cannot_use_a_job_or_create_staging_records(rig):
    request = rig[1].model_copy(update={'course_id': 'course-b'})
    with pytest.raises(staging.StagingError): run(rig, request=request)
    assert not rig[0].providers.calls
    assert rig[0].store.one('SELECT COUNT(*) AS n FROM exploration_candidate_indexes')['n'] == 0


def test_account_ownership_is_checked_before_embedding(rig):
    rig[0].store.execute("UPDATE course_owners SET user_id='user-b' WHERE course_id='course-a'")
    with pytest.raises(ValueError): run(rig)
    assert not rig[0].providers.calls


def test_changed_index_configuration_invalidates_cache_without_replaying_paid_calls(rig):
    run(rig)
    count = len(rig[0].providers.calls)
    rig[0].settings.chunk_max_chars = 200
    with pytest.raises(staging.StagingError) as exc: run(rig)
    assert exc.value.code == 'stage_configuration_changed' and len(rig[0].providers.calls) == count


def test_interrupted_index_is_not_silently_repeated(rig):
    rig[0].providers.reply = RuntimeError('fixture provider outage')
    with pytest.raises(RuntimeError): run(rig)
    count = len(rig[0].providers.calls)
    rig[0].providers.reply = None
    with pytest.raises(staging.StagingError) as exc: run(rig)
    assert exc.value.code == 'stage_interrupted' and len(rig[0].providers.calls) == count


@pytest.mark.parametrize('reply', [lambda texts: [], lambda texts: [[0., 0.] for _ in texts],
    lambda texts: [[float('nan'), 1.] for _ in texts], lambda texts: [[True, 1.] for _ in texts]])
def test_invalid_embedding_vectors_never_complete_a_stage(rig, reply):
    rig[0].providers.reply = reply
    with pytest.raises(staging.StagingError) as exc: run(rig)
    assert exc.value.code == 'invalid_vectors'
    state = json.loads(rig[0].store.one('SELECT state FROM exploration_candidate_indexes')['state'])
    assert state['status'] == 'interrupted' and not state['passages']


def test_query_and_document_vector_dimensions_must_match(rig):
    calls = []
    def reply(texts):
        calls.append(texts)
        return [[1., 0.] if len(calls) == 1 else [1., 0., 0.] for _ in texts]
    rig[0].providers.reply = reply
    with pytest.raises(staging.StagingError) as exc: run(rig)
    assert exc.value.code == 'invalid_vectors'


def test_passages_have_exact_coordinates_and_fair_bounded_gap_coverage(rig):
    pipeline = rig[0]; pipeline.settings.chunk_max_chars = 3000
    text = '\n\n'.join(f'topic{i} paragraph {j} ' + 'explanatory educational material ' * 80
                       for i in range(1, 7) for j in range(4))
    result = run(rig, document(text), gaps(6))
    passages = result['passages']
    assert len(result['chunks']) == 24 and result['audit']['selected_chunk_count'] == 24
    assert len(passages) <= 48 and sum(len(row['text']) for row in passages) <= 24000
    assert all(len(row['text']) <= 900 and text[row['start_char']:row['end_char']] == row['text'] for row in passages)
    assert {gap for row in passages[:6] for gap in row['gap_ids']} == {'g' + str(i) for i in range(1, 7)}
    assert [row['id'] for row in passages] == ['p' + str(i) for i in range(1, len(passages) + 1)]


def test_overbroad_candidate_is_rejected_before_any_remote_call(rig):
    rig[0].settings.exploration_max_source_chunks = 1
    with pytest.raises(staging.StagingError) as exc:
        run(rig, document('topic1 independent paragraph with academic details.\n\n' * 20))
    assert exc.value.code == 'source_too_broad' and not rig[0].providers.calls


def test_retirement_removes_temporary_vectors_and_text_but_keeps_audit(rig):
    run(rig)
    pipeline, _, job_id, _, _ = rig
    staging.retire_stages(pipeline.store, job_id)
    state = json.loads(pipeline.store.one('SELECT state FROM exploration_candidate_indexes')['state'])
    assert state['status'] == 'retired' and state['previous_status'] == 'complete'
    assert not {'chunks', 'vectors', 'report', 'passages', 'queries'}.intersection(state)
    assert state['audit']['chunk_count'] and state['audit']['per_gap']
    staging.retire_stages(pipeline.store, job_id)
    assert json.loads(pipeline.store.one('SELECT state FROM exploration_candidate_indexes')['state']) == state


def test_unlicensed_body_is_not_persisted_or_embedded(rig):
    with pytest.raises(staging.StagingError) as exc:
        run(rig, fetched={'raw': b'Publicly readable does not establish persistence permission.', 'storage_policy': 'unknown'})
    assert exc.value.code == 'invalid_source' and not rig[0].providers.calls


def neighbor_chunks():
    texts = ['Previous background sentence about learning tasks.',
             '## Unsupervised learning\nFirst recall how supervised learning uses labels.',
             'Unsupervised learning discovers patterns in data without target labels.',
             '## Reinforcement learning\nAgents learn from rewards through interaction.']
    page = '\n\n'.join(texts)
    chunks, position = [], 0
    boundary = page.index('## Reinforcement')
    for index, text in enumerate(texts):
        left, right = (0, boundary) if index < 3 else (boundary, len(page))
        chunks.append({'id': 'leaf-' + str(index), 'page': 1, 'text': text,
            'metadata': {'page_char_start': position, 'page_char_end': position + len(text),
                         'section_start': left, 'section_end': right,
                         'heading_path': ['Unsupervised'] if index < 3 else ['Reinforcement']}})
        position += len(text) + 2
    return page, chunks


def test_next_leaf_reveals_definition_after_comparison_without_crossing_section():
    page, chunks = neighbor_chunks()
    selected, inherited, audit = staging._with_neighbors(chunks, [chunks[1]], {'leaf-1': ['g2']})
    assert [row['id'] for row in selected] == ['leaf-1', 'leaf-2', 'leaf-0']
    assert 'without target labels' in selected[1]['text']
    assert inherited['leaf-2'] == ['g2'] and audit['seed_chunk_ids'] == ['leaf-1']
    assert audit['neighbor_chunk_ids'] == ['leaf-2', 'leaf-0']
    assert all(page[row['metadata']['page_char_start']:row['metadata']['page_char_end']] == row['text'] for row in selected)
    selected, _, _ = staging._with_neighbors(chunks, [chunks[2]], {'leaf-2': ['g2']})
    assert 'leaf-3' not in {row['id'] for row in selected}


@pytest.mark.parametrize('change', ['missing_section', 'different_page', 'different_heading', 'invalid_section'])
def test_neighbor_expansion_requires_valid_same_page_section_metadata(change):
    _, chunks = neighbor_chunks()
    if change == 'missing_section': chunks[2]['metadata'].pop('section_start')
    if change == 'different_page': chunks[2]['page'] = 2
    if change == 'different_heading': chunks[2]['metadata']['heading_path'] = ['Another section']
    if change == 'invalid_section': chunks[2]['metadata']['section_end'] = 1
    selected, _, _ = staging._with_neighbors(chunks, [chunks[1]], {'leaf-1': ['g2']})
    assert 'leaf-2' not in {row['id'] for row in selected}


def test_neighbor_budget_preserves_all_seeds_and_prioritizes_next_fairly(monkeypatch):
    _, chunks = neighbor_chunks()
    other = [{**row, 'id': 'other-' + row['id'], 'page': 2} for row in chunks]
    seeds = [chunks[1], other[1]]
    monkeypatch.setattr(staging, 'MAX_SELECTED_CHUNKS', 4)
    selected, _, audit = staging._with_neighbors(chunks + other, seeds,
        {chunks[1]['id']: ['g1'], other[1]['id']: ['g2']})
    assert [row['id'] for row in selected] == ['leaf-1', 'other-leaf-1', 'leaf-2', 'other-leaf-2']
    assert audit['seed_chunk_count'] == 2 and audit['neighbor_chunk_count'] == 2


def test_stage_passages_include_same_section_following_definition(rig, monkeypatch):
    page, chunks = neighbor_chunks()
    def parsed_with_sections(report, configuration, digest, maximum):
        # Controlled index fixture with original coordinates and structural
        # metadata. The live replay uses the real document chunker separately.
        return [row | {'metadata': row['metadata'] | {'source_document_sha256': digest}} for row in chunks]
    monkeypatch.setattr(staging, 'parsed_chunks', parsed_with_sections)
    monkeypatch.setattr(staging, 'rank_chunks', lambda rows, *args, **kwargs: [rows[1]])
    result = run(rig, document(page), gaps(1))
    assert any('without target labels' in passage['text'] for passage in result['passages'])
    assert result['audit']['seed_chunk_ids'] == ['leaf-1']
    assert result['audit']['neighbor_chunk_ids'] == ['leaf-2', 'leaf-0']
    assert result['audit']['version'] == 'candidate_index_gap_retrieval_v2'
    assert len(result['chunks']) == len(result['vectors']) == 4
    assert len(rig[0].providers.calls) == 2
