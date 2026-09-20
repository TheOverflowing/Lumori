"""Source offsets, additive storage, and atomic live-index replacement."""
import hashlib
import json

import pytest

from app.config import Settings
from app.providers import ProviderError
from app.store import Store
from test_workflow import done, generated, rig, seed


def upload(client, course, text):
    response = client.post('/api/documents', data={'course_id': course},
                          files={'file': ('notes.md', text.encode('utf-8'))})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_upload_metadata_maps_to_original_and_ids_are_course_scoped(rig):
    client, app, _ = rig
    text = '\n# Binary search 二分查找\n\nSearch a **sorted array** with `binary_search`.\n'
    documents = []
    for name in ('Computer Science', '计算机'):
        course = client.post('/api/courses', json={'name': name}).json()['id']
        documents.append(upload(client, course, text))
    first, second = [client.get(f'/api/documents/{doc}/chunks').json() for doc in documents]
    assert not {row['id'] for row in first} & {row['id'] for row in second}
    for chunk in first:
        metadata = chunk['metadata']
        assert text[metadata['page_char_start']:metadata['page_char_end']] == chunk['text']
        assert metadata['source_page_sha256'] == hashlib.sha256(text.encode()).hexdigest()
        assert metadata['source_document_sha256'] == hashlib.sha256(text.encode()).hexdigest()
        assert metadata['heading_path'] and metadata['keywords']
        assert 'vector' not in chunk
    current = client.get('/api/status').json()['chunking']
    assert current == app.state.store.chunking_configuration(documents[0])


def test_additive_migration_preserves_legacy_chunk_rows_and_content_snapshots(rig):
    client, app, _ = rig
    course, doc = seed(client)
    content_id = generated(client, course)
    store = app.state.store
    snapshot = store.one('SELECT sources FROM contents WHERE id=?', (content_id,))['sources']
    old_ids = {r['id'] for r in store.document_chunks(doc)}
    # A pre-R3 database has neither side table. The six-column schema is retained.
    store.execute('DROP TABLE chunk_metadata')
    store.execute('DROP TABLE document_chunking')
    Store(app.state.settings.data_dir)
    assert store.document_chunks(doc)[0]['metadata'] == {}
    assert store.one('SELECT sources FROM contents WHERE id=?', (content_id,))['sources'] == snapshot
    result = done(client, client.post(f'/api/documents/{doc}/index'))
    assert result['status'] == 'succeeded' and result['result']['rechunked']
    assert store.document_chunks(doc)[0]['metadata']['chunking_version']
    assert store.one('SELECT sources FROM contents WHERE id=?', (content_id,))['sources'] == snapshot
    assert {s['id'] for s in json.loads(snapshot)} == old_ids


def test_failed_second_embedding_batch_keeps_entire_old_index_and_snapshot(rig, monkeypatch):
    client, app, _ = rig
    course = client.post('/api/courses', json={'name': 'Algorithms'}).json()['id']
    text = '\n\n'.join(f'## Topic {n}\nBinary search requires sorted input. Compare bounds and halve the interval.' for n in range(25))
    doc = upload(client, course, text)
    assert done(client, client.post(f'/api/documents/{doc}/index'))['status'] == 'succeeded'
    store = app.state.store
    before = store.document_chunks(doc)
    before_config = store.chunking_configuration(doc)
    app.state.settings.chunk_max_chars = 300
    app.state.settings.chunk_overlap_chars = 30
    provider = app.state.pipeline.providers
    original = provider.embed
    batches = []

    async def fail_second(texts, job_id=None):
        batches.append(texts)
        if len(batches) == 2:
            raise ProviderError('fixture second batch failure')
        return await original(texts, job_id)

    monkeypatch.setattr(provider, 'embed', fail_second)
    result = done(client, client.post(f'/api/documents/{doc}/index'))
    assert result['status'] == 'failed' and len(batches) == 2
    assert store.document_chunks(doc) == before
    assert store.chunking_configuration(doc) == before_config
    assert store.one('SELECT status FROM documents WHERE id=?', (doc,))['status'] == 'ready'
    monkeypatch.setattr(provider, 'embed', original)
    result = done(client, client.post(f'/api/documents/{doc}/index'))
    assert result['status'] == 'succeeded' and result['result']['rechunked']
    assert store.chunking_configuration(doc)['max_chars'] == 300
    assert all(row['vector'] and row['metadata'] for row in store.document_chunks(doc))
    assert store.one('PRAGMA foreign_key_check') is None


