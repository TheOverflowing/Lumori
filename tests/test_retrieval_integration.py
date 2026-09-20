"""Retrieval strategies share the same course boundaries and auditable pipeline."""
import json

import pytest

from app.config import Settings
from app.retrieval import STRATEGIES
from test_workflow import done, generated, request, rig, seed


@pytest.mark.parametrize('strategy', STRATEGIES)
def test_each_strategy_preserves_sources_and_records_retrieval_trace(rig, strategy):
    client, app, wire = rig
    course, document = seed(client)
    other, other_document = seed(client)
    app.state.settings.retrieval_strategy = strategy
    before = wire.calls
    response = client.post('/api/generations', json=request(course, 'retrieval-' + strategy))
    job = done(client, response)
    assert job['status'] == 'succeeded'
    content = client.get('/api/contents/' + job['result']['content_id']).json()
    assert {item['document_id'] for item in content['sources']} == {document}
    assert other_document not in {item['document_id'] for item in content['sources']}
    assert wire.calls - before == 3  # Query embedding + author + difficulty review.
    evidence = client.get('/api/jobs/' + job['id'] + '/evidence').json()['evidence']
    trace = evidence['retrieval']
    assert trace['configuration']['strategy'] == strategy
    assert trace['configuration'] == content['config']['retrieval']
    assert trace['query'] == request(course)['topic']
    assert trace['query_encoding'] == 'raw_topic_v1'
    assert trace['candidate_count'] == trace['selected_count'] == 1
    assert len(trace['query_vector_sha256']) == len(trace['corpus_sha256']) == 64
    assert all(value >= 0 for value in trace['timing_ms'].values())
    assert 'vector' not in content['sources'][0]
    assert -1 <= content['sources'][0]['score'] <= 1
    assert client.get('/api/status').json()['retrieval']['strategy'] == strategy
    reviewer_sources = evidence['attempts'][0]['assessment']['contract']['reference_chunks']
    assert all('retrieval' not in source for source in reviewer_sources)


@pytest.mark.parametrize('strategy', STRATEGIES)
def test_document_filter_and_stale_index_guard_apply_to_all_strategies(rig, strategy):
    client, app, wire = rig
    course, allowed = seed(client)
    second = client.post('/api/documents', data={'course_id': course},
                         files={'file': ('extra.md', '检索为生成提供额外依据。'.encode())}).json()['id']
    done(client, client.post('/api/documents/' + second + '/index'))
    app.state.settings.retrieval_strategy = strategy
    response = client.post('/api/generations', json=request(course) | {'document_ids': [allowed]})
    job = done(client, response)
    content = client.get('/api/contents/' + job['result']['content_id']).json()
    assert {source['document_id'] for source in content['sources']} == {allowed}
    app.state.store.execute('UPDATE chunks SET embedding_signature=? WHERE document_id=?', ('old', allowed))
    before = wire.calls
    job = done(client, client.post('/api/generations', json=request(course, 'stale-index') | {'document_ids': [allowed]}))
    assert job['status'] == 'failed' and '重新' in job['error']
    assert wire.calls == before


def test_bm25_empty_result_returns_insufficient_without_text_call(rig):
    client, app, wire = rig
    course, _ = seed(client)
    app.state.settings.retrieval_strategy = 'bm25_v1'
    before = wire.calls
    response = client.post('/api/generations', json=request(course) | {'topic':'unrelated quasar spectroscopy'})
    job = done(client, response)
    assert job['status'] == 'insufficient_evidence'
    assert wire.calls == before + 1  # Only the diagnostic query embedding.
    evidence = client.get('/api/jobs/' + job['id'] + '/evidence').json()['evidence']
    assert evidence['retrieval']['selected_count'] == 0
    assert evidence['attempts'] == []
    assert client.get('/api/contents', params={'course_id':course}).json() == []


@pytest.mark.parametrize('field,value', [('retrieval_strategy', 'unknown'), ('top_k', 0),
                                        ('retrieval_candidate_k', 2), ('retrieval_mmr_lambda', float('nan'))])
def test_invalid_retrieval_configuration_fails_without_api_calls(rig, field, value):
    client, app, wire = rig
    course, _ = seed(client)
    setattr(app.state.settings, field, value)
    before = wire.calls
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'failed'
    assert wire.calls == before
    status = client.get('/api/status')
    assert status.status_code == 503 and '检索配置' in status.json()['detail']


def test_retrieval_env_settings_preserve_dense_default_and_allow_explicit_selection(monkeypatch, tmp_path):
    # A user's selected local profile is independent of the library default.
    monkeypatch.setattr('app.config.ROOT', tmp_path)
    for name in ('RETRIEVAL_STRATEGY', 'RETRIEVAL_TOP_K', 'RETRIEVAL_CANDIDATE_K', 'RETRIEVAL_RRF_K', 'RETRIEVAL_MMR_LAMBDA'):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env().retrieval_strategy == 'dense_v1'
    monkeypatch.setenv('RETRIEVAL_STRATEGY', 'hybrid_rrf_v1')
    monkeypatch.setenv('RETRIEVAL_TOP_K', '7')
    monkeypatch.setenv('RETRIEVAL_CANDIDATE_K', '30')
    settings = Settings.from_env()
    assert settings.retrieval_strategy == 'hybrid_rrf_v1'
    assert settings.top_k == 7 and settings.retrieval_candidate_k == 30
