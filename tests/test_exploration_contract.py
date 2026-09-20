"""HTTP/account/config integration, with no live search or model requests."""
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.exploration_policy import capabilities, configuration, document_source
from app.generation_preparation import fingerprint
from app.models import GenerateRequest
from app.store import dumps
from test_workflow import rig, seed, done, authenticate, generated


def request(course_id, **values):
    return GenerateRequest(course_id=course_id, topic='Explain evidence retrieval',
        request_key='exploration-contract-key', **values)


def test_disabled_preserves_existing_payload_and_preparation_identity():
    old = request('course')
    disabled = request('course', auto_explore=False)
    enabled = request('course', auto_explore=True)
    assert old.model_dump() == disabled.model_dump()
    assert 'auto_explore' not in old.model_dump()
    assert fingerprint(old) == fingerprint(disabled) != fingerprint(enabled)
    assert enabled.model_dump()['auto_explore'] is True


@pytest.mark.parametrize('value', [None, 0, 1, 'true', 'false', [], {}])
def test_http_mode_requires_actual_boolean(value):
    with pytest.raises(ValidationError):
        request('course', auto_explore=value)


def test_search_configuration_never_exports_key():
    settings = Settings(exploration_search_provider='brave', exploration_search_api_key='test-search-secret')
    assert capabilities(settings)['web_search_configured']
    assert capabilities(settings)['available']
    assert 'test-search-secret' not in dumps(capabilities(settings)) + repr(settings)
    settings.exploration_search_api_key = ''
    assert not capabilities(settings)['available']
    settings.exploration_search_provider = 'curated'
    assert capabilities(settings)['available'] and not capabilities(settings)['web_search_configured']
    settings.exploration_enabled = False
    assert not capabilities(settings)['available']


@pytest.mark.parametrize('name,value', [('exploration_max_rounds', 0), ('exploration_max_documents', 100),
    ('exploration_max_seconds', float('nan')), ('exploration_search_provider', 'arbitrary-url')])
def test_invalid_limits_disable_capability(name, value):
    settings = Settings()
    setattr(settings, name, value)
    with pytest.raises(ValueError):
        configuration(settings)
    assert not capabilities(settings)['available']


def test_unconfigured_search_fails_before_creating_generation_job(rig):
    client, app, wire = rig
    cid = client.post('/api/courses', json={'name': 'Empty course'}).json()['id']
    app.state.settings.exploration_search_provider = 'brave'
    assert client.get('/api/status').json()['auto_exploration']['available'] is False
    response = client.post('/api/generations', json=request(cid, auto_explore=True).model_dump())
    assert response.status_code == 503
    assert not app.state.store.all('SELECT id FROM jobs') and wire.calls == 0


def test_empty_index_only_returns_gap_for_explicit_exploration(rig):
    client, app, wire = rig
    cid = client.post('/api/courses', json={'name': 'Empty course'}).json()['id']
    with pytest.raises(ValueError, match='索引'):
        client.portal.call(app.state.pipeline._retrieve_local, request(cid), 'read-only-fixture')
    assert client.portal.call(app.state.pipeline._retrieve_local,
        request(cid, auto_explore=True), 'read-only-fixture') == []
    assert wire.calls == 0


def test_invalid_index_is_not_replaced_by_web_search(rig):
    client, app, wire = rig
    cid, did = seed(client)
    app.state.store.execute('UPDATE chunks SET embedding_signature=? WHERE document_id=?', ('obsolete', did))
    before = wire.calls
    with pytest.raises(ValueError, match='嵌入配置'):
        client.portal.call(app.state.pipeline._retrieve_local,
            request(cid, auto_explore=True), 'read-only-fixture')
    assert wire.calls == before


