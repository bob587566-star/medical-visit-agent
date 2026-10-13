"""Malformed JSON Unicode must be rejected before hashing or encrypted persistence."""
import json
from fastapi.testclient import TestClient
import pytest

from backend.main import create_app
from backend.model import ModelService


@pytest.mark.parametrize('endpoint,body', [
    ('/api/account/register', {'username': 'fictional_unicode', 'password': 'long-password-' + '\ud800'}),
    ('/api/account/recover', {'username': 'bad\ud800', 'recovery_code': 'a' * 64, 'new_password': 'fictional-password-long'}),
    ('/api/visits', {'title': 'fictional\ud800'}),
])
def test_unpaired_surrogate_is_controlled_422_without_input_echo(tmp_path, endpoint, body):
    app = create_app(tmp_path / 'fixture.sqlite3', ModelService())
    with TestClient(app) as client:
        boot = client.get('/api/bootstrap').json()
        headers = {'X-CSRF-Token': boot['csrf_token'], 'Content-Type': 'application/json'}
        consent = {'notice_version': boot['privacy']['notice_version'], 'local_storage': True, 'external_ai': False}
        assert client.post('/api/privacy/consent', json=consent, headers=headers).status_code == 200
        response = client.post(endpoint, content=json.dumps(body).encode('ascii'), headers=headers)
        assert response.status_code == 422
        assert 'long-password' not in response.text and '\\ud800' not in response.text
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert client.get('/api/privacy/export').json()['visits'] == []
