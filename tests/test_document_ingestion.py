"""Owner-scoped ingestion/provenance integration using real auth and parser fixtures.

Parsing is substituted with explicit dataclasses so these tests verify storage,
transactions and HTTP boundaries, not OCR quality. Providers remain mocked.
"""
from contextlib import contextmanager
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from app import document_storage
from app.document_parsing import ParsedDocument, ParsedPage, VisualAsset
from test_workflow import authenticate, done, generated, rig

TEXT = ('# Binary search 二分查找\n\n'
        'A sorted array is required. Compare the middle value and shrink the search interval. '
        '二分查找每次将搜索区间缩小一半，前提是输入数组有序。\n\n')
ASSET_BYTES = b'\x89PNG\r\n\x1a\nfixture visual evidence only'


def fixture_document(name, *, text=TEXT, asset_id='page-1-v1', asset_data=ASSET_BYTES):
    return ParsedDocument(
        name=name, strategy='fixture_structured_parser',
        pages=[ParsedPage(number=1, text=text,
                          method='fixture_ocr', status='good' if text else 'unreadable',
                          warnings=['fixture_accuracy_not_measured'],
                          blocks=[{'type': 'text', 'text': text, 'bbox': [0, 0, 100, 80]}] if text else [],
                          asset_ids=[asset_id])],
        assets=[VisualAsset(id=asset_id, page=1, kind='page_render',
                            mime_type='image/png', data=asset_data, width=100, height=80)],
        warnings=['fixture_parser_not_real_ocr'], tools={'fixture': 'v1'}, elapsed_seconds=.0123)


@pytest.fixture
def parser_stub(monkeypatch):
    calls = []
    state = {'text': TEXT, 'asset_id': 'page-1-v1', 'asset_data': ASSET_BYTES}

    def parse(name, raw, max_pages, **kwargs):
        calls.append({'name': name, 'raw': raw, 'max_pages': max_pages, **kwargs})
        return fixture_document(name, **state)

    monkeypatch.setattr(document_storage, 'parse_document_structured', parse)
    return calls, state


def upload(client, name='scanned.pdf', raw=b'%PDF fixture original scan bytes'):
    course = client.post('/api/courses', json={'name': 'Private algorithms'}).json()['id']
    response = client.post('/api/documents', data={'course_id': course},
                           files={'file': (name, raw)})
    assert response.status_code == 201, response.text
    return course, response.json(), raw


@contextmanager
def separate_client(app, running_client):
    client = TestClient(app)
    client.portal = running_client.portal
    try:
        yield client
    finally:
        client.close()
        client.portal = None


def files_under(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()} if path.exists() else {}


def snapshot(app, document_id):
    store = app.state.store
    return {
        'document': store.one('SELECT * FROM documents WHERE id=?', (document_id,)),
        'chunks': store.document_chunks(document_id),
        'configuration': store.chunking_configuration(document_id),
        'report': document_storage.report_for(store, document_id),
        'asset_registry': store.all('SELECT * FROM document_visual_assets WHERE document_id=? ORDER BY asset_id', (document_id,)),
        'assets': files_under(app.state.settings.data_dir / 'document_assets' / document_id),
    }


