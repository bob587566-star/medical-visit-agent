"""A browser cookie jar is shared across localhost ports; environments must not be."""
from fastapi.testclient import TestClient
import pytest

from backend.main import create_app
from backend.model import ModelService


def test_database_profiles_choose_distinct_default_cookie_without_opening_database(tmp_path, monkeypatch):
    monkeypatch.delenv('VISITOR_COOKIE_NAME', raising=False)
    monkeypatch.setenv('DATABASE_URL', 'postgresql+psycopg://fictional:fixture@127.0.0.1:1/fictional')
    assert create_app(model_service=ModelService()).state.cookie_name == 'zhihe_visitor_postgres'
    assert create_app(tmp_path / 'fictional.sqlite3', ModelService()).state.cookie_name == 'zhihe_visitor'


def test_two_environment_cookies_preserve_both_visitors_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv('VISITOR_COOKIE_NAME', 'zhihe_visitor')
    local = create_app(tmp_path / 'local.sqlite3', ModelService())
    monkeypatch.setenv('VISITOR_COOKIE_NAME', 'zhihe_visitor_postgres')
    isolated = create_app(tmp_path / 'isolated.sqlite3', ModelService())
    with TestClient(local) as first, TestClient(isolated) as second:
        original = first.get('/api/bootstrap').json()
        local_cookie = first.cookies.get('zhihe_visitor')
        second.cookies.update(first.cookies)
        other = second.get('/api/bootstrap').json()
        assert second.cookies.get('zhihe_visitor') == local_cookie
        assert second.cookies.get('zhihe_visitor_postgres')
        first.cookies.update(second.cookies)
        assert first.get('/api/bootstrap').json()['csrf_token'] == original['csrf_token']
        assert second.get('/api/bootstrap').json()['csrf_token'] == other['csrf_token']
        assert original['csrf_token'] != other['csrf_token']
        for client, bootstrap in ((first, original), (second, other)):
            assert client.post('/api/privacy/consent', json={
                'notice_version': bootstrap['privacy']['notice_version'], 'local_storage': True,
                'external_ai': False, 'retention_days': 7,
            }, headers={'X-CSRF-Token': bootstrap['csrf_token']}).status_code == 200


@pytest.mark.parametrize('name', ['short', 'unsafe;Path=/', '中文会话名称', 'a' * 65])
def test_invalid_cookie_configuration_fails_before_service_start(tmp_path, monkeypatch, name):
    monkeypatch.setenv('VISITOR_COOKIE_NAME', name)
    with pytest.raises(ValueError):
        create_app(tmp_path / 'fixture.sqlite3', ModelService())
