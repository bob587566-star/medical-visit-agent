"""Real PostgreSQL tests use an explicitly configured fictional test database.

Each test gets a unique schema and key directory. No production schema is
read, migrated, truncated, or restored by this suite.
"""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from backend.data_crypto import DataCrypto, DataIntegrityError
from backend.postgres_storage import PostgreSQLStore, DataConflictError, bind_qmarks
from backend.storage import now
from backend.task_storage import TaskStore
from backend.tests.test_storage_crypto import fixture_visit


@pytest.mark.parametrize('sql,values,expected', [
    ("SELECT ?,'literal?','it''s ?'", ['value?'], "SELECT :p0,'literal?','it''s ?'"),
    ('SELECT "identifier?",?', ['x'], 'SELECT "identifier?",:p0'),
    ("SELECT ? WHERE ?='?';", ["x');DROP TABLE visits;--", 7], "SELECT :p0 WHERE :p1='?';"),
])
def test_qmark_translation_never_interpolates_values(sql, values, expected):
    converted, binds = bind_qmarks(sql, values)
    assert converted == expected
    assert list(binds.values()) == values
    assert not any(str(value) in converted for value in values if len(str(value)) > 10)


@pytest.mark.parametrize('sql,params', [
    ('SELECT ?', []), ("SELECT 'unclosed", []),
    ('SELECT 1; DROP TABLE visits', []), ('SELECT -- comment?\n ?', [1]),
    ('SELECT $payload$?$payload$', []),
])
def test_unreviewed_or_malformed_sql_syntax_rejected(sql, params):
    with pytest.raises(ValueError):
        bind_qmarks(sql, params)


@pytest.fixture
def pg_store(tmp_path):
    url = os.environ.get('MEDICAL_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set MEDICAL_TEST_DATABASE_URL to the dedicated fictional PostgreSQL database.')
    schema = 'fixture_' + uuid4().hex
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as conn:
        conn.execute(text('CREATE SCHEMA ' + schema))  # UUID-only trusted identifier.
    store = PostgreSQLStore(url, data_dir=tmp_path, schema=schema)
    try:
        class PrivateURL(str):
            def __repr__(self):
                return '<private PostgreSQL test URL>'
        yield store, PrivateURL(url), schema, tmp_path
    finally:
        store.close()
        with engine.begin() as conn:
            conn.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
        engine.dispose()


def test_real_pg_schema_and_timeout_are_transaction_local(pg_store):
    store, _, schema, _ = pg_store
    with store.engine.begin() as raw:
        before = (raw.execute(text('SHOW search_path')).scalar_one(),
                  raw.execute(text('SHOW statement_timeout')).scalar_one())
        assert schema not in before[0]
    with store.connect() as conn:
        assert conn.execute('SELECT current_schema()').fetchone()[0] == schema
        assert conn.execute('SHOW statement_timeout').fetchone()[0] == '10s'
    with store.engine.begin() as raw:
        after = (raw.execute(text('SHOW search_path')).scalar_one(),
                 raw.execute(text('SHOW statement_timeout')).scalar_one())
        assert after == before


def test_real_pooled_pg_encrypted_crud_csrf_and_bookmarks(pg_store):
    store, url, schema, directory = pg_store
    cookie, owner, csrf = store.new_session()
    _, other, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    assert store.session(cookie) == (owner, csrf)
    assert store.session('missing') is None
    assert store.get(other, visit.id) is None
    assert store.bookmark(owner, 'fictional-source', True) == ['fictional-source']
    assert store.bookmarks(other) == []
    assert store.list_visits(owner)[0] == visit
    with store.connect() as conn:
        ciphertext = conn.execute('SELECT payload FROM visits WHERE id=?', (visit.id,)).fetchone()[0]
        saved_csrf = conn.execute('SELECT csrf FROM sessions WHERE id=?', (owner,)).fetchone()[0]
        assert ciphertext.startswith(DataCrypto.PREFIX) and '虚构' not in ciphertext
        assert csrf not in saved_csrf
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='visits'").fetchone()
        assert 'payload' in [row[1] for row in conn.execute('PRAGMA table_info(visits)')]
    reopened = PostgreSQLStore(url, data_dir=directory, schema=schema)
    try:
        assert reopened.get(owner, visit.id) == visit
        assert reopened.session(cookie) == (owner, csrf)
        assert reopened.delete(other, visit.id) is False
        assert reopened.delete(owner, visit.id) is True
    finally:
        reopened.close()


