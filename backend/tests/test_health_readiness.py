from app import core


def test_health_reports_database_readiness_without_credentials(client, monkeypatch):
    monkeypatch.setattr(core.settings, 'app_environment', 'production')
    response = client.get('/api/v1/health')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'database': 'ok', 'environment': 'production'}


def test_database_failure_is_unhealthy_and_does_not_disclose_connection(client, monkeypatch):
    def unavailable():
        raise RuntimeError('sensitive-connection-information')
    monkeypatch.setattr(core, 'DB', unavailable)
    response = client.get('/api/v1/health')
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'DATABASE_UNAVAILABLE'
    assert 'sensitive' not in response.text
