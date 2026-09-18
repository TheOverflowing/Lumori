from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


def test_deployment_hosts_and_health(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, allowed_hosts=['127.0.0.1', 'lumori.example.org']))
    with TestClient(app, base_url='https://lumori.example.org') as client:
        response = client.get('/healthz')
        assert response.status_code == 200
        assert response.json() == {'status': 'ok'}
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
