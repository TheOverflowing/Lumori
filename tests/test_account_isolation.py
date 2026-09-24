"""Account boundaries over real HTTP routes, SQLite, and mocked provider calls.

The app and cookie jars are real. All data lives in pytest temporary directories;
HTTP model transport is mocked and no configured production services are loaded.
"""
import csv
import io
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Endpoint, Settings
from app.main import create_app
from app.store import dumps, now, uid
from test_workflow import Wire, done, request

PASSWORD = 'An isolated account password 42!'

# Every business route must reject anonymous requests before inspecting inputs.
BUSINESS_ROUTES = [
    ('PATCH','/api/documents/{did}'),
    ('DELETE','/api/documents/{did}'),
    ('POST','/api/documents/{did}/restore'),
    ('GET', '/api/documents/{did}/figures'),
    ('POST', '/api/documents/{did}/figures/retry'),
    ('PATCH', '/api/documents/{did}/figures/{aid}'),
    ('GET', '/api/status'),
    ('GET', '/api/courses'),
    ('POST', '/api/courses'),
    ('GET', '/api/documents'),
    ('POST', '/api/documents'),
    ('GET', '/api/documents/{did}/chunks'),
    ('GET', '/api/documents/{did}/parsing'),
    ('GET', '/api/documents/{did}/source'),
    ('GET', '/api/documents/{did}/assets/{aid}'),
    ('POST', '/api/documents/{did}/parse'),
    ('POST', '/api/documents/{did}/index'),
    ('POST', '/api/generations'),
    ('POST', '/api/generations/prepare'),
    ('GET', '/api/jobs'),
    ('GET', '/api/jobs/{jid}'),
    ('GET', '/api/jobs/{jid}/evidence'),
    ('POST', '/api/jobs/{jid}/resume'),
    ('POST', '/api/jobs/{jid}/cancel'),
    ('GET', '/api/jobs/{jid}/partial-content'),
    ('GET', '/api/jobs/{jid}/generation-request'),
    ('GET', '/api/contents'),
    ('GET', '/api/contents/{cid}'),
    ('POST', '/api/contents/{cid}/review'),
    ('POST', '/api/contents/{cid}/questions/{slot_id}/revise'),
    ('POST', '/api/contents/{cid}/media'),
    ('GET', '/api/media/{mid}/file'),
    ('GET', '/api/media/{mid}/download'),
    ('POST', '/api/media/{mid}/approve'),
    ('GET', '/api/learn/{cid}'),
    ('POST', '/api/contents/{cid}/evaluations'),
    ('GET', '/api/difficulty/evaluations/export'),
    ('GET', '/api/evaluations/export'),
    ('GET', '/api/contents/{cid}/evidence'),
    ('GET', '/api/contents/{cid}/export'),
]


def isolated_app(path):
    settings = Settings(data_dir=path, workers=1)
    for capability, endpoint_path in [
        ('text', '/chat/completions'), ('embedding', '/embeddings'),
        ('speech', '/audio/speech'), ('image', '/images/generations'),
    ]:
        setattr(settings, capability, Endpoint(
            'https://test.invalid/v1', 'test-key-not-real', 'fixture-model', endpoint_path))
    wire = Wire()
    app = create_app(settings, lambda s, db: ApiProvidersForFixture(s, db, wire))
    return app, wire


def ApiProvidersForFixture(settings, store, wire):
    from app.providers import ApiProviders
    return ApiProviders(settings, store, httpx.AsyncClient(transport=httpx.MockTransport(wire)))


@contextmanager
def another_client(app, running_client):
    """Use one app lifespan/worker loop while keeping each HTTP cookie jar separate."""
    client = TestClient(app)
    client.portal = running_client.portal
    try:
        yield client
    finally:
        client.close()
        client.portal = None


def register(client, email, name):
    response = client.post('/api/auth/register', json={
        'email': email, 'password': PASSWORD, 'display_name': name,
    })
    assert response.status_code == 201, response.text
    session = client.get('/api/auth/session').json()
    assert session['user']['email'] == email.lower()
    assert isinstance(session['csrf_token'], str) and session['csrf_token']
    client.headers['X-CSRF-Token'] = session['csrf_token']
    return session