def test_real_cross_instance_compare_and_swap_prevents_silent_overwrite(pg_store):
    store, url, schema, directory = pg_store
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    second = PostgreSQLStore(url, data_dir=directory, schema=schema)
    try:
        first_copy, stale_copy = store.get(owner, visit.id), second.get(owner, visit.id)
        first_copy.title = '虚构先提交的变更'
        store.put(owner, first_copy)
        stale_copy.title = '虚构迟到的变更'
        with pytest.raises(DataConflictError):
            second.put(owner, stale_copy)
        assert second.get(owner, visit.id).title == first_copy.title
        # Model copies retain the private compare-and-swap revision.
        fresh = second.get(owner, visit.id).model_copy(deep=True)
        fresh.title = '虚构重新载入后提交'
        second.put(owner, fresh)
        assert store.get(owner, visit.id).title == fresh.title
    finally:
        second.close()


def test_real_pg_task_apply_is_atomic_encrypted_and_stale_result_cannot_overwrite(pg_store):
    store, *_ = pg_store
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    tasks = TaskStore(store)
    task = {'id': 'fictional-task', 'visit_id': visit.id, 'status': 'completed',
            'created_at': now(), 'updated_at': now()}
    assert tasks.put(owner, task)
    stale = store.get(owner, visit.id)
    fresh = store.get(owner, visit.id)
    fresh.title = '虚构新信息'
    store.put(owner, fresh)
    stale.title = '虚构旧任务结果'
    with pytest.raises(DataConflictError):
        tasks.apply(owner, stale, task)
    assert tasks.audit(owner)['event_count'] == 1
    loaded = store.get(owner, visit.id)
    loaded.title = '虚构已确认草稿'
    assert tasks.apply(owner, loaded, task)
    assert store.get(owner, visit.id).title == loaded.title
    assert tasks.audit(owner)['event_count'] == 2
    with store.connect() as conn:
        assert conn.execute('SELECT payload FROM visits').fetchone()[0].startswith(DataCrypto.PREFIX)


def test_real_pg_fk_transaction_failure_rolls_back(pg_store):
    store, *_ = pg_store
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    with pytest.raises(sqlite3.IntegrityError):
        with store.connect() as conn:
            conn.execute('UPDATE visits SET updated_at=? WHERE id=?', ('changed-inside-failed-transaction', visit.id))
            conn.execute('INSERT INTO bookmarks(owner,source_id) VALUES (?,?)', ('missing-owner', 'fictional-source'))
    with store.connect() as conn:
        assert conn.execute('SELECT updated_at FROM visits').fetchone()[0] == visit.updated_at


def test_real_pg_audit_concurrency_is_serialized_across_instances(pg_store):
    store, url, schema, directory = pg_store
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    other = PostgreSQLStore(url, data_dir=directory, schema=schema)
    first_tasks, other_tasks = TaskStore(store), TaskStore(other)
    try:
        def create(index):
            task = {'id': 'fictional-' + str(index), 'visit_id': visit.id, 'status': 'completed',
                    'created_at': now(), 'updated_at': now()}
            return (first_tasks if index % 2 else other_tasks).put(owner, task)
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert all(pool.map(create, range(32)))
        assert first_tasks.audit(owner)['verified']
        assert other_tasks.audit(owner)['event_count'] == 32
        assert len(first_tasks.list(owner, visit.id)) == 32
    finally:
        other.close()


def test_real_pg_tampering_and_plaintext_downgrade_fail_closed(pg_store):
    store, *_ = pg_store
    _, owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    with store.connect() as conn:
        conn.execute('UPDATE visits SET payload=? WHERE id=?', (visit.model_dump_json(), visit.id))
    with pytest.raises(DataIntegrityError):
        store.get(owner, visit.id)


def test_real_pg_visitor_expiry_protects_account_anchor_and_clear_cascades_tasks(pg_store):
    import asyncio
    from backend.accounts import Accounts, Credentials
    store, *_ = pg_store
    account_cookie, owner, _ = store.new_session()
    _, expired_owner, _ = store.new_session()
    visit = fixture_visit()
    store.put(owner, visit)
    tasks = TaskStore(store)
    task = {'id': 'fictional-task', 'visit_id': visit.id, 'status': 'completed',
            'created_at': now(), 'updated_at': now()}
    tasks.put(owner, task)
    accounts = Accounts(store)
    asyncio.run(accounts.register(owner, Credentials(username='fictional.anchor', password='Fictional-passphrase-anchor-15')))
    with store.connect() as conn:
        conn.execute('UPDATE sessions SET created_at=?', ('2020-01-01T00:00:00+00:00',))
    store.expire('2021-01-01T00:00:00+00:00')
    assert store.session(account_cookie)
    assert store.get(owner, visit.id)
    store.clear(owner)
    assert store.get(owner, visit.id) is None
    assert tasks.list(owner, visit.id) == []
    assert tasks.audit(owner)['event_count'] == 0
    assert store.session(account_cookie)