@pytest.mark.parametrize('name,raw', [
    ('scanned.pdf', b'%PDF isolated scan fixture'),
    ('photo.jpg', b'\xff\xd8isolated photo fixture\xff\xd9'),
])
def test_upload_persists_source_pages_assets_and_chunk_provenance(rig, parser_stub, name, raw):
    client, app, wire = rig
    calls, _ = parser_stub
    course, document, raw = upload(client, name, raw)
    did = document['id']
    assert wire.calls == 0
    assert len(calls) == 1 and calls[0]['raw'] == raw
    assert calls[0]['strategy'] == 'adaptive_local_v1'
    assert calls[0]['ocr_engine'] == app.state.settings.document_ocr_engine
    assert calls[0]['max_ocr_pages'] == app.state.settings.document_ocr_max_pages
    assert document['pages'] == 1 and document['chunks'] >= 1
    assert document['parsing']['status'] == 'good'
    assert document['parsing']['metrics']['recognition_accuracy'] is None
    report_response = client.get(f'/api/documents/{did}/parsing')
    assert report_response.status_code == 200
    report = report_response.json()
    assert report['source_document_sha256'] == hashlib.sha256(raw).hexdigest()
    assert report['pages'][0]['text'] == TEXT
    assert report['pages'][0]['blocks'][0]['bbox'] == [0, 0, 100, 80]
    assert 'storage_name' not in json.dumps(report)
    assert str(app.state.settings.data_dir) not in report_response.text
    source = client.get(report['source_url'])
    assert source.content == raw
    assert 'attachment' in source.headers['content-disposition']
    assert source.headers['cache-control'] == 'no-store'
    asset = report['assets'][0]
    visual = client.get(asset['url'])
    assert visual.status_code == 200 and visual.content == ASSET_BYTES
    assert visual.headers['content-type'] == 'image/png'
    assert asset['sha256'] == hashlib.sha256(ASSET_BYTES).hexdigest()
    assert visual.headers['cache-control'] == 'no-store'
    assert client.get(f'/api/documents/{did}/assets/missing').status_code == 404
    chunks = client.get(f'/api/documents/{did}/chunks').json()
    for chunk in chunks:
        metadata = chunk['metadata']
        assert metadata['source_document_sha256'] == hashlib.sha256(raw).hexdigest()
        assert metadata['source_asset_ids'] == [asset['id']]
        assert metadata['extraction_method'] == 'fixture_ocr'
        assert metadata['ocr_accuracy_verified'] is False
        assert 'vector' not in chunk
    listing = client.get('/api/documents', params={'course_id': course}).json()[0]
    assert listing['parsing'] == document['parsing']
    assert not listing['index_current']


@pytest.mark.parametrize('method,suffix', [
    ('GET', '/parsing'), ('GET', '/source'),
    ('GET', '/assets/page-1-v1'), ('POST', '/parse'),
])
def test_new_document_routes_require_authentication_before_lookup(rig, method, suffix):
    client, app, wire = rig
    with separate_client(app, client) as anonymous:
        response = anonymous.request(method, '/api/documents/unknown' + suffix)
    assert response.status_code == 401
    assert response.json()['code'] == 'authentication_required'
    assert wire.calls == 0


def test_all_new_routes_reject_foreign_document_without_parser_or_data_leaks(rig, parser_stub):
    client, app, wire = rig
    calls, _ = parser_stub
    _, document, raw = upload(client)
    did = document['id']
    before = snapshot(app, did)
    with separate_client(app, client) as other:
        authenticate(other, email='other-parser-account@example.test')
        for method, suffix in [('GET', '/parsing'), ('GET', '/source'),
                               ('GET', '/assets/page-1-v1'), ('POST', '/parse')]:
            foreign = other.request(method, f'/api/documents/{did}' + suffix)
            missing = other.request(method, '/api/documents/unknown' + suffix)
            assert foreign.status_code == missing.status_code == 404
            assert foreign.json() == missing.json()
            assert document['name'] not in foreign.text
            assert TEXT not in foreign.text
            assert str(app.state.settings.data_dir) not in foreign.text
    assert len(calls) == 1 and wire.calls == 0
    assert snapshot(app, did) == before


def test_reparse_requires_csrf_before_parser_execution(rig, parser_stub):
    client, app, _ = rig
    calls, _ = parser_stub
    _, document, _ = upload(client)
    response = client.post(f"/api/documents/{document['id']}/parse", headers={'X-CSRF-Token': 'wrong'})
    assert response.status_code == 403 and response.json()['code'] == 'csrf_invalid'
    assert len(calls) == 1


def test_unreadable_photo_is_retained_for_review_but_cannot_call_embedding(rig, parser_stub):
    client, app, wire = rig
    calls, state = parser_stub
    state['text'] = ''
    course, document, raw = upload(client, 'unreadable.png', b'fixture original photo')
    did = document['id']
    assert document['chunks'] == 0 and document['parsing']['status'] == 'unreadable'
    assert document['parsing']['metrics']['pages_with_text'] == 0
    report = client.get(f'/api/documents/{did}/parsing').json()
    assert client.get(report['source_url']).content == raw
    assert client.get(report['assets'][0]['url']).content == ASSET_BYTES
    assert client.get(f'/api/documents/{did}/chunks').json() == []
    job = done(client, client.post(f'/api/documents/{did}/index'))
    assert job['status'] == 'failed' and '没有可索引片段' in job['error']
    assert wire.calls == 0 and len(calls) == 1
    listing = client.get('/api/documents', params={'course_id': course}).json()[0]
    assert listing['parsing']['status'] == 'unreadable' and not listing['index_current']