def seed_account(client, name):
    course_response = client.post('/api/courses', json={'name': name + ' private course'})
    assert course_response.status_code == 201, course_response.text
    course = course_response.json()['id']
    document_response = client.post('/api/documents', data={'course_id': course},
        files={'file': (name + '-notes.md', (name + ' PRIVATE: 检索为生成提供课程参考资料。').encode())})
    assert document_response.status_code == 201, document_response.text
    document = document_response.json()['id']
    index_job = done(client, client.post('/api/documents/' + document + '/index'))
    assert index_job['status'] == 'succeeded', index_job
    generation = done(client, client.post('/api/generations', json=request(course, 'same-generation-request')))
    assert generation['status'] == 'succeeded', generation
    content = generation['content_id']
    assert client.post('/api/contents/' + content + '/review',
        json={'version': 1, 'action': 'approve'}).status_code == 200
    media_job = done(client, client.post('/api/contents/' + content + '/media',
        json={'version': 1, 'kind': 'audio', 'request_key': 'same-media-request'}))
    assert media_job['status'] == 'succeeded', media_job
    asset = client.get('/api/contents/' + content).json()
    media = asset['media'][0]['id']
    assert client.post('/api/media/' + media + '/approve', json={'version': 1}).status_code == 200
    slot = asset['asset']['questions'][0]['slot_id']
    rating = client.post('/api/contents/' + content + '/evaluations', json={
        'version': 1, 'correctness': 5, 'groundedness': 4, 'difficulty_match': 3,
        'notes': name + ' PRIVATE feedback',
        'question_difficulties': [{'slot_id': slot, 'assessed_difficulty': 'medium'}],
    })
    assert rating.status_code == 201, rating.text
    return {'course': course, 'document': document, 'content': content, 'media': media,
        'jobs': {index_job['id'], generation['id'], media_job['id']},
        'generation': generation['id'], 'media_job': media_job['id'], 'evaluation': rating.json()['id']}


@pytest.fixture
def account_rig(tmp_path):
    app, wire = isolated_app(tmp_path)
    with TestClient(app) as alice:
        alice_session = register(alice, 'alice@example.test', 'Alice')
        with another_client(app, alice) as bob:
            bob_session = register(bob, 'bob@example.test', 'Bob')
            with another_client(app, alice) as anonymous:
                yield alice, bob, anonymous, app, wire, alice_session, bob_session


@pytest.fixture
def populated(account_rig):
    alice, bob, anonymous, app, wire, alice_session, bob_session = account_rig
    first = seed_account(alice, 'ALICE')
    second = seed_account(bob, 'BOB')
    return account_rig, first, second


@pytest.mark.parametrize('method,path', BUSINESS_ROUTES)
def test_all_business_routes_require_a_session(tmp_path, method, path):
    app, wire = isolated_app(tmp_path)
    path = path.format(did='unknown-document', jid='unknown-job', cid='unknown-content', mid='unknown-media', aid='unknown-asset', slot_id='q1')
    with TestClient(app) as client:
        result = client.request(method, path)
        assert result.status_code == 401, (method, path, result.text)
        assert wire.calls == 0


def test_claim_legacy_requires_a_session(tmp_path):
    app, _ = isolated_app(tmp_path)
    with TestClient(app) as client:
        response = client.post('/api/auth/claim-legacy', json={'token': 'untrusted'})
        assert response.status_code == 401, response.text


def test_business_route_inventory_has_an_anonymous_test(tmp_path):
    app, _ = isolated_app(tmp_path)
    actual = {(method, route.path) for route in app.routes
        if hasattr(route, 'methods') and route.path.startswith('/api/')
        and not route.path.startswith('/api/auth/')
        for method in route.methods if method not in {'HEAD', 'OPTIONS'}}
    assert actual == set(BUSINESS_ROUTES), 'Update the isolation matrix and anonymous coverage for changed APIs.'


