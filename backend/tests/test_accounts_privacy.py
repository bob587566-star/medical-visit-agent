"""Security contracts for recoverable accounts and separate processing consent."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.model import ModelService
from backend.privacy import NOTICE_VERSION

PASSWORD = 'Fictional-account-passphrase-4827'
NEW_PASSWORD = 'Changed-fictional-passphrase-1954'
USERNAME = 'fictional.recoverable'
COOKIE = 'zhihe_visitor'


def bootstrap(client):
    result = client.get('/api/bootstrap')
    assert result.status_code == 200
    return {'X-CSRF-Token': result.json()['csrf_token']}


def grant(client, headers, *, external=False, days=7):
    result = client.post('/api/privacy/consent', headers=headers, json={
        'notice_version': NOTICE_VERSION, 'local_storage': True,
        'external_ai': external, 'retention_days': days,
    })
    assert result.status_code == 200
    return result.json()


def register(client, headers, username=USERNAME):
    result = client.post('/api/account/register', headers=headers,
                         json={'username': username, 'password': PASSWORD})
    assert result.status_code == 201
    return result.json()


def fictional_visit(client, headers):
    result = client.post('/api/visits', json={'title': '虚构安全验收记录'}, headers=headers)
    assert result.status_code == 201
    return result.json(), '/api/visits/' + result.json()['id']


def fresh_client_identity(client):
    client.cookies.clear()
    return bootstrap(client)


def test_default_consent_blocks_health_processing_without_implicit_acceptance(tmp_path):
    app = create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)
    with TestClient(app) as client:
        headers = bootstrap(client)
        privacy = client.get('/api/privacy').json()
        assert privacy['consent_required'] and not privacy['accepted']
        assert not privacy['scopes']['local_storage'] and not privacy['scopes']['external_ai']
        assert client.post('/api/visits', json={}, headers=headers).status_code == 403
        with app.state.store.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM visits').fetchone()[0] == 0
        accepted = grant(client, headers)
        assert accepted['accepted'] and accepted['scopes']['local_storage']
        assert not accepted['scopes']['external_ai']
        fictional_visit(client, headers)


@pytest.mark.parametrize('change,status', [
    ({'notice_version': 'unread-old-version'}, 409),
    ({'local_storage': False}, 422),
    ({'retention_days': 2}, 422),
    ({'retention_days': '7'}, 422),
    ({'external_ai': 'false'}, 422),
    ({'hidden_agreement': True}, 422),
])
def test_consent_requires_explicit_valid_current_notice_and_scopes(tmp_path, change, status):
    with TestClient(create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)) as client:
        headers = bootstrap(client)
        body = {'notice_version': NOTICE_VERSION, 'local_storage': True,
                'external_ai': False, 'retention_days': 7, **change}
        assert client.post('/api/privacy/consent', json=body, headers=headers).status_code == status
        assert not client.get('/api/privacy').json()['accepted']


def test_account_login_restores_records_after_cookie_clear_and_old_guest_is_revoked(tmp_path):
    with TestClient(create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)) as client:
        headers = bootstrap(client)
        guest_cookie = client.cookies.get(COOKIE)
        grant(client, headers)
        visit, base = fictional_visit(client, headers)
        account = register(client, headers)
        assert account['account']['role'] == 'user'
        assert account['account']['identity_verified'] is False
        assert account['recovery_code'] and account['csrf_token'] != headers['X-CSRF-Token']
        assert client.get(base).status_code == 200
        client.cookies.clear()
        client.cookies.set(COOKIE, guest_cookie)
        bootstrap(client)
        assert client.get(base).status_code == 404
        headers = fresh_client_identity(client)
        assert client.get(base).status_code == 404
        login = client.post('/api/account/login', headers=headers,
                            json={'username': USERNAME, 'password': PASSWORD})
        assert login.status_code == 200
        assert client.get(base).json()['id'] == visit['id']
        assert client.get('/api/privacy').json()['accepted']


def test_recovery_code_rotates_password_and_sessions_and_cannot_replay(tmp_path):
    with TestClient(create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)) as client:
        headers = bootstrap(client)
        grant(client, headers)
        _, base = fictional_visit(client, headers)
        registered = register(client, headers)
        old_authenticated_cookie = client.cookies.get(COOKIE)
        headers = fresh_client_identity(client)
        recovered = client.post('/api/account/recover', headers=headers, json={
            'username': USERNAME, 'recovery_code': registered['recovery_code'], 'new_password': NEW_PASSWORD,
        })
        assert recovered.status_code == 200
        assert recovered.json()['recovery_code'] != registered['recovery_code']
        assert client.get(base).status_code == 200
        client.cookies.clear()
        client.cookies.set(COOKIE, old_authenticated_cookie)
        bootstrap(client)
        assert client.get(base).status_code == 404
        headers = fresh_client_identity(client)
        assert client.post('/api/account/login', headers=headers,
                           json={'username': USERNAME, 'password': PASSWORD}).status_code == 401
        assert client.post('/api/account/recover', headers=headers, json={
            'username': USERNAME, 'recovery_code': registered['recovery_code'], 'new_password': PASSWORD,
        }).status_code == 401
        login = client.post('/api/account/login', headers=headers,
                            json={'username': USERNAME, 'password': NEW_PASSWORD})
        assert login.status_code == 200
        assert client.get(base).status_code == 200


def test_cross_account_data_and_privacy_export_are_isolated(tmp_path):
    with TestClient(create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)) as client:
        headers = bootstrap(client)
        grant(client, headers)
        first, base = fictional_visit(client, headers)
        first_account = register(client, headers, 'fictional.alice')
        headers = fresh_client_identity(client)
        grant(client, headers)
        second_account = register(client, headers, 'fictional.bob')
        bob_headers = {'X-CSRF-Token': second_account['csrf_token']}
        assert first_account['account']['id'] != second_account['account']['id']
        assert client.get(base).status_code == 404
        assert client.get(base + '/tasks').status_code == 404
        assert client.post(base + '/summary', json={}, headers=bob_headers).status_code == 404
        assert client.delete(base, headers=bob_headers).status_code == 404
        exported = client.get('/api/privacy/export').json()
        assert exported['account']['username'] == 'fictional.bob'
        assert first['id'] not in json.dumps(exported)
        assert exported['visits'] == []


@pytest.mark.parametrize('route,method,body', [
    ('/api/privacy/consent', 'POST', {'notice_version': NOTICE_VERSION, 'local_storage': True}),
    ('/api/privacy/consent', 'DELETE', None),
    ('/api/account/register', 'POST', {'username': USERNAME, 'password': PASSWORD}),
    ('/api/account/login', 'POST', {'username': USERNAME, 'password': PASSWORD}),
    ('/api/account/recover', 'POST', {'username': USERNAME, 'recovery_code': 'a' * 64, 'new_password': PASSWORD}),
    ('/api/account/logout', 'POST', {}),
    ('/api/account', 'DELETE', {'password': PASSWORD}),
])
def test_all_identity_and_consent_mutations_require_csrf_and_safe_origin(tmp_path, route, method, body):
    with TestClient(create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)) as client:
        headers = bootstrap(client)
        assert client.request(method, route, json=body).status_code == 403
        assert client.request(method, route, json=body,
                              headers={**headers, 'Origin': 'https://untrusted.invalid'}).status_code == 403


def test_role_self_assignment_rejected_and_unknown_login_does_not_echo_identity(tmp_path):
    with TestClient(create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)) as client:
        headers = bootstrap(client)
        for field in ('role', 'doctor', 'identity_verified'):
            body = {'username': USERNAME, 'password': PASSWORD, field: 'admin'}
            response = client.post('/api/account/register', json=body, headers=headers)
            assert response.status_code == 422
            assert PASSWORD not in response.text
        result = client.post('/api/account/login', headers=headers,
                             json={'username': 'fictional.nonexistent', 'password': PASSWORD})
        assert result.status_code == 401
        assert 'fictional.nonexistent' not in result.text and PASSWORD not in result.text


def test_export_contains_owned_data_and_history_but_no_password_cookie_key_or_recovery(tmp_path):
    app = create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)
    with TestClient(app) as client:
        headers = bootstrap(client)
        grant(client, headers, days=30)
        _, base = fictional_visit(client, headers)
        account = register(client, headers)
        response = client.get('/api/privacy/export')
        assert response.status_code == 200 and 'attachment;' in response.headers['content-disposition']
        result = response.json()
        assert result['visits'][0]['title'] == '虚构安全验收记录'
        assert result['consent_history'][0]['action'] == 'consent_granted'
        assert result['privacy']['retention_days'] == 30
        for forbidden in (PASSWORD, account['recovery_code'], account['csrf_token'], client.cookies.get(COOKIE),
                          'password_hash', 'recovery_digest', '.data.keys', 'auth_sessions'):
            assert forbidden not in response.text


def test_external_ai_is_separate_consent_and_withdrawal_deletes_health_data(tmp_path):
    app = create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)
    with TestClient(app) as client:
        headers = bootstrap(client)
        grant(client, headers, external=False)
        _, base = fictional_visit(client, headers)
        rejected = client.post(base + '/messages', json={'content': '虚构咳嗽', 'use_model': True}, headers=headers)
        assert rejected.status_code == 403
        assert client.post(base + '/tasks', json={'goal': 'prepare_visit', 'use_model': True}, headers=headers).status_code == 403
        assert client.post(base + '/summary', json={'use_model': True}, headers=headers).status_code == 403
        assert client.post(base + '/messages', json={'content': '虚构咳嗽', 'use_model': False}, headers=headers).status_code == 200
        grant(client, headers, external=True)
        assert client.get('/api/privacy').json()['scopes']['external_ai']
        withdrawn = client.delete('/api/privacy/consent', headers=headers)
        assert withdrawn.status_code == 200 and not withdrawn.json()['accepted']
        assert client.get(base).status_code == 404
        assert client.get('/api/tasks/audit').json()['event_count'] == 0
        assert client.post('/api/visits', json={}, headers=headers).status_code == 403
        exported = client.get('/api/privacy/export').json()
        assert exported['visits'] == [] and exported['tasks'] == []
        assert any(item['action'] == 'consent_withdrawn_and_data_deleted' for item in exported['consent_history'])


def test_account_deletion_requires_current_password_and_removes_owned_records(tmp_path):
    app = create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)
    with TestClient(app) as client:
        headers = bootstrap(client)
        grant(client, headers)
        _, base = fictional_visit(client, headers)
        account = register(client, headers)
        headers = {'X-CSRF-Token': account['csrf_token']}
        assert client.request('DELETE', '/api/account', json={'password': 'wrong'}, headers=headers).status_code == 401
        assert client.get(base).status_code == 200
        assert client.request('DELETE', '/api/account', json={'password': PASSWORD}, headers=headers).status_code == 204
        headers = bootstrap(client)
        assert client.get(base).status_code == 404
        assert client.post('/api/account/login', json={'username': USERNAME, 'password': PASSWORD}, headers=headers).status_code == 401
        with app.state.store.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 0
            assert conn.execute('SELECT COUNT(*) FROM visits').fetchone()[0] == 0
            assert conn.execute('SELECT COUNT(*) FROM privacy_events').fetchone()[0] == 0


def test_account_profile_consent_and_auth_csrf_not_stored_as_plaintext(tmp_path):
    app = create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)
    with TestClient(app) as client:
        headers = bootstrap(client)
        grant(client, headers)
        account = register(client, headers)
        with app.state.store.connect() as conn:
            for table, column in (('accounts', 'profile'), ('privacy_current', 'payload'), ('privacy_events', 'payload'), ('auth_sessions', 'csrf')):
                payload = conn.execute(f'SELECT {column} FROM {table}').fetchone()[0]
                assert payload.startswith('zhihe:v1:')
                assert USERNAME not in payload and account['csrf_token'] not in payload
                assert 'local_storage' not in payload
            password = conn.execute('SELECT password_hash FROM accounts').fetchone()[0]
            assert password.startswith('scrypt-v1:') and PASSWORD not in password