def test_rechunk_uses_stored_pages_and_never_reexecutes_ocr(rig, parser_stub, monkeypatch):
    client, app, wire = rig
    calls, state = parser_stub
    state['text'] = TEXT * 12
    _, document, _ = upload(client)
    did = document['id']
    assert done(client, client.post(f'/api/documents/{did}/index'))['status'] == 'succeeded'
    before = snapshot(app, did)
    app.state.settings.chunk_max_chars = 300
    app.state.settings.chunk_overlap_chars = 30

    def forbidden(*args, **kwargs):
        raise AssertionError('Rechunk must not perform OCR or reparse source pages.')

    monkeypatch.setattr(document_storage, 'parse_document_structured', forbidden)
    monkeypatch.setattr('app.pipeline.parse_document', forbidden)
    job = done(client, client.post(f'/api/documents/{did}/index'))
    assert job['status'] == 'succeeded' and job['result']['rechunked']
    after = snapshot(app, did)
    assert len(calls) == 1 and wire.calls >= 2
    assert after['report'] == before['report'] and after['assets'] == before['assets']
    assert after['configuration']['max_chars'] == 300
    assert all(row['vector'] for row in after['chunks'])
    assert all(row['metadata']['source_asset_ids'] == ['page-1-v1'] for row in after['chunks'])


@pytest.mark.parametrize('legacy_registry', [False, True])
def test_explicit_reparse_invalidates_vectors_preserves_generated_evidence(rig, parser_stub, legacy_registry):
    client, app, wire = rig
    calls, state = parser_stub
    course, document, raw = upload(client)
    did = document['id']
    assert done(client, client.post(f'/api/documents/{did}/index'))['status'] == 'succeeded'
    cid = generated(client, course)
    original_content = app.state.store.one('SELECT * FROM contents WHERE id=?', (cid,))
    if legacy_registry:
        # Simulate a persisted report predating the additive visual registry.
        app.state.store.execute('DELETE FROM document_visual_assets WHERE document_id=?', (did,))
    before = snapshot(app, did)
    assert all(row['vector'] for row in before['chunks'])
    state.update(text=TEXT + '\nA new verified parsing fixture region.\n', asset_id='page-1-v2',
                 asset_data=ASSET_BYTES + b'new version')
    provider_calls = wire.calls
    response = client.post(f'/api/documents/{did}/parse')
    assert response.status_code == 200, response.text
    assert response.json()['index_rebuild_required'] is True
    assert len(calls) == 2 and wire.calls == provider_calls
    after = snapshot(app, did)
    assert after['document']['status'] == 'parsed'
    assert all(row['vector'] is None and row['embedding_signature'] is None for row in after['chunks'])
    assert after['report']['pages'][0]['text'] == state['text']
    assert after['report']['source_document_sha256'] == hashlib.sha256(raw).hexdigest()
    assert client.get(f'/api/documents/{did}/assets/page-1-v2').content == state['asset_data']
    assert state['asset_data'] != ASSET_BYTES
    # A historical citation must retrieve its original image, not the current one.
    historical = client.get(f'/api/documents/{did}/assets/page-1-v1')
    assert historical.status_code == 200 and historical.content == ASSET_BYTES
    assert {row['asset_id'] for row in after['asset_registry']} == {'page-1-v1', 'page-1-v2'}
    assert all(source['metadata']['source_asset_ids'] == ['page-1-v1']
               for source in json.loads(original_content['sources']))
    with separate_client(app, client) as other:
        authenticate(other, email='historical-image-outsider@example.test')
        for asset_id in ('page-1-v1', 'page-1-v2'):
            forbidden = other.get(f'/api/documents/{did}/assets/{asset_id}')
            assert forbidden.status_code == 404
            assert ASSET_BYTES not in forbidden.content
            assert str(app.state.settings.data_dir) not in forbidden.text
    assert app.state.store.one('SELECT * FROM contents WHERE id=?', (cid,)) == original_content
    assert client.get(f'/api/contents/{cid}/evidence').json()['sources'] == json.loads(original_content['sources'])
    assert done(client, client.post(f'/api/documents/{did}/index'))['status'] == 'succeeded'
    assert all(row['vector'] for row in app.state.store.document_chunks(did))
    assert len(calls) == 2