def test_public_shell_and_anonymous_session_remain_available(tmp_path):
    app, _ = isolated_app(tmp_path)
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        session = client.get('/api/auth/session')
        assert session.status_code == 200 and session.json()['user'] is None
        assert session.headers['cache-control'] == 'no-store'


def test_registration_stores_password_hash_and_sets_private_cookie(account_rig):
    alice, bob, anonymous, app, _, first, second = account_rig
    assert first['user']['id'] != second['user']['id']
    assert anonymous.get('/api/auth/session').json()['user'] is None
    assert first['csrf_token'] != second['csrf_token']
    rows = app.state.store.all('SELECT * FROM users ORDER BY email')
    assert len(rows) == 2
    assert all(PASSWORD not in dumps(row) for row in rows)
    assert rows[0]['password_hash'] != rows[1]['password_hash'], 'Each account needs a random salt.'
    assert all(row['password_hash'].startswith('scrypt$') for row in rows)
    for client, session in [(alice, first), (bob, second)]:
        assert 'password' not in json.dumps(session)
        raw_token = client.cookies.get(app.state.settings.auth_cookie_name)
        assert raw_token and raw_token not in dumps(app.state.store.all('SELECT * FROM auth_sessions'))
        assert raw_token not in session['csrf_token']
    # Cookie attributes must be tested on an actual issuance response, not a mock.
    assert alice.post('/api/auth/logout').status_code in (200, 204)
    response = alice.post('/api/auth/login', json={'email': 'alice@example.test', 'password': PASSWORD})
    assert response.status_code == 200, response.text
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=lax' in cookie and 'path=/' in cookie


def test_registration_validation_duplicate_and_login_identity(account_rig):
    alice, _, anonymous, _, _, _, _ = account_rig
    for payload in [
        {'email': 'invalid', 'password': PASSWORD, 'display_name': 'Person'},
        {'email': 'new@example.test', 'password': 'tiny-secret-1', 'display_name': 'Person'},
        {'email': 'new@example.test', 'password': PASSWORD, 'display_name': ''},
    ]:
        rejected = anonymous.post('/api/auth/register', json=payload)
        assert rejected.status_code in (400, 422)
        assert payload['password'] not in rejected.text
    duplicate = anonymous.post('/api/auth/register', json={
        'email': 'ALICE@EXAMPLE.TEST', 'password': PASSWORD, 'display_name': 'Impersonator'})
    assert duplicate.status_code == 409
    wrong = anonymous.post('/api/auth/login', json={'email': 'alice@example.test', 'password': 'wrong password'})
    missing = anonymous.post('/api/auth/login', json={'email': 'nobody@example.test', 'password': 'wrong password'})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json(), 'Do not distinguish unknown email from incorrect password.'
    success = anonymous.post('/api/auth/login', json={'email': 'ALICE@EXAMPLE.TEST', 'password': PASSWORD})
    assert success.status_code == 200
    assert anonymous.get('/api/auth/session').json()['user']['id'] == alice.get('/api/auth/session').json()['user']['id']


def test_csrf_token_cannot_be_missing_or_copied_from_another_account(account_rig):
    alice, bob, _, app, wire, first, second = account_rig
    saved = alice.headers.pop('X-CSRF-Token')
    for token in (None, 'invalid-csrf-token', second['csrf_token']):
        headers = {} if token is None else {'X-CSRF-Token': token}
        response = alice.post('/api/courses', json={'name': 'must not exist'}, headers=headers)
        assert response.status_code == 403, response.text
    alice.headers['X-CSRF-Token'] = saved
    assert alice.post('/api/courses', json={'name': 'valid own course'}).status_code == 201
    assert len(app.state.store.all('SELECT * FROM courses')) == 1
    assert wire.calls == 0


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.invalid'},
    {'Sec-Fetch-Site': 'cross-site'},
])
def test_cross_site_auth_and_business_mutations_are_rejected(account_rig, headers):
    alice, _, anonymous, app, _, _, _ = account_rig
    attempts = [
        (anonymous, '/api/auth/login', {'email': 'alice@example.test', 'password': PASSWORD}),
        (anonymous, '/api/auth/register', {'email': 'mallory@example.test', 'password': PASSWORD, 'display_name': 'Mallory'}),
        (alice, '/api/auth/logout', {}),
        (alice, '/api/courses', {'name': 'injected'}),
    ]
    for client, path, payload in attempts:
        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 403, (path, response.text)
    assert len(app.state.store.all('SELECT * FROM users')) == 2
    assert app.state.store.all('SELECT * FROM courses') == []
    assert alice.get('/api/auth/session').json()['user'] is not None