def test_provenance_and_exploration_audit_remain_owner_scoped(rig):
    client, app, wire = rig
    cid, did = seed(client)
    store = app.state.store
    metadata = dict(url='https://example.org/course', title='Reference', provider='curated',
                    acquired_at='2026-09-20T00:00:00+00:00', private_internal='not-public')
    store.execute('INSERT INTO external_document_sources VALUES(?,?)', (did, dumps(metadata)))
    public = client.get('/api/documents', params={'course_id':cid}).json()[0]['external_source']
    assert public['url'] == metadata['url'] and 'private_internal' not in public
    owner = store.one('SELECT user_id FROM course_owners WHERE course_id=?', (cid,))['user_id']
    job, _ = store.job('exploration-audit', 'generate', request(cid).model_dump(), owner_id=owner)
    store.execute('INSERT INTO exploration_runs VALUES(?,?)', (job['id'], dumps({'reason':'fixture'})))
    assert client.get('/api/jobs/'+job['id']+'/evidence').json()['exploration'] == {'reason':'fixture'}
    other = TestClient(app); other.portal = client.portal
    try:
        authenticate(other, 'other-exploration@example.test')
        assert other.get('/api/documents', params={'course_id':cid}).status_code == 404
        assert other.get('/api/documents/'+did+'/source').status_code == 404
        assert other.get('/api/jobs/'+job['id']+'/evidence').status_code == 404
    finally:
        other.close(); other.portal = None
    store.execute('UPDATE external_document_sources SET metadata=? WHERE document_id=?',
        (dumps({'url':'javascript:alert(1)', 'title':'safe data'}), did))
    assert 'url' not in document_source(store, did)


def test_public_progress_records_exploration_counts_not_prose(rig):
    from app import job_progress
    client, app, wire = rig
    cid = client.post('/api/courses', json={'name':'Progress fixture'}).json()['id']
    owner = app.state.store.one('SELECT user_id FROM course_owners WHERE course_id=?', (cid,))['user_id']
    job, _ = app.state.store.job('exploration-progress', 'generate', request(cid, auto_explore=True).model_dump(), owner_id=owner)
    app.state.store.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
    job_progress.begin(app.state.store, job['id'], ['exploration','retrieval','writing','saving'])
    job_progress.update(app.state.store, job['id'], 'exploration', activity='searching_sources')
    job_progress.event(app.state.store, job['id'], 'exploration', 'exploration_search', round=1, queries=2, reasoning='private model text')
    job_progress.event(app.state.store, job['id'], 'exploration', 'exploration_source_accepted', accepted=1)
    timeline = client.get('/api/jobs/'+job['id']).json()['timeline']
    assert timeline['active_stage'] == 'exploration'
    assert timeline['activity'] == 'searching_sources'
    assert 'private model text' not in dumps(timeline)
    assert any(event['code']=='exploration_search' and event['data']=={'round':1,'queries':2} for event in timeline['events'])


def test_external_reference_survives_reparse_generation_and_exports(rig):
    client, app, wire = rig
    cid, did = seed(client)
    store = app.state.store
    source = dict(url='https://example.org/lesson.md', reading_url='https://example.org/lesson',
        title='Public lesson', attribution='Example author', license='CC BY 4.0',
        license_url='https://creativecommons.org/licenses/by/4.0/', provider='curated')
    store.execute('INSERT INTO external_document_sources VALUES(?,?)', (did, dumps(source)))
    assert client.post('/api/documents/'+did+'/parse').status_code == 200
    chunks = store.document_chunks(did)
    assert all(row['metadata']['source_url'] == source['url'] for row in chunks)
    assert done(client, client.post('/api/documents/'+did+'/index'))['status'] == 'succeeded'
    content_id = generated(client, cid)
    job_id = store.one('SELECT job_id FROM contents WHERE id=?', (content_id,))['job_id']
    audit = dict(status='complete', reason='local_sufficient', assessments=[{'fixture': True}])
    store.execute('INSERT INTO exploration_runs VALUES(?,?)', (job_id, dumps(audit)))
    evidence = client.get('/api/contents/'+content_id+'/evidence').json()
    assert evidence['exploration'] == audit
    assert evidence['sources'][0]['external_source']['attribution'] == source['attribution']
    exported = client.get('/api/contents/'+content_id+'/export').text
    assert source['reading_url'] in exported and source['license_url'] in exported
    assert source['attribution'] in exported
    other = TestClient(app); other.portal = client.portal
    try:
        authenticate(other, 'foreign-exports@example.test')
        assert other.get('/api/contents/'+content_id+'/evidence').status_code == 404
        assert other.get('/api/contents/'+content_id+'/export').status_code == 404
    finally:
        other.close(); other.portal = None
