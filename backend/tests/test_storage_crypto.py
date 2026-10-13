"""Independent temporary databases only; no live record/key is opened here."""
import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from backend.data_crypto import DataCrypto, DataIntegrityError, key_id
from backend.schemas import Intake, Message, Risk, Visit
from backend.storage import Store, now, session_digest
from backend.task_storage import TaskStore


def fixture_visit(identifier='fictional-visit'):
    risk = Risk(message='虚构测试提示')
    return Visit(id=identifier, title='虚构敏感标题', created_at=now(), updated_at=now(),
                 intake=Intake(chief_complaint='虚构嗓子疼', medications='虚构用药原文'),
                 messages=[Message(id='fictional-msg', role='user', content='虚构完整对话',
                                   created_at=now(), risk=risk)], risk=risk)


def legacy_database(path, *, mismatched=False):
    visit = fixture_visit()
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY,csrf TEXT NOT NULL,created_at TEXT NOT NULL)')
        conn.execute('CREATE TABLE visits (id TEXT PRIMARY KEY,owner TEXT NOT NULL REFERENCES sessions(id),payload TEXT NOT NULL,updated_at TEXT NOT NULL)')
        conn.execute('INSERT INTO sessions VALUES (?,?,?)', ('owner', 'legacy-csrf', now()))
        conn.execute('INSERT INTO visits VALUES (?,?,?,?)', ('wrong-id' if mismatched else visit.id,
                     'owner', visit.model_dump_json(), visit.updated_at))
    return visit


def test_whole_visit_and_csrf_encrypted_and_reload_keeps_exact_values(tmp_path):
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    cookie, owner, csrf = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    with store.connect() as conn:
        payload = conn.execute('SELECT payload FROM visits').fetchone()[0]
        encrypted_csrf = conn.execute('SELECT csrf FROM sessions').fetchone()[0]
    assert payload.startswith(DataCrypto.PREFIX) and '虚构' not in payload
    assert csrf not in encrypted_csrf
    reopened = Store(path)
    assert reopened.session(cookie) == (owner, csrf)
    assert reopened.get(owner, visit.id) == visit
    assert reopened.list_visits(owner) == [visit]
    assert reopened.get('another-owner', visit.id) is None


@pytest.mark.parametrize('change', ['payload_swap', 'owner_swap', 'id_swap', 'ciphertext', 'plaintext'])
def test_context_swap_or_downgrade_fail_closed(tmp_path, change):
    store = Store(tmp_path / 'fictional.sqlite3')
    _, owner, _ = store.new_session()
    _, other, _ = store.new_session()
    visit, another = fixture_visit(), fixture_visit('second')
    store.put(owner, visit)
    store.put(other if change == 'owner_swap' else owner, another)
    with store.connect() as conn:
        payload = conn.execute('SELECT payload FROM visits WHERE id=?', (visit.id,)).fetchone()[0]
        if change == 'payload_swap':
            payload = conn.execute('SELECT payload FROM visits WHERE id=?', (another.id,)).fetchone()[0]
        elif change == 'owner_swap':
            payload = store.encrypt_visit(other, visit)
        elif change == 'id_swap':
            payload = store.encrypt_visit(owner, another)
        elif change == 'ciphertext':
            payload = payload[:-6] + 'xxxxxx'
        else:
            payload = visit.model_dump_json()
        conn.execute('UPDATE visits SET payload=? WHERE id=?', (payload, visit.id))
    with pytest.raises(DataIntegrityError):
        store.get(owner, visit.id)
    with pytest.raises(DataIntegrityError):
        Store(tmp_path / 'fictional.sqlite3')


def test_session_ciphertext_cannot_be_moved_between_owners(tmp_path):
    store = Store(tmp_path / 'fictional.sqlite3')
    first, owner, _ = store.new_session()
    _, other, _ = store.new_session()
    with store.connect() as conn:
        payload = conn.execute('SELECT csrf FROM sessions WHERE id=?', (other,)).fetchone()[0]
        conn.execute('UPDATE sessions SET csrf=? WHERE id=?', (payload, owner))
    with pytest.raises(DataIntegrityError):
        store.session(first)