def test_stale_account_header_cannot_mix_new_cookie_with_previous_account(account_rig):
    alice, _, _, app, _, first, second = account_rig
    for method, path, kwargs in [
        ('GET', '/api/courses', {}),
        ('POST', '/api/courses', {'json': {'name': 'mixed tab identity'}}),
    ]:
        response = alice.request(method, path, headers={'X-Account-ID': second['user']['id']}, **kwargs)
        assert response.status_code == 401, response.text
    assert app.state.store.all('SELECT * FROM courses') == []
    assert alice.get('/api/courses', headers={'X-Account-ID': first['user']['id']}).status_code == 200


def test_logout_invalidates_copied_session_and_csrf(account_rig):
    alice, _, anonymous, app, _, first, _ = account_rig
    anonymous.cookies.update(alice.cookies)
    anonymous.headers['X-CSRF-Token'] = first['csrf_token']
    assert anonymous.get('/api/courses').status_code == 200
    assert alice.post('/api/auth/logout').status_code in (200, 204)
    assert alice.get('/api/auth/session').json()['user'] is None
    assert anonymous.get('/api/courses').status_code == 401
    assert anonymous.post('/api/courses', json={'name': 'replayed'}).status_code == 401
    assert app.state.store.all('SELECT * FROM courses') == []


def test_expired_session_and_tampered_cookie_are_rejected(account_rig):
    alice, bob, anonymous, app, _, first, _ = account_rig
    app.state.store.execute('UPDATE auth_sessions SET expires_at=0 WHERE user_id=?', (first['user']['id'],))
    assert alice.get('/api/auth/session').json()['user'] is None
    assert alice.get('/api/courses').status_code == 401
    anonymous.cookies.set(app.state.settings.auth_cookie_name, 'forged-session-token')
    assert anonymous.get('/api/courses').status_code == 401
    assert bob.get('/api/courses').status_code == 200


def test_owned_lists_details_evidence_csv_and_call_counts_are_scoped(populated):
    rig, first, second = populated
    alice, bob, _, app, _, _, _ = rig
    for client, own, foreign, own_name, foreign_name in [
        (alice, first, second, 'ALICE', 'BOB'), (bob, second, first, 'BOB', 'ALICE'),
    ]:
        assert {x['id'] for x in client.get('/api/courses').json()} == {own['course']}
        assert {x['id'] for x in client.get('/api/documents', params={'course_id': own['course']}).json()} == {own['document']}
        assert {x['id'] for x in client.get('/api/contents').json()} == {own['content']}
        assert {x['id'] for x in client.get('/api/jobs').json()} == own['jobs']
        content = client.get('/api/contents/' + own['content']).json()
        assert {x['document_id'] for x in content['sources']} == {own['document']}
        assert client.get('/api/media/' + own['media'] + '/file').status_code == 200
        assert client.get('/api/learn/' + own['content']).status_code == 200
        for path in [
            '/api/contents/' + own['content'] + '/evidence',
            '/api/jobs/' + own['generation'] + '/evidence',
            '/api/contents/' + own['content'] + '/export',
        ]:
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert foreign['document'] not in response.text and foreign_name + ' PRIVATE' not in response.text
        for path in ['/api/evaluations/export', '/api/difficulty/evaluations/export']:
            response = client.get(path)
            assert response.status_code == 200
            rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
            assert len(rows) == 1 and rows[0]['content_id'] == own['content']
            assert foreign['content'] not in response.text and foreign_name + ' PRIVATE' not in response.text
        expected_calls = sum(len(app.state.store.calls_for_job(jid)) for jid in own['jobs'])
        assert client.get('/api/status').json()['calls_today'] == expected_calls
        assert expected_calls < len(app.state.store.all('SELECT * FROM calls'))