def test_reparse_repairs_missing_same_id_visual_without_changing_owner_access(rig, parser_stub):
    client, app, wire = rig
    _, document, original_source = upload(client)
    did = document['id']
    first_report = client.get(f'/api/documents/{did}/parsing').json()
    asset = first_report['assets'][0]
    original_url = asset['url']
    assert client.get(original_url).status_code == 200
    registry_before = app.state.store.one(
        'SELECT * FROM document_visual_assets WHERE document_id=? AND asset_id=?', (did, asset['id']))
    previous_metadata = document_storage.asset_for(app.state.store, did, asset['id'])
    asset_directory = app.state.settings.data_dir / 'document_assets' / did
    previous_file = asset_directory / previous_metadata['storage_name']
    previous_file.unlink()
    assert client.get(original_url).status_code == 404

    # Identical source pixels produce the same content-addressed asset ID but a
    # new parse directory. Existing citation URLs must resolve the restored copy.
    response = client.post(f'/api/documents/{did}/parse')
    assert response.status_code == 200, response.text
    latest_report = document_storage.report_for(app.state.store, did)
    latest_asset = latest_report['assets'][0]
    assert latest_asset['id'] == asset['id']
    restored_file = asset_directory / latest_asset['storage_name']
    assert restored_file != previous_file and restored_file.is_file()
    restored = client.get(original_url)
    assert restored.status_code == 200
    assert restored.content == restored_file.read_bytes() == ASSET_BYTES
    assert client.get(f'/api/documents/{did}/source').content == original_source
    registry_after = app.state.store.one(
        'SELECT * FROM document_visual_assets WHERE document_id=? AND asset_id=?', (did, asset['id']))
    assert registry_after['created_at'] == registry_before['created_at']
    assert json.loads(registry_after['metadata'])['storage_name'] == latest_asset['storage_name']
    with separate_client(app, client) as other:
        authenticate(other, email='restored-image-outsider@example.test')
        forbidden = other.get(original_url)
        assert forbidden.status_code == 404
        assert ASSET_BYTES not in forbidden.content
    assert wire.calls == 0


@pytest.mark.parametrize('failure_stage', ['parser', 'database'])
def test_failed_reparse_preserves_old_report_assets_and_ready_index(rig, parser_stub, monkeypatch, failure_stage):
    client, app, wire = rig
    calls, state = parser_stub
    _, document, _ = upload(client)
    did = document['id']
    assert done(client, client.post(f'/api/documents/{did}/index'))['status'] == 'succeeded'
    before = snapshot(app, did)
    provider_calls = wire.calls
    state.update(text=TEXT + 'new content', asset_id='page-1-new')
    if failure_stage == 'parser':
        def fail(*args, **kwargs):
            raise ValueError('fixture parse failed')
        monkeypatch.setattr(document_storage, 'parse_document_structured', fail)
        response = client.post(f'/api/documents/{did}/parse')
        assert response.status_code == 400
    else:
        original_save_report = document_storage.save_report

        def fail(db, document_id, report):
            original_save_report(db, document_id, report)
            assert db.execute('SELECT asset_id FROM document_visual_assets WHERE document_id=? AND asset_id=?',
                              (document_id, 'page-1-new')).fetchone()
            raise RuntimeError('fixture database write failed')
        monkeypatch.setattr(document_storage, 'save_report', fail)
        with pytest.raises(RuntimeError, match='fixture database write failed'):
            client.post(f'/api/documents/{did}/parse')
    assert snapshot(app, did) == before
    assert wire.calls == provider_calls
    assert client.get(f'/api/documents/{did}/assets/page-1-v1').content == ASSET_BYTES
    assert app.state.store.one('PRAGMA foreign_key_check') is None


def test_failed_upload_rolls_back_database_source_and_assets(rig, parser_stub, monkeypatch):
    client, app, wire = rig
    course = client.post('/api/courses', json={'name': 'Rollback course'}).json()['id']
    before_sources = files_under(app.state.settings.data_dir / 'documents')
    before_assets = files_under(app.state.settings.data_dir / 'document_assets')

    def fail(*args, **kwargs):
        raise RuntimeError('fixture report write failed')

    monkeypatch.setattr(document_storage, 'save_report', fail)
    with pytest.raises(RuntimeError, match='fixture report write failed'):
        client.post('/api/documents', data={'course_id': course}, files={'file': ('scan.pdf', b'fixture')})
    assert app.state.store.all('SELECT * FROM documents') == []
    assert app.state.store.all('SELECT * FROM chunks') == []
    assert app.state.store.all('SELECT * FROM document_parsing') == []
    assert app.state.store.all('SELECT * FROM document_visual_assets') == []
    assert files_under(app.state.settings.data_dir / 'documents') == before_sources
    assert files_under(app.state.settings.data_dir / 'document_assets') == before_assets
    assert wire.calls == 0


