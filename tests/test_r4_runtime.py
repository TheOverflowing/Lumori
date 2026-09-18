import hashlib
import json
import pytest
from test_workflow import rig, done, seed, request, generated


def configure(app, tmp_path):
    from tokenizers import Tokenizer, models, pre_tokenizers
    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'search': 1, 'evidence': 2}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path / 'tokenizer.json'
    tokenizer.save(str(path))
    s = app.state.settings
    s.chunk_strategy = 'recursive_token_v1'
    s.chunk_max_tokens = 8
    s.chunk_overlap_chars = 0
    s.rag_tokenizer_path = path
    s.retrieval_document_encoding = 'title_path_v1'
    s.retrieval_query_encoding = 'cs_instruction_v1'
    s.retrieval_context_tokens = 24
    s.retrieval_strategy = 'hybrid_dense_lexical_v1'
    return path


def test_r4_generation_uses_versioned_inputs_and_exact_original_sources(rig, tmp_path, monkeypatch):
    client, app, wire = rig
    configure(app, tmp_path)
    captured = []
    provider = app.state.pipeline.providers
    original = provider.embed
    async def spy(texts, job_id=None):
        captured.extend(texts)
        return await original(texts, job_id)
    monkeypatch.setattr(provider, 'embed', spy)
    course = client.post('/api/courses', json={'name': 'CS'}).json()['id']
    raw = '# Retrieval\n\n' + 'search for evidence in original documents. ' * 12
    did = client.post('/api/documents', data={'course_id': course}, files={'file': ('lecture.md', raw.encode())}).json()['id']
    assert done(client, client.post('/api/documents/' + did + '/index'))['status'] == 'succeeded'
    assert all(text.startswith('lecture > Retrieval\n') for text in captured)
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'succeeded', job
    assert captured[-1].startswith('Instruct: ') and captured[-1].endswith('Query: 检索的作用')
    content = client.get('/api/contents/' + job['result']['content_id']).json()
    for source in content['sources']:
        meta = source['metadata']
        assert raw[meta['page_char_start']:meta['page_char_end']] == source['text']
        assert source['id'].startswith('context_') and source['document_id'] == did
    evidence = client.get('/api/jobs/' + job['id'] + '/evidence').json()['evidence']
    assert evidence['retrieval']['context_tokens'] <= 24
    assert evidence['retrieval']['query_encoding'] == 'cs_instruction_v1'
    assert content['config']['embedding_signature'] != app.state.settings.embedding.signature


def test_encoding_change_requires_reindex_and_preserves_old_content(rig, tmp_path):
    client, app, wire = rig
    course, did = seed(client)
    cid = generated(client, course)
    snapshot = client.get('/api/contents/' + cid).json()['sources']
    configure(app, tmp_path)
    before = wire.calls
    job = done(client, client.post('/api/generations', json=request(course, 'stale-r4-index')))
    assert job['status'] == 'failed' and wire.calls == before
    assert not client.get('/api/documents', params={'course_id': course}).json()[0]['index_current']
    assert done(client, client.post('/api/documents/' + did + '/index'))['status'] == 'succeeded'
    assert client.get('/api/documents', params={'course_id': course}).json()[0]['index_current']
    assert client.get('/api/contents/' + cid).json()['sources'] == snapshot


def test_query_instruction_change_does_not_invalidate_document_vectors(rig):
    client, app, wire = rig
    course, did = seed(client)
    signature = app.state.pipeline.embedding_signature()
    app.state.settings.retrieval_query_encoding = 'cs_instruction_v1'
    assert app.state.pipeline.embedding_signature() == signature
    before = wire.calls
    result = done(client, client.post('/api/documents/' + did + '/index'))
    assert result['result']['reused'] and wire.calls == before


def test_failed_token_reindex_keeps_old_index(rig, tmp_path):
    client, app, wire = rig
    _, did = seed(client)
    old = app.state.store.document_chunks(did)
    configure(app, tmp_path)
    wire.mode = 'http_error'
    job = done(client, client.post('/api/documents/' + did + '/index'))
    assert job['status'] == 'failed'
    assert app.state.store.document_chunks(did) == old


@pytest.mark.parametrize('field,value', [('retrieval_document_encoding', 'unknown'),
                                       ('retrieval_query_encoding', 'unknown'),
                                       ('retrieval_context_tokens', -1), ('retrieval_expansion', 'unknown')])
def test_invalid_encoding_stops_before_api(rig, field, value):
    client, app, wire = rig
    course, _ = seed(client)
    setattr(app.state.settings, field, value)
    before = wire.calls
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'failed' and wire.calls == before


def test_tokenizer_change_rejects_old_cut_config(rig, tmp_path):
    client, app, wire = rig
    path = configure(app, tmp_path)
    _, did = seed(client)
    before = app.state.store.document_chunks(did)
    path.write_text(path.read_text() + ' ')
    assert app.state.store.chunking_configuration(did) != app.state.pipeline.chunking_configuration()
    assert app.state.store.document_chunks(did) == before


def test_remote_rerank_contract_reorders_before_context_and_is_metered(rig, tmp_path, monkeypatch):
    import httpx
    from app.config import Endpoint
    client, app, wire = rig
    configure(app, tmp_path)
    app.state.settings.rerank = Endpoint('https://test.invalid/v1','test-key-not-real','fixture-reranker','/rerank')
    original = wire.__call__
    captured = []
    def transport(req):
        if req.url.path.endswith('/rerank'):
            wire.calls += 1
            body = json.loads(req.content)
            captured.append(body)
            assert body['top_n'] == len(body['documents'])
            return httpx.Response(200,json={'model':'fixture-reranker','results':[
                {'index':i,'relevance_score':float(i)} for i in reversed(range(len(body['documents'])))],
                'usage':{'total_tokens':123,'search_units':1,'cost':.001}})
        return original(req)
    app.state.pipeline.providers.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    course, did = seed(client)
    job = done(client,client.post('/api/generations',json=request(course)))
    assert job['status']=='succeeded',job
    assert captured[0]['query']=='检索的作用'
    evidence=client.get('/api/jobs/'+job['id']+'/evidence').json()['evidence']
    assert evidence['retrieval']['configuration']['reranker_model']=='fixture-reranker'
    calls=app.state.store.calls_for_job(job['id'])
    rerank=[c for c in calls if c['capability']=='rerank']
    assert len(rerank)==1 and rerank[0]['usage']['search_units']==1


@pytest.mark.parametrize('results', [[], [{'index':0,'relevance_score':1.0},{'index':0,'relevance_score':.5}],
    [{'index':0,'relevance_score':float('nan')}], [{'index':True,'relevance_score':.5}]])
def test_invalid_rerank_indices_and_scores_rejected(results):
    from app.providers import ApiProviders,ProviderError
    with pytest.raises(ProviderError):ApiProviders._rerank_scores({'results':results},2)


def test_embedding_key_reuse_rejects_another_origin(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv('EMBEDDING_BASE_URL','https://safe.invalid/v1')
    monkeypatch.setenv('EMBEDDING_API_KEY','fixture-secret')
    monkeypatch.setenv('RERANK_BASE_URL','https://other.invalid/v1')
    monkeypatch.setenv('RERANK_USE_EMBEDDING_KEY','true')
    monkeypatch.delenv('RERANK_API_KEY',raising=False)
    with pytest.raises(ValueError,match='相同网关'):Settings.from_env()