def test_every_foreign_object_surface_looks_missing_without_side_effects(populated):
    rig, first, second = populated
    alice, bob, _, app, wire, _, _ = rig
    for attacker, foreign in [(alice, second), (bob, first)]:
        cid, did, mid = foreign['content'], foreign['document'], foreign['media']
        operations = [
            ('GET', '/api/documents', {'params': {'course_id': foreign['course']}}),
            ('POST', '/api/documents', {'data': {'course_id': foreign['course']}, 'files': {'file': ('intrusion.md', b'private')}}),
            ('GET', '/api/documents/' + did + '/chunks', {}),
            ('POST', '/api/documents/' + did + '/index', {}),
            ('POST', '/api/generations', {'json': request(foreign['course'], 'unauthorized-generation')}),
            ('GET', '/api/contents', {'params': {'course_id': foreign['course']}}),
            ('GET', '/api/jobs', {'params': {'course_id': foreign['course']}}),
            ('GET', '/api/contents/' + cid, {}),
            ('POST', '/api/contents/' + cid + '/review', {'json': {'version': 1, 'action': 'approve'}}),
            ('POST', '/api/contents/' + cid + '/review', {'json': {'version': 999, 'action': 'save'}}),
            ('POST', '/api/contents/' + cid + '/media', {'json': {'version': 1, 'kind': 'audio', 'request_key': 'unauthorized-media'}}),
            ('GET', '/api/media/' + mid + '/file', {}),
            ('POST', '/api/media/' + mid + '/approve', {'json': {'version': 1}}),
            ('GET', '/api/learn/' + cid, {}),
            ('GET', '/api/learn/' + cid + '?reveal_answers=true', {}),
            ('POST', '/api/contents/' + cid + '/evaluations', {'json': {'version': 1, 'correctness': 1, 'groundedness': 1, 'difficulty_match': 1}}),
            ('GET', '/api/contents/' + cid + '/evidence', {}),
            ('GET', '/api/contents/' + cid + '/export', {}),
        ]
        for jid in foreign['jobs']:
            operations.extend([('GET', '/api/jobs/' + jid, {}), ('GET', '/api/jobs/' + jid + '/evidence', {})])
        before_calls = wire.calls
        before = {table: app.state.store.all('SELECT * FROM ' + table) for table in
            ['courses', 'documents', 'chunks', 'jobs', 'contents', 'revisions', 'media', 'evaluations', 'calls']}
        for method, path, kwargs in operations:
            response = attacker.request(method, path, **kwargs)
            assert response.status_code == 404, (method, path, response.status_code, response.text)
            assert did not in response.text and cid not in response.text
        assert wire.calls == before_calls
        assert before == {table: app.state.store.all('SELECT * FROM ' + table) for table in before}


def test_foreign_document_ids_cannot_enter_an_owned_generation(populated):
    rig, first, second = populated
    alice, _, _, app, wire, _, _ = rig
    before = wire.calls
    existing_jobs = len(app.state.store.all('SELECT * FROM jobs'))
    for documents in [[second['document']], [first['document'], second['document']], ['nonexistent-document']]:
        response = alice.post('/api/generations', json=request(first['course'], 'foreign-document-request') | {'document_ids': documents})
        assert response.status_code == 404, response.text
    assert wire.calls == before
    assert len(app.state.store.all('SELECT * FROM jobs')) == existing_jobs
    own_other_course = alice.post('/api/courses', json={'name': 'Alice second course'}).json()['id']
    response = alice.post('/api/generations', json=request(own_other_course, 'own-wrong-course') | {'document_ids': [first['document']]})
    assert response.status_code == 400, response.text