def test_real_pg_never_routes_sqlite_maintenance_to_live_pg(pg_store):
    store, *_ = pg_store
    with store.connect() as conn:
        for sql in ('PRAGMA writable_schema=ON', 'VACUUM', 'SELECT rowid FROM visits'):
            with pytest.raises(ValueError):
                conn.execute(sql)
    with pytest.raises(ValueError):
        store.rotate_data_key()


def test_real_pg_accounts_recovery_privacy_and_owned_records_round_trip(pg_store):
    import asyncio
    from backend.accounts import Accounts, Credentials, Recovery
    from backend.privacy import Privacy, ConsentRequest, NOTICE_VERSION
    store, *_ = pg_store
    guest_cookie, owner, _ = store.new_session()
    TaskStore(store)
    accounts, privacy = Accounts(store), Privacy(store)
    privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True, retention_days=30))
    visit = fixture_visit()
    store.put(owner, visit)
    password, changed = 'Fictional-pg-passphrase-3815', 'Fictional-pg-new-passphrase-9271'
    async def lifecycle():
        cookie, csrf, recovery = await accounts.register(owner, Credentials(username='fictional.pg.user', password=password))
        assert accounts.resolve(cookie) == (owner, csrf)
        assert accounts.resolve(guest_cookie) is None
        assert accounts.public(owner)['identity_verified'] is False
        login_cookie, login_csrf, login_owner = await accounts.login(Credentials(username='fictional.pg.user', password=password))
        assert login_owner == owner and accounts.resolve(login_cookie) == (owner, login_csrf)
        recovered_cookie, recovered_csrf, recovered_owner, new_recovery = await accounts.recover(
            Recovery(username='fictional.pg.user', recovery_code=recovery, new_password=changed))
        assert recovered_owner == owner and new_recovery != recovery
        assert accounts.resolve(login_cookie) is None and accounts.resolve(cookie) is None
        assert accounts.resolve(recovered_cookie) == (owner, recovered_csrf)
        assert store.get(owner, visit.id) == visit
        assert privacy.status(owner)['retention_days'] == 30
        await accounts.delete(owner, changed)
    asyncio.run(lifecycle())
    assert store.get(owner, visit.id) is None
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM privacy_events').fetchone()[0] == 0


def test_real_pg_anonymous_consent_thirty_days_and_bounded_empty_retirement(pg_store):
    from datetime import datetime, timedelta, timezone
    from backend.privacy import Privacy, ConsentRequest, NOTICE_VERSION
    store, *_ = pg_store
    cookie, owner, _ = store.new_session()
    TaskStore(store)
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
    assert store.session(cookie) and store.get(owner, visit.id)
    privacy.withdraw(owner)
    assert store.get(owner, visit.id) is None
    store.expire((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    assert store.session(cookie), 'Recent withdrawal history has a bounded tail, not immediate removal.'
    with store.connect() as conn:
        conn.execute('UPDATE sessions SET created_at=? WHERE id=?', ('2020-01-01T00:00:00+00:00', owner))
        conn.execute('UPDATE privacy_events SET created_at=? WHERE owner=?', ('2020-01-01T00:00:00+00:00', owner))
    store.expire((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    assert store.session(cookie) is None
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM privacy_events').fetchone()[0] == 0


def test_real_pg_live_db_consent_gate_blocks_other_instance_late_write_after_withdrawal(pg_store):
    from backend.data_crypto import DataConsentError
    from backend.privacy import Privacy, ConsentRequest, NOTICE_VERSION
    store, url, schema, directory = pg_store
    _, owner, _ = store.new_session()
    TaskStore(store)
    privacy = Privacy(store)
    privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True))
    store.enforce_privacy = True
    visit = fixture_visit()
    store.put(owner, visit)
    other = PostgreSQLStore(url, data_dir=directory, schema=schema)
    other.enforce_privacy = True
    try:
        stale = other.get(owner, visit.id)
        privacy.withdraw(owner)
        for record in (stale, fixture_visit('late-new-record')):
            with pytest.raises(DataConsentError):
                other.put(owner, record)
        assert other.list_visits(owner) == []
    finally:
        other.close()