@pytest.mark.parametrize('name', ['scan-with-context.pdf', 'photo-with-context.jpg'])
def test_r4_context_from_ocr_cache_generates_with_extraction_quality_contract(rig, parser_stub, monkeypatch, tmp_path, name):
    from test_r4_runtime import configure
    from test_workflow import request

    client, app, wire = rig
    calls, state = parser_stub
    configure(app, tmp_path)
    state['text'] = TEXT * 3
    course, document, _ = upload(client, name, b'fixture image pixels, not a text layer')
    did = document['id']
    assert done(client, client.post(f'/api/documents/{did}/index'))['status'] == 'succeeded'
    generation_messages = []
    provider = app.state.pipeline.providers
    original_generate = provider.generate

    async def spy_generate(messages, job_id=None):
        generation_messages.append(json.loads(json.dumps(messages)))
        return await original_generate(messages, job_id)

    def forbidden(*args, **kwargs):
        raise AssertionError('Context assembly must read the saved OCR report, not parse the photo/PDF again.')

    monkeypatch.setattr(provider, 'generate', spy_generate)
    monkeypatch.setattr(document_storage, 'parse_document_structured', forbidden)
    monkeypatch.setattr('app.pipeline.read_document_pages', forbidden)
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'succeeded', job
    assert len(calls) == 1
    content = client.get('/api/contents/' + job['result']['content_id']).json()
    assert content['sources']
    for source in content['sources']:
        metadata = source['metadata']
        assert source['id'].startswith('context_') and source['document_id'] == did
        assert state['text'][metadata['page_char_start']:metadata['page_char_end']] == source['text']
        assert metadata['extraction_method'] == 'fixture_ocr'
        assert metadata['source_asset_ids'] == ['page-1-v1']
    evidence = client.get('/api/jobs/' + job['id'] + '/evidence').json()['evidence']
    assert evidence['retrieval']['context_tokens'] <= app.state.settings.retrieval_context_tokens
    assert evidence['source_quality_contract'] == 'document-source-quality-v1'
    assert content['config']['source_quality_contract'] == 'document-source-quality-v1'
    generation = next(messages for messages in generation_messages
                      if 'request' in json.loads(messages[1]['content']))
    instruction = json.loads(generation[1]['content'])
    for reference in instruction['reference_chunks']:
        extraction = reference['source_extraction']
        assert extraction['method'] == 'fixture_ocr'
        assert extraction['visual_content_interpreted'] is False
        assert extraction['recognition_accuracy_verified'] is False
        assert 'fixture_accuracy_not_measured' in extraction['warnings']
    assert '未作为可理解的图像输入' in generation[0]['content']


def test_r4_context_rejects_mismatched_cached_report_before_text_generation(rig, parser_stub, monkeypatch, tmp_path):
    from test_r4_runtime import configure
    from test_workflow import request

    client, app, wire = rig
    calls, _ = parser_stub
    configure(app, tmp_path)
    course, document, _ = upload(client, 'tampered-report.jpg', b'original photo fixture')
    did = document['id']
    assert done(client, client.post(f'/api/documents/{did}/index'))['status'] == 'succeeded'
    old_chunks = app.state.store.document_chunks(did)
    report = document_storage.report_for(app.state.store, did)
    report['source_document_sha256'] = '0' * 64
    app.state.store.execute('UPDATE document_parsing SET report=? WHERE document_id=?',
                            (json.dumps(report), did))
    called_text = []

    async def forbidden_generation(*args, **kwargs):
        called_text.append(True)
        raise AssertionError('An inconsistent source report must not reach text generation.')

    def forbidden_parse(*args, **kwargs):
        raise AssertionError('A bad report must fail instead of silently reparsing its original.')

    monkeypatch.setattr(app.state.pipeline.providers, 'generate', forbidden_generation)
    monkeypatch.setattr(document_storage, 'parse_document_structured', forbidden_parse)
    monkeypatch.setattr('app.pipeline.read_document_pages', forbidden_parse)
    job = done(client, client.post('/api/generations', json=request(course)))
    assert job['status'] == 'failed' and '解析报告与原始资料不一致' in job['error']
    assert not called_text and len(calls) == 1
    assert app.state.store.document_chunks(did) == old_chunks
    assert client.get('/api/contents', params={'course_id': course}).json() == []
    assert all(call['capability'] != 'text' for call in app.state.store.calls_for_job(job['id']))