def test_idempotency_keys_reuse_only_within_the_same_account(populated):
    rig, first, second = populated
    alice, bob, _, _, wire, _, _ = rig
    assert first['generation'] != second['generation']
    assert first['media_job'] != second['media_job']
    before = wire.calls
    for client, own in [(alice, first), (bob, second)]:
        generation = client.post('/api/generations', json=request(own['course'], 'same-generation-request'))
        assert generation.status_code == 202, generation.text
        assert generation.json()['reused'] is True and generation.json()['job_id'] == own['generation']
        media = client.post('/api/contents/' + own['content'] + '/media',
            json={'version': 1, 'kind': 'audio', 'request_key': 'same-media-request'})
        assert media.status_code == 202 and media.json()['reused'] is True
        assert media.json()['job_id'] == own['media_job']
        conflict = client.post('/api/generations', json=request(own['course'], 'same-generation-request') | {'count': 2})
        assert conflict.status_code == 409
    assert wire.calls == before


def test_duplicate_upload_is_account_local(account_rig):
    alice, bob, _, _, _, _, _ = account_rig
    uploaded = []
    for client in [alice, bob]:
        cid = client.post('/api/courses', json={'name': 'Same course name'}).json()['id']
        def upload():
            return client.post('/api/documents', data={'course_id': cid}, files={'file': ('same.md', b'Same public notes')})
        original, repeated = upload(), upload()
        assert original.status_code == repeated.status_code == 201
        assert repeated.json()['duplicate'] is True and repeated.json()['id'] == original.json()['id']
        uploaded.append(original.json()['id'])
    assert uploaded[0] != uploaded[1]


def test_legacy_unowned_data_is_preserved_but_not_claimed_by_registration(tmp_path):
    app, wire = isolated_app(tmp_path)
    store = app.state.store
    course, document, chunk, job, content = [uid() for _ in range(5)]
    timestamp = now()
    store.execute('INSERT INTO courses VALUES(?,?,?)', (course, 'Legacy private course', timestamp))
    store.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)', (document, course, 'legacy.md', 'legacy-sha', 'ready', 1, timestamp))
    store.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?)', (chunk, document, 1, 'LEGACY PRIVATE SOURCE', None, None))
    store.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)', (job, 'legacy-unowned-request', 'generate', dumps({'course_id': course}), 'succeeded', dumps({'content_id': content}), None, timestamp, timestamp))
    store.execute('INSERT INTO contents VALUES(?,?,?,?,?,?,?,?,?)', (content, course, job, 1, 'approved', '{}', '[]', '{}', timestamp))
    before = {table: store.all('SELECT * FROM ' + table) for table in ['courses', 'documents', 'chunks', 'jobs', 'contents']}
    with TestClient(app) as first:
        register(first, 'first@example.test', 'First registrant')
        for path in ['/api/courses', '/api/jobs', '/api/contents']:
            assert first.get(path).json() == []
        for path in ['/api/documents/' + document + '/chunks', '/api/jobs/' + job,
            '/api/jobs/' + job + '/evidence', '/api/contents/' + content, '/api/learn/' + content]:
            assert first.get(path).status_code == 404
        assert first.get('/api/status').json()['calls_today'] == 0
    assert before == {table: store.all('SELECT * FROM ' + table) for table in before}
    assert wire.calls == 0


def test_job_account_filter_is_applied_before_recent_limit(account_rig):
    alice, bob, _, app, _, first, second = account_rig
    first_course = alice.post('/api/courses', json={'name': 'Alice quiet course'}).json()['id']
    second_course = bob.post('/api/courses', json={'name': 'Bob busy course'}).json()['id']
    expected, _ = app.state.store.job('alice-one-job', 'generate', request(first_course, 'alice-one-job'), owner_id=first['user']['id'])
    for i in range(35):
        key = 'bob-busy-' + str(i)
        historical, _ = app.state.store.job(key, 'generate', request(second_course, key), owner_id=second['user']['id'])
        app.state.store.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (historical['id'],))
    assert [row['id'] for row in alice.get('/api/jobs').json()] == [expected['id']]
    assert len(bob.get('/api/jobs').json()) == 30


