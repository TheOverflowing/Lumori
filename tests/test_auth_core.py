import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3

import pytest
from starlette.responses import Response

from app.auth import AuthError, AuthService, hash_password, normalize_email, verify_password
from app.config import Settings
from app.store import Conflict, Store, now


PASSWORD = 'My isolated account password 42!'


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def auth(store, tmp_path):
    return AuthService(store, Settings(data_dir=tmp_path))


def add_user(store, identity):
    store.execute('INSERT INTO users VALUES(?,?,?,?,?)',
                  (identity, identity+'@example.com', 'test-only-no-password-login', identity, now()))


def test_scrypt_salt_unicode_and_invalid_records():
    password = '一个允许中文与空格的足够长的密码 phrase'
    first, second = hash_password(password), hash_password(password)
    assert first != second
    assert first.startswith('scrypt$131072$8$1$')
    assert password not in first
    assert verify_password(password, first)
    assert not verify_password(password+'?', first)
    for invalid in ('', 'sha256$1$8$1$ab$cd', first.replace('$131072$', '$1073741824$')):
        assert not verify_password(password, invalid)
    assert not verify_password('x'*129, first)
    assert not verify_password('\ud800', first)


def test_registration_session_contains_no_stored_bearer_or_password(auth, store):
    session = auth.register('  Alice@Example.COM  ', PASSWORD, ' Alice ')
    assert session.user['email'] == 'alice@example.com'
    assert session.user['display_name'] == 'Alice'
    assert set(session.response()) == {'user', 'csrf_token', 'expires_at'}
    assert 'password_hash' not in session.user
    assert session.token not in repr(session)
    user = store.one('SELECT * FROM users')
    saved = store.one('SELECT * FROM auth_sessions')
    assert user['password_hash'] != PASSWORD
    assert saved['token_hash'] == hashlib.sha256(session.token.encode()).hexdigest()
    assert session.token not in str(saved)
    assert auth.authenticate(session.token).response() == session.response()
    assert auth.authenticate('a'*42) is None
    assert auth.authenticate('é'*43) is None
    assert auth.authenticate('x'*43) is None


def test_normalized_duplicate_and_wrong_credentials(auth, store):
    session = auth.register('alice@example.com', PASSWORD, 'Alice')
    with pytest.raises(AuthError) as duplicate:
        auth.register(' ALICE@example.com ', PASSWORD, 'Someone else')
    assert duplicate.value.status_code == 409
    assert len(store.all('SELECT * FROM users')) == 1
    login = auth.login('ALICE@example.com', PASSWORD)
    assert login.user == session.user and login.token != session.token
    messages = []
    for email in ('alice@example.com', 'unknown@example.com'):
        with pytest.raises(AuthError) as error:
            auth.login(email, 'Wrong account password phrase')
        messages.append((error.value.status_code, error.value.code, error.value.message))
    assert messages[0] == messages[1] == (401, 'invalid_credentials', '邮箱或密码不正确。')


def test_session_expiration_revocation_and_session_bound_csrf(auth, store, monkeypatch):
    session = auth.register('alice@example.com', PASSWORD, 'Alice')
    another = auth.login('alice@example.com', PASSWORD)
    assert auth.check_csrf(session, session.csrf_token)
    assert not auth.check_csrf(session, another.csrf_token)
    for token in (None, '', 'é'*64, session.csrf_token+'0'):
        assert not auth.check_csrf(session, token)
    auth.logout(session.token)
    assert auth.authenticate(session.token) is None
    assert auth.authenticate(another.token)
    monkeypatch.setattr('app.auth.time.time', lambda: another.expires_at)
    assert auth.authenticate(another.token) is None


def test_cookie_flags_and_secure_setting(auth):
    session = auth.register('alice@example.com', PASSWORD, 'Alice')
    response = Response()
    auth.set_cookie(response, session)
    value = response.headers['set-cookie']
    assert 'HttpOnly' in value and 'SameSite=lax' in value and 'Path=/' in value
    assert 'Secure' not in value and 'Max-Age=' in value
    secure = AuthService(auth.store, replace(auth.settings, auth_cookie_secure=True))
    response = Response()
    secure.set_cookie(response, session)
    assert 'Secure' in response.headers['set-cookie']
    response = Response()
    secure.clear_cookie(response)
    assert 'Max-Age=0' in response.headers['set-cookie']
    assert 'HttpOnly' in response.headers['set-cookie'] and 'Secure' in response.headers['set-cookie']


@pytest.mark.parametrize('email', ['none', 'a@', 'a@example', 'a@-example.com', 'a@example..com', 'a'*65+'@example.com', '.a@example.com', 'a..b@example.com'])
def test_email_validation(email):
    with pytest.raises(AuthError) as error:
        normalize_email(email)
    assert error.value.code == 'invalid_email'


@pytest.mark.parametrize('password', ['short', 'x'*129, '\ud800'*15])
def test_invalid_password_rejected_before_hash(auth, password):
    with pytest.raises(AuthError) as error:
        auth.register('alice@example.com', password, 'Alice')
    assert error.value.code == 'invalid_password'
    assert not auth.store.all('SELECT * FROM users')