def test_unversioned_legacy_migration_keeps_records_and_removes_plaintext_pages(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    visit = legacy_database(path)
    store = Store(path)
    assert store.get('owner', visit.id) == visit
    with store.connect() as conn:
        assert dict(conn.execute('SELECT name,value FROM storage_crypto_meta'))['version'] == '1'
        assert store.open_value('session_csrf', 'owner', 'owner',
            conn.execute('SELECT csrf FROM sessions').fetchone()[0]) == 'legacy-csrf'
    # Retired SQLite pages/WAL must not keep the original plaintext.
    for file in tmp_path.glob('legacy.sqlite3*'):
        assert '虚构敏感标题'.encode() not in file.read_bytes()
        assert b'legacy-csrf' not in file.read_bytes()


def test_invalid_legacy_record_rolls_back_all_payload_updates(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    visit = legacy_database(path, mismatched=True)
    with pytest.raises(DataIntegrityError):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT payload FROM visits').fetchone()[0] == visit.model_dump_json()
        assert conn.execute('SELECT csrf FROM sessions').fetchone()[0] == 'legacy-csrf'
        assert not conn.execute('SELECT 1 FROM storage_crypto_meta').fetchone()


def test_missing_key_never_replaces_existing_records(tmp_path):
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    key_path = store.data_crypto.key_path
    key_path.unlink()
    with pytest.raises(DataIntegrityError):
        Store(path)
    assert not key_path.exists()


def test_missing_key_still_fails_when_attacker_removes_version_marker(tmp_path):
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    cookie, _, _ = store.new_session()
    with store.connect() as conn:
        conn.execute('DELETE FROM storage_crypto_meta')
    store.data_crypto.key_path.unlink()
    with pytest.raises(DataIntegrityError):
        Store(path)


def test_removed_version_marker_never_enables_mixed_plaintext_migration(tmp_path):
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    with store.connect() as conn:
        conn.execute('DELETE FROM storage_crypto_meta')
        conn.execute('UPDATE visits SET payload=? WHERE id=?', (visit.model_dump_json(), visit.id))
    with pytest.raises(DataIntegrityError):
        Store(path)


def test_external_environment_key_and_file_are_supported_without_local_key(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv('MEDICAL_DATA_KEY', key.decode())
    store = Store(tmp_path / 'environment.sqlite3')
    cookie, owner, csrf = store.new_session()
    assert store.data_crypto.key_source == 'environment'
    assert store.data_crypto.key_path is None
    assert Store(tmp_path / 'environment.sqlite3').session(cookie) == (owner, csrf)
    assert not list(tmp_path.glob('*.data.keys.json'))
    with pytest.raises(DataIntegrityError):
        store.rotate_data_key()
    monkeypatch.delenv('MEDICAL_DATA_KEY')
    external = tmp_path / 'external.key'
    external.write_bytes(key)
    monkeypatch.setenv('MEDICAL_DATA_KEY_FILE', str(external))
    stored = Store(tmp_path / 'file.sqlite3')
    assert stored.data_crypto.key_path == external
    assert stored.data_crypto.key_source == 'external_file'


def test_wrong_key_fails_without_mutating_encrypted_payload(tmp_path):
    path = tmp_path / 'fictional.sqlite3'
    key = Fernet.generate_key()
    store = Store(path, data_key=key)
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    with store.connect() as conn:
        before = conn.execute('SELECT payload FROM visits').fetchone()[0]
    with pytest.raises(DataIntegrityError):
        Store(path, data_key=Fernet.generate_key())
    with store.connect() as conn:
        assert conn.execute('SELECT payload FROM visits').fetchone()[0] == before


def test_task_apply_does_not_reintroduce_plaintext_visit(tmp_path):
    store = Store(tmp_path / 'fictional.sqlite3')
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    tasks = TaskStore(store)
    task = {'id': 'fictional-task', 'visit_id': visit.id, 'status': 'completed',
            'created_at': now(), 'updated_at': now()}
    assert tasks.put(owner, task)
    visit.title = '虚构确认后标题'
    assert tasks.apply(owner, visit, task)
    with store.connect() as conn:
        payload = conn.execute('SELECT payload FROM visits').fetchone()[0]
    assert payload.startswith(DataCrypto.PREFIX) and '标题' not in payload
    assert store.get(owner, visit.id) == visit


def test_purpose_binding_for_additional_sensitive_fields(tmp_path):
    store = Store(tmp_path / 'fictional.sqlite3')
    sealed = store.seal_value('account_profile', 'fictional-owner', 'fictional-account', {'name': '虚构用户'})
    assert store.open_value('account_profile', 'fictional-owner', 'fictional-account', sealed) == {'name': '虚构用户'}
    with pytest.raises(DataIntegrityError):
        store.open_value('visit', 'fictional-owner', 'fictional-account', sealed)


def test_offline_rotation_preserves_data_and_additional_encrypted_fields(tmp_path):
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    cookie, owner, csrf = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    profile = store.seal_value('profile', owner, 'profile-id', '虚构资料')
    with store.connect() as conn:
        conn.execute('CREATE TABLE fictional_profiles(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('INSERT INTO fictional_profiles VALUES(?,?)', ('profile-id', profile))
    old = store.data_crypto.active_id
    result = store.rotate_data_key()
    assert result['key_id'] != old and result['encrypted_cells'] == 3
    assert result['old_keys_retained']
    reopened = Store(path)
    assert reopened.session(cookie) == (owner, csrf)
    assert reopened.get(owner, visit.id) == visit
    with reopened.connect() as conn:
        rotated = conn.execute('SELECT payload FROM fictional_profiles').fetchone()[0]
        event_id, event = conn.execute('SELECT id,payload FROM storage_crypto_events').fetchone()
    assert rotated != profile and result['key_id'] in rotated
    assert reopened.open_value('profile', owner, 'profile-id', rotated) == '虚构资料'
    assert reopened.open_value('key_rotation', '_system', str(event_id), event)['encrypted_cells'] == 3


def test_rotation_failed_transaction_does_not_partially_change_records(tmp_path, monkeypatch):
    store = Store(tmp_path / 'fictional.sqlite3')
    cookie, owner, csrf = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    original, count = store.data_crypto.reseal, 0
    def fail_after_one(payload, identifier):
        nonlocal count
        count += 1
        if count == 2:
            raise DataIntegrityError('fictional failure')
        return original(payload, identifier)
    monkeypatch.setattr(store.data_crypto, 'reseal', fail_after_one)
    old = store.data_crypto.active_id
    with pytest.raises(DataIntegrityError):
        store.rotate_data_key()
    reopened = Store(tmp_path / 'fictional.sqlite3')
    assert reopened.data_crypto.active_id == old
    assert reopened.session(cookie) == (owner, csrf)
    assert reopened.get(owner, visit.id) == visit


def test_rotation_recovers_database_commit_before_final_keyring_switch(tmp_path, monkeypatch):
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    cookie, owner, csrf = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    old = store.data_crypto.active_id
    def fail_final_switch(identifier):
        raise OSError('fictional keyring pointer failure')
    monkeypatch.setattr(store.data_crypto, 'commit_rotation', fail_final_switch)
    with pytest.raises(OSError):
        store.rotate_data_key()
    assert json.loads(store.data_crypto.key_path.read_text())['active'] == old
    reopened = Store(path)
    assert reopened.data_crypto.active_id != old
    assert reopened.session(cookie) == (owner, csrf)
    assert reopened.get(owner, visit.id) == visit


def test_second_rotation_keeps_previous_keys_and_task_audit_readable(tmp_path):
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    cookie, owner, csrf = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    tasks = TaskStore(store)
    task = {'id': 'fictional-task', 'visit_id': visit.id, 'status': 'completed',
            'created_at': now(), 'updated_at': now()}
    tasks.put(owner, task)
    original = store.data_crypto.active_id
    first = store.rotate_data_key()['key_id']
    second = store.rotate_data_key()['key_id']
    assert len({original, first, second}) == 3
    reopened = Store(path)
    assert len(reopened.data_crypto.keys) == 3
    assert reopened.session(cookie) == (owner, csrf)
    assert reopened.get(owner, visit.id) == visit
    recovered_tasks = TaskStore(reopened)
    assert recovered_tasks.get(owner, visit.id, task['id']) == task
    assert recovered_tasks.audit(owner)['verified']


def test_plaintext_csrf_downgrade_is_rejected_on_read_and_restart(tmp_path):
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    cookie, owner, csrf = store.new_session()
    with store.connect() as conn:
        conn.execute('UPDATE sessions SET csrf=? WHERE id=?', (csrf, owner))
    with pytest.raises(DataIntegrityError):
        store.session(cookie)
    with pytest.raises(DataIntegrityError):
        Store(path)


def test_offline_cli_requires_stop_acknowledgement_and_preserves_original_on_backup_error(tmp_path):
    from scripts.rotate_data_key import rotate
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    cookie, owner, csrf = store.new_session()
    before = store.data_crypto.active_id
    backup = tmp_path / 'protected-backup.bin'
    with pytest.raises(ValueError):
        rotate(path, backup, b'fictional long backup password')
    assert not backup.exists()
    backup.write_bytes(b'do not overwrite')
    with pytest.raises(ValueError):
        rotate(path, backup, b'fictional long backup password', confirm_service_stopped=True)
    assert backup.read_bytes() == b'do not overwrite'
    assert Store(path).data_crypto.active_id == before


def test_offline_cli_encrypted_backup_restore_and_rotation_keep_both_copies_readable(tmp_path):
    from scripts.backup import restore_backup
    from scripts.rotate_data_key import rotate
    path = tmp_path / 'fictional.sqlite3'
    store = Store(path)
    cookie, owner, csrf = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    before = store.data_crypto.active_id
    backup = tmp_path / 'protected-backup.bin'
    password = b'fictional long backup password'
    result = rotate(path, backup, password, confirm_service_stopped=True)
    assert result['backup_verified']
    assert result['rotation']['key_id'] != before
    assert b'fictional' not in backup.read_bytes() and '虚构'.encode() not in backup.read_bytes()
    restored = tmp_path / 'restored.sqlite3'
    restore_backup(backup, restored, password)
    original_store, restored_store = Store(path), Store(restored)
    assert restored_store.data_crypto.active_id == before
    assert original_store.data_crypto.active_id != before
    for readable in (original_store, restored_store):
        assert readable.session(cookie) == (owner, csrf)
        assert readable.get(owner, visit.id) == visit


def test_external_task_key_file_is_usable_without_local_task_key(tmp_path):
    store = Store(tmp_path / 'fictional.sqlite3')
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    key_path = tmp_path / 'external-task.key'
    key_path.write_bytes(Fernet.generate_key())
    tasks = TaskStore(store, key_path=key_path)
    task = {'id': 'fictional-task', 'visit_id': visit.id, 'status': 'completed',
            'created_at': now(), 'updated_at': now()}
    tasks.put(owner, task)
    assert tasks.key_path == key_path and tasks.key_source == 'external_file'
    assert not (tmp_path / 'fictional.tasks.key').exists()
    assert TaskStore(store, key_path=key_path).get(owner, visit.id, task['id']) == task


def test_visitor_expiry_preserves_account_anchor_and_individually_controlled_retention(tmp_path):
    import asyncio
    from backend.accounts import Accounts, Credentials
    from datetime import datetime, timedelta, timezone
    store = Store(tmp_path / 'fictional.sqlite3')
    account_cookie, account_owner, _ = store.new_session()
    visitor_cookie, visitor_owner, _ = store.new_session()
    registered, anonymous = fixture_visit('registered'), fixture_visit('anonymous')
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    registered.updated_at = old
    store.put(account_owner, registered)
    store.put(visitor_owner, anonymous)
    accounts = Accounts(store)
    asyncio.run(accounts.register(account_owner, Credentials(username='fictional.anchor', password='Fictional-passphrase-anchor-15')))
    with store.connect() as conn:
        conn.execute('UPDATE sessions SET created_at=?', (old,))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    store.expire(cutoff)
    assert store.session(account_cookie) is not None
    assert store.get(account_owner, registered.id) == registered
    assert store.session(visitor_cookie) is None
    assert store.get(visitor_owner, anonymous.id) is None
    # The privacy controller may remove account-owned data without destroying
    # login recovery's stable ownership principal.
    store.clear(account_owner)
    assert store.get(account_owner, registered.id) is None
    assert store.session(account_cookie) is not None


def test_expired_anonymous_owner_removes_encrypted_consent_before_fk_anchor(tmp_path):
    from backend.privacy import Privacy, ConsentRequest, NOTICE_VERSION
    store = Store(tmp_path / 'fictional.sqlite3')
    cookie, owner, _ = store.new_session()
    privacy = Privacy(store)
    privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True))
    with store.connect() as conn:
        conn.execute('UPDATE sessions SET created_at=? WHERE id=?', ('2020-01-01T00:00:00+00:00', owner))
        conn.execute('UPDATE privacy_events SET created_at=? WHERE owner=?', ('2020-01-01T00:00:00+00:00', owner))
    store.expire('2021-01-01T00:00:00+00:00')
    assert store.session(cookie) is None
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM privacy_current').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM privacy_events').fetchone()[0] == 0


def test_anonymous_thirty_day_consent_is_not_overridden_by_legacy_seven_day_ttl(tmp_path):
    from datetime import datetime, timedelta, timezone
    from backend.privacy import Privacy, ConsentRequest, NOTICE_VERSION
    store = Store(tmp_path / 'fictional.sqlite3')
    cookie, owner, _ = store.new_session()
    privacy = Privacy(store)
    privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True, retention_days=30))
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    visit = fixture_visit()
    visit.updated_at = old
    store.put(owner, visit)
    with store.connect() as conn:
        conn.execute('UPDATE sessions SET created_at=? WHERE id=?', (old, owner))
    privacy.expire()
    store.expire((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    assert store.session(cookie)
    assert store.get(owner, visit.id) == visit
    privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True, retention_days=1))
    privacy.expire()
    assert store.get(owner, visit.id) is None
    store.expire((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    assert store.session(cookie), 'A recent consent must not be retired as a legacy seven-day visitor.'


def test_transaction_guard_prevents_late_write_and_apply_from_resurrecting_withdrawn_data(tmp_path):
    from backend.data_crypto import DataConsentError
    from backend.privacy import Privacy, ConsentRequest, NOTICE_VERSION
    store = Store(tmp_path / 'fictional.sqlite3')
    _, owner, _ = store.new_session()
    privacy, tasks = Privacy(store), TaskStore(store)
    privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True))
    store.enforce_privacy = True
    visit = fixture_visit()
    store.put(owner, visit)
    task = {'id': 'fictional-task', 'visit_id': visit.id, 'status': 'completed',
            'created_at': now(), 'updated_at': now()}
    tasks.put(owner, task)
    privacy.withdraw(owner)
    for operation in (lambda: store.put(owner, visit), lambda: tasks.apply(owner, visit, task),
                      lambda: store.bookmark(owner, 'fictional-sensitive-interest', True)):
        with pytest.raises(DataConsentError):
            operation()
    assert store.get(owner, visit.id) is None
    assert store.bookmark(owner, 'fictional-sensitive-interest', False) == []
    store.clear(owner)  # Deletion rights stay available after withdrawal.


def test_missing_consent_blocks_enforced_new_record_before_any_payload_write(tmp_path):
    from backend.data_crypto import DataConsentError
    from backend.privacy import Privacy
    store = Store(tmp_path / 'fictional.sqlite3')
    _, owner, _ = store.new_session()
    Privacy(store)
    store.enforce_privacy = True
    with pytest.raises(DataConsentError):
        store.put(owner, fixture_visit())
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM visits').fetchone()[0] == 0