@pytest.mark.parametrize('source_state', ['missing', 'changed'])
def test_rechunk_requires_matching_original_and_preserves_old_vectors(rig, source_state):
    client, app, wire = rig
    _, doc = seed(client)
    before = app.state.store.document_chunks(doc)
    source = app.state.settings.data_dir / 'documents' / (doc + '.md')
    if source_state == 'missing': source.unlink()
    else: source.write_text('Different source document')
    app.state.settings.chunk_max_chars = 600
    calls = wire.calls
    result = done(client, client.post(f'/api/documents/{doc}/index'))
    assert result['status'] == 'failed' and '原' in result['error']
    assert wire.calls == calls and app.state.store.document_chunks(doc) == before


def test_reindex_unchanged_source_reuses_valid_index_without_calls(rig):
    client, app, wire = rig
    _, doc = seed(client)
    before = app.state.store.document_chunks(doc)
    calls = wire.calls
    result = done(client, client.post(f'/api/documents/{doc}/index'))
    assert result['status'] == 'succeeded' and result['result']['reused']
    assert wire.calls == calls and app.state.store.document_chunks(doc) == before


@pytest.mark.parametrize('source_state', ['missing', 'changed'])
def test_reupload_same_original_repairs_source_without_duplicate_or_index_loss(rig, source_state):
    client, app, _ = rig
    course, doc = seed(client)
    source = app.state.settings.data_dir / 'documents' / (doc + '.md')
    original = source.read_bytes()
    chunks = app.state.store.document_chunks(doc)
    if source_state == 'missing': source.unlink()
    else: source.write_bytes(b'Corrupted original')
    response = client.post('/api/documents', data={'course_id': course}, files={'file': ('different-name.txt', original)})
    assert response.status_code == 201 and response.json()['id'] == doc
    assert response.json()['source_restored'] and response.json()['duplicate']
    assert source.read_bytes() == original and app.state.store.document_chunks(doc) == chunks
    app.state.settings.chunk_max_chars = 600
    assert done(client, client.post(f'/api/documents/{doc}/index'))['status'] == 'succeeded'


def test_settings_change_during_embedding_cannot_mislabel_vectors(rig, monkeypatch):
    from dataclasses import replace
    client, app, _ = rig
    _, doc = seed(client)
    before = app.state.store.document_chunks(doc)
    app.state.settings.chunk_max_chars = 600
    provider = app.state.pipeline.providers
    original = provider.embed

    async def changed(texts, job_id=None):
        vectors = await original(texts, job_id)
        app.state.settings.embedding = replace(app.state.settings.embedding, model='different-model')
        return vectors

    monkeypatch.setattr(provider, 'embed', changed)
    result = done(client, client.post(f'/api/documents/{doc}/index'))
    assert result['status'] == 'failed' and '配置发生变化' in result['error']
    assert app.state.store.document_chunks(doc) == before


def test_retrieval_uses_frozen_ranking_configuration_in_trace(rig, monkeypatch):
    from test_workflow import request
    client, app, _ = rig
    course, _ = seed(client)
    provider = app.state.pipeline.providers
    original = provider.embed

    async def changed(texts, job_id=None):
        vectors = await original(texts, job_id)
        app.state.settings.retrieval_strategy = 'hybrid_dense_lexical_v1'
        app.state.settings.retrieval_metadata_weight = .25
        return vectors

    monkeypatch.setattr(provider, 'embed', changed)
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'succeeded'
    evidence = client.get(f"/api/jobs/{job['id']}/evidence").json()['evidence']
    trace = evidence['retrieval']
    assert trace['configuration']['strategy'] == 'dense_v1'
    assert all(row['retrieval']['strategy'] == 'dense_v1' for row in trace['selected'])


def test_invalid_chunk_config_is_rejected_before_import_or_api(rig):
    client, app, wire = rig
    course, doc = seed(client)
    app.state.settings.chunk_overlap_chars = app.state.settings.chunk_max_chars
    calls = wire.calls
    result = done(client, client.post(f'/api/documents/{doc}/index'))
    assert result['status'] == 'failed' and wire.calls == calls
    assert client.get('/api/status').status_code == 503
    response = client.post('/api/documents', data={'course_id': course}, files={'file': ('new.md', b'new source')})
    assert response.status_code == 503


def test_chunk_settings_from_environment(monkeypatch):
    monkeypatch.setenv('CHUNK_STRATEGY', 'legacy_char_v1')
    monkeypatch.setenv('CHUNK_MAX_CHARS', '800')
    monkeypatch.setenv('CHUNK_OVERLAP_CHARS', '80')
    monkeypatch.setenv('RETRIEVAL_METADATA_WEIGHT', '.25')
    settings = Settings.from_env()
    assert (settings.chunk_strategy, settings.chunk_max_chars, settings.chunk_overlap_chars) == ('legacy_char_v1', 800, 80)
    assert settings.retrieval_metadata_weight == .25
