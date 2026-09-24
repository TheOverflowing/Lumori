from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


def test_deployment_hosts_and_health(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, allowed_hosts=['127.0.0.1', 'lumori.example.org']))
    with TestClient(app, base_url='https://lumori.example.org') as client:
        response = client.get('/healthz')
        assert response.status_code == 200
        assert response.json()['status'] == 'ok'
        assert response.json()['ready'] is True
        assert response.json()['database'] is True
        assert response.json()['workers'] is True
        assert client.get('/api/courses').status_code == 401
        assert client.get('/healthz', headers={'host': 'attacker.example'}).status_code == 400
        response = client.post('/api/auth/register', headers={'origin': 'https://attacker.example'}, json={})
        assert response.status_code == 403
        response = client.post('/api/auth/register', headers={'origin': 'https://lumori.example.org'}, json={
            'email': 'deploy@example.org', 'password': 'test deployment password 42', 'display_name': 'Deployment'})
        assert response.status_code == 201


def test_env_hosts(monkeypatch):
    monkeypatch.setenv('ALLOWED_HOSTS', '127.0.0.1, lumori.example.org ')
    assert Settings.from_env().allowed_hosts == ['127.0.0.1', 'lumori.example.org']


def test_only_public_assets_are_revalidated_and_account_data_is_not_cached(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        first = client.get('/static/app.js')
        assert first.status_code == 200
        assert first.headers['cache-control'] == 'public, max-age=0, must-revalidate'
        cached = client.get('/static/app.js', headers={'If-None-Match': first.headers['etag']})
        assert cached.status_code == 304
        assert cached.content == b''
        assert cached.headers['cache-control'] == first.headers['cache-control']
        for path in ('/', '/api/auth/session', '/api/courses', '/static/not-found.js'):
            assert client.get(path).headers['cache-control'] == 'no-store'