def test_worker_rechecks_owner_before_using_persisted_payload(populated):
    rig, first, second = populated
    alice, bob, _, app, wire, alice_session, _ = rig
    before = wire.calls
    cases = [
        ('index', {'document_id': second['document']}),
        ('generate', request(second['course'], 'mismatched-persisted-course')),
        ('generate', request(first['course'], 'mismatched-persisted-document') | {'document_ids': [second['document']]}),
        ('media', {'content_id': second['content'], 'version': 1, 'kind': 'audio', 'request_key': 'mismatched-media'}),
    ]
    for i, (kind, payload) in enumerate(cases):
        job, _ = app.state.store.job('worker-mismatch-' + str(i), kind, payload, owner_id=alice_session['user']['id'])
        alice.portal.call(app.state.pipeline.run, job['id'])
        saved = app.state.store.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
        assert saved['status'] == 'failed', saved
        assert not saved['result']
        assert bob.get('/api/jobs/' + job['id']).status_code == 404
    unowned, _ = app.state.store.job('worker-unowned-account-mode', 'generate', request(first['course'], 'worker-unowned-account-mode'))
    alice.portal.call(app.state.pipeline.run, unowned['id'])
    assert app.state.store.one('SELECT status FROM jobs WHERE id=?', (unowned['id'],))['status'] == 'failed'
    assert alice.get('/api/jobs/' + unowned['id']).status_code == 404
    assert wire.calls == before


def legacy_pair(store, label):
    course, job = uid(), uid()
    timestamp = now()
    store.execute('INSERT INTO courses VALUES(?,?,?)', (course, label, timestamp))
    store.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
        (job, 'legacy-' + job, 'generate', dumps({'course_id': course}),
         'succeeded', None, None, timestamp, timestamp))
    return course, job


def test_legacy_claim_is_email_bound_single_use_and_snapshot_limited(account_rig):
    from app.account_migration import prepare_legacy_claim
    alice, bob, _, app, _, first, second = account_rig
    store = app.state.store
    legacy_course, legacy_job = legacy_pair(store, 'Legacy claim target')
    bob_course = bob.post('/api/courses', json={'name': 'Already owned by Bob'}).json()['id']
    claim = prepare_legacy_claim(store, first['user']['email'])
    assert set(claim['courses']) == {legacy_course}
    assert set(claim['jobs']) == {legacy_job}
    later_course, later_job = legacy_pair(store, 'Created after the claim snapshot')
    assert bob.post('/api/auth/claim-legacy', json={'token': claim['token']}).status_code == 403
    invalid = alice.post('/api/auth/claim-legacy', json={'token': 'invalid-token-with-enough-characters'})
    assert invalid.status_code == 400
    token = alice.headers.pop('X-CSRF-Token')
    assert alice.post('/api/auth/claim-legacy', json={'token': claim['token']}).status_code == 403
    alice.headers['X-CSRF-Token'] = token
    assert alice.get('/api/courses').json() == []
    claimed = alice.post('/api/auth/claim-legacy', json={'token': claim['token']})
    assert claimed.status_code == 200, claimed.text
    assert {row['id'] for row in alice.get('/api/courses').json()} == {legacy_course}
    assert {row['id'] for row in alice.get('/api/jobs').json()} == {legacy_job}
    assert {row['id'] for row in bob.get('/api/courses').json()} == {bob_course}
    assert bob.get('/api/jobs/' + legacy_job).status_code == 404
    assert store.one('SELECT * FROM course_owners WHERE course_id=?', (later_course,)) is None
    assert store.one('SELECT * FROM job_owners WHERE job_id=?', (later_job,)) is None
    before_owners = {table: store.all('SELECT * FROM ' + table) for table in ['course_owners', 'job_owners']}
    replay = alice.post('/api/auth/claim-legacy', json={'token': claim['token']})
    assert replay.status_code in (400, 409), replay.text
    assert before_owners == {table: store.all('SELECT * FROM ' + table) for table in before_owners}
    for table in store.all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        assert claim['token'] not in dumps(store.all('SELECT * FROM "' + table['name'] + '"'))