def test_throttle_persists_across_services_and_resets_at_boundary(auth, monkeypatch):
    settings = replace(auth.settings, auth_login_limit=2, auth_throttle_window_seconds=60)
    first = AuthService(auth.store, settings)
    monkeypatch.setattr('app.auth.time.time', lambda: 1000)
    first._consume_attempt('login', 'a@example.com', 'client-one')
    second = AuthService(auth.store, settings)
    second._consume_attempt('login', 'a@example.com', 'client-two')
    with pytest.raises(AuthError) as error:
        second._consume_attempt('login', 'a@example.com', 'client-three')
    assert error.value.status_code == 429 and error.value.retry_after == 60
    monkeypatch.setattr('app.auth.time.time', lambda: 1060)
    second._consume_attempt('login', 'a@example.com', 'client-three')
    assert all(row['window_start'] == 1060 for row in auth.store.all('SELECT * FROM auth_throttles'))


def test_concurrent_registration_throttle_reserves_atomically(auth):
    auth = AuthService(auth.store, replace(auth.settings, auth_register_limit=3))
    def attempt(_):
        try:
            auth._consume_attempt('register', '', 'same-client')
            return True
        except AuthError as error:
            assert error.status_code == 429
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert sum(outcomes) == 3
    assert auth.store.one('SELECT attempts FROM auth_throttles')['attempts'] == 3


def test_legacy_rows_stay_unowned_after_registration_and_reopening(auth, store):
    store.execute('INSERT INTO courses VALUES(?,?,?)', ('legacy-course', 'Old course', now()))
    legacy, _ = store.job('old-key', 'generate', {'course_id': 'legacy-course'})
    auth.register('alice@example.com', PASSWORD, 'Alice')
    reopened = Store(store.path.parent)
    assert reopened.one('SELECT * FROM courses')['id'] == 'legacy-course'
    assert reopened.one('SELECT * FROM jobs')['id'] == legacy['id']
    assert not reopened.all('SELECT * FROM course_owners')
    assert not reopened.all('SELECT * FROM job_owners')
    with reopened.connect() as db:
        assert len(db.execute('PRAGMA table_info(courses)').fetchall()) == 3
        assert len(db.execute('PRAGMA table_info(jobs)').fetchall()) == 9


def test_migration_from_pre_account_database_retains_original_rows(tmp_path):
    path = tmp_path / 'studio.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE courses(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL)')
        db.execute('''CREATE TABLE jobs(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,result TEXT,error TEXT,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
        db.execute('INSERT INTO courses VALUES(?,?,?)', ('course', 'Existing course', 'before-auth'))
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                   ('job', 'request', 'generate', '{"course_id":"course"}', 'completed', '{}', None, 'before-auth', 'before-auth'))
        original_courses = db.execute('SELECT * FROM courses').fetchall()
        original_jobs = db.execute('SELECT * FROM jobs').fetchall()
    migrated = Store(tmp_path)
    Store(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM courses').fetchall() == original_courses
        assert db.execute('SELECT * FROM jobs').fetchall() == original_jobs
    assert not migrated.all('SELECT * FROM course_owners')
    assert not migrated.all('SELECT * FROM job_owners')
    assert not migrated.all('SELECT * FROM users')


def test_account_job_keys_conflicts_and_owner_insert_are_atomic(store):
    add_user(store, 'alice')
    add_user(store, 'bob')
    old, _ = store.job('same-key', 'generate', {'course_id': 'legacy'})
    a, created = store.job('same-key', 'generate', {'course_id': 'alice-course'}, owner_id='alice')
    b, _ = store.job('same-key', 'generate', {'course_id': 'bob-course'}, owner_id='bob')
    assert created and len({old['id'], a['id'], b['id']}) == 3
    assert store.job('same-key', 'generate', {'course_id': 'alice-course'}, owner_id='alice') == (a, False)
    with pytest.raises(Conflict):
        store.job('same-key', 'generate', {'course_id': 'changed'}, owner_id='alice')
    with pytest.raises(sqlite3.IntegrityError):
        store.job('invalid-owner-key', 'generate', {}, owner_id='missing')
    assert len(store.all('SELECT * FROM jobs')) == 3
    # Historical unowned keys imitating a namespaced key must never be returned.
    forged_key = 'account:' + hashlib.sha256('["alice", "forged"]'.encode()).hexdigest()
    store.job(forged_key, 'generate', {})
    with pytest.raises(Conflict):
        store.job('forged', 'generate', {}, owner_id='alice')


def test_daily_call_limit_is_shared_within_account_not_between_accounts(store):
    add_user(store, 'alice')
    add_user(store, 'bob')
    a, _ = store.job('one', 'generate', {}, owner_id='alice')
    a2, _ = store.job('two', 'generate', {}, owner_id='alice')
    b, _ = store.job('one', 'generate', {}, owner_id='bob')
    store.reserve_call(a['id'], 'text', 'test-model', 1)
    with pytest.raises(ValueError):
        store.reserve_call(a2['id'], 'text', 'test-model', 1)
    store.reserve_call(b['id'], 'text', 'test-model', 1)
    assert len(store.all('SELECT * FROM calls')) == 2
    with pytest.raises(ValueError):
        store.reserve_call(None, 'text', 'test-model', 1)