def test_legacy_claim_rolls_back_if_snapshot_ownership_has_changed(account_rig):
    from app.account_migration import prepare_legacy_claim
    alice, bob, _, app, _, first, second = account_rig
    store = app.state.store
    course, job = legacy_pair(store, 'Contested legacy data')
    claim = prepare_legacy_claim(store, first['user']['email'])
    store.execute('INSERT INTO course_owners VALUES(?,?)', (course, second['user']['id']))
    result = alice.post('/api/auth/claim-legacy', json={'token': claim['token']})
    assert result.status_code == 409, result.text
    assert store.one('SELECT user_id FROM course_owners WHERE course_id=?', (course,))['user_id'] == second['user']['id']
    assert store.one('SELECT * FROM job_owners WHERE job_id=?', (job,)) is None
    assert alice.get('/api/courses').json() == []
    assert {row['id'] for row in bob.get('/api/courses').json()} == {course}


def test_expired_and_superseded_legacy_tokens_cannot_claim(account_rig, monkeypatch):
    from app import account_migration
    alice, _, _, app, _, session, _ = account_rig
    course, job = legacy_pair(app.state.store, 'Expiry fixture')
    expired = account_migration.prepare_legacy_claim(app.state.store, session['user']['email'], ttl_seconds=60)
    with monkeypatch.context() as patch:
        patch.setattr(account_migration.time, 'time', lambda: expired['expires_at'])
        result = alice.post('/api/auth/claim-legacy', json={'token': expired['token']})
        assert result.status_code == 400
    old = account_migration.prepare_legacy_claim(app.state.store, session['user']['email'])
    fresh = account_migration.prepare_legacy_claim(app.state.store, session['user']['email'])
    assert old['token'] != fresh['token']
    assert alice.post('/api/auth/claim-legacy', json={'token': old['token']}).status_code == 400
    assert app.state.store.one('SELECT * FROM course_owners WHERE course_id=?', (course,)) is None
    assert alice.post('/api/auth/claim-legacy', json={'token': fresh['token']}).status_code == 200


def test_concurrent_legacy_claim_consumes_token_once(account_rig):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.account_migration import prepare_legacy_claim, claim_legacy_workspace
    from app.auth import AuthError
    alice, _, _, app, _, session, _ = account_rig
    course, job = legacy_pair(app.state.store, 'Concurrent claim fixture')
    claim = prepare_legacy_claim(app.state.store, session['user']['email'])
    ready = Barrier(2)
    def attempt():
        ready.wait()
        try:
            claim_legacy_workspace(app.state.store, session['user'], claim['token'])
            return 200
        except AuthError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [200, 409]
    assert app.state.store.one('SELECT count(*) AS n FROM course_owners WHERE course_id=?', (course,))['n'] == 1
    assert app.state.store.one('SELECT count(*) AS n FROM job_owners WHERE job_id=?', (job,))['n'] == 1


def test_boot_preserves_legacy_pending_and_running_jobs(tmp_path):
    app, wire = isolated_app(tmp_path)
    for status in ['queued', 'running']:
        _, job = legacy_pair(app.state.store, 'Unassigned ' + status)
        app.state.store.execute('UPDATE jobs SET status=? WHERE id=?', (status, job))
    before = app.state.store.all('SELECT * FROM jobs ORDER BY id')
    with TestClient(app) as first:
        register(first, 'first@example.test', 'First registrant')
        assert first.get('/api/jobs').json() == []
        assert app.state.store.all('SELECT * FROM jobs ORDER BY id') == before
    assert app.state.store.all('SELECT * FROM jobs ORDER BY id') == before
    assert wire.calls == 0
