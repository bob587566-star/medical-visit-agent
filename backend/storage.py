from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .schemas import Visit
from .data_crypto import DataCrypto, DataIntegrityError, DataConsentError
from .migrations import apply_migrations


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def session_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, *, key_path: Path | None = None, data_key: str | bytes | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        self.enforce_privacy = False
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS visits (id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id), payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS visits_owner ON visits(owner)")
            conn.execute("CREATE TABLE IF NOT EXISTS bookmarks (owner TEXT NOT NULL REFERENCES sessions(id), source_id TEXT NOT NULL, PRIMARY KEY(owner,source_id))")
            apply_migrations(conn)
            conn.execute("CREATE TABLE IF NOT EXISTS storage_crypto_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version_row = conn.execute("SELECT value FROM storage_crypto_meta WHERE name='version'").fetchone()
            encrypted_database = bool(version_row and version_row[0] != '0')
            # A removed marker must not allow overwriting the original key.
            encrypted_database = encrypted_database or bool(conn.execute(
                "SELECT 1 FROM visits WHERE payload LIKE ? LIMIT 1", (DataCrypto.PREFIX + '%',)).fetchone())
            encrypted_database = encrypted_database or bool(conn.execute(
                "SELECT 1 FROM sessions WHERE csrf LIKE ? LIMIT 1", (DataCrypto.PREFIX + '%',)).fetchone())
            if encrypted_database and not version_row:
                raise DataIntegrityError('已有密文但加密版本登记缺失，未启动明文迁移。')
            self.data_crypto = DataCrypto(path, key_path=key_path, data_key=data_key,
                                          encrypted_database=encrypted_database)
            if version_row:
                if version_row[0] != '1':
                    raise DataIntegrityError("不支持的数据加密版本。")
                identifier = conn.execute("SELECT value FROM storage_crypto_meta WHERE name='key_id'").fetchone()
                if not identifier:
                    raise DataIntegrityError("数据密钥版本登记缺失。")
                self.data_crypto.select(identifier[0])
                self._verify_encrypted(conn)
            else:
                self._migrate_legacy(conn)
            pending = conn.execute("SELECT value FROM storage_crypto_meta WHERE name='cleanup_pending'").fetchone()
            self._legacy_rows_migrated = bool(pending and pending[0] == '1')
        if self._legacy_rows_migrated:
            self._remove_legacy_pages()
            with self.connect() as conn:
                conn.execute("UPDATE storage_crypto_meta SET value='0' WHERE name='cleanup_pending'")

    def _remove_legacy_pages(self) -> None:
        """Migration also removes legacy plaintext from free pages and the WAL.

        Service migration is performed offline. Backups created before migration
        remain plaintext unless separately encrypted; this cannot change them.
        """
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0]:
                raise DataIntegrityError("旧明文清理需要停止其他数据库连接后再启动。")
            conn.execute("VACUUM")
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0]:
                raise DataIntegrityError("旧明文清理未完成，请保持服务停止并重试。")
        finally:
            conn.close()

    def _verify_encrypted(self, conn) -> None:
        for visit_id, owner, payload in conn.execute("SELECT id,owner,payload FROM visits"):
            self.decode_visit(owner, visit_id, payload)
        for owner, csrf in conn.execute("SELECT id,csrf FROM sessions"):
            self.open_value('session_csrf', owner, owner, csrf)

    def _migrate_legacy(self, conn) -> None:
        """Only an unversioned legacy database may contain plaintext on first open.

        Validation and updates share the constructor transaction. A failed old
        record leaves every original row unchanged instead of partially migrating.
        """
        visits = conn.execute("SELECT id,owner,payload FROM visits").fetchall()
        sessions = conn.execute("SELECT id,csrf FROM sessions").fetchall()
        self._legacy_rows_migrated = bool(visits or sessions)
        encrypted_visits = []
        for visit_id, owner, payload in visits:
            if payload.startswith(DataCrypto.PREFIX):
                visit = self.decode_visit(owner, visit_id, payload)
            else:
                try:
                    visit = Visit.model_validate_json(payload)
                except (ValueError, TypeError) as error:
                    raise DataIntegrityError("旧记录格式无法校验，未修改原数据。") from error
                if visit.id != visit_id:
                    raise DataIntegrityError("旧记录标识不匹配，未修改原数据。")
            encrypted_visits.append((self.encrypt_visit(owner, visit), visit_id, owner))
        encrypted_sessions = []
        for owner, csrf in sessions:
            if csrf.startswith(DataCrypto.PREFIX):
                csrf = self.open_value('session_csrf', owner, owner, csrf)
            if not isinstance(csrf, str) or not csrf or len(csrf) > 200:
                raise DataIntegrityError("旧会话格式无法校验，未修改原数据。")
            encrypted_sessions.append((self.seal_value('session_csrf', owner, owner, csrf), owner))
        conn.executemany("UPDATE visits SET payload=? WHERE id=? AND owner=?", encrypted_visits)
        conn.executemany("UPDATE sessions SET csrf=? WHERE id=?", encrypted_sessions)
        conn.execute("INSERT INTO storage_crypto_meta VALUES('version','1')")
        conn.execute("INSERT INTO storage_crypto_meta VALUES('key_id',?)", (self.data_crypto.active_id,))
        conn.execute("INSERT INTO storage_crypto_meta VALUES('cleanup_pending',?)", ('1' if self._legacy_rows_migrated else '0',))

    def seal_value(self, purpose: str, owner: str, record_id: str, value) -> str:
        return self.data_crypto.seal(purpose, owner, record_id, value)

    def open_value(self, purpose: str, owner: str, record_id: str, payload: str):
        return self.data_crypto.open(purpose, owner, record_id, payload)

    def encrypt_visit(self, owner: str, visit: Visit) -> str:
        return self.seal_value('visit', owner, visit.id, visit.model_dump(mode='json'))

    def check_write_consent(self, conn, owner: str) -> None:
        """Read the current encrypted scope inside the caller's write lock/tx."""
        if not self.enforce_privacy:
            return
        from .privacy import NOTICE_VERSION
        row = conn.execute('SELECT event_id,payload FROM privacy_current WHERE owner=?', (owner,)).fetchone()
        state = self.open_value('privacy_current', owner, row[0], row[1]) if row else {}
        if not isinstance(state, dict):
            raise DataIntegrityError('保存同意格式无法校验。')
        if state.get('notice_version') != NOTICE_VERSION or state.get('local_storage') is not True:
            raise DataConsentError('本机保存同意已变化，未提交健康资料。')

    def decode_visit(self, owner: str, visit_id: str, payload: str) -> Visit:
        try:
            visit = Visit.model_validate(self.open_value('visit', owner, visit_id, payload))
            if visit.id != visit_id:
                raise DataIntegrityError("记录标识校验失败。")
            return visit
        except (ValueError, TypeError) as error:
            raise DataIntegrityError("记录内容无法校验，未使用损坏数据。") from error

    def rotate_data_key(self) -> dict:
        """Offline rotation: keyring first, database transaction second.

        Retained keys make an interrupted final keyring pointer update recoverable:
        the database's committed key_id is authoritative on the next open. Caller
        must stop all application/worker processes and first create a verified,
        encrypted backup. Other independently keyed task payloads are unchanged.
        """
        old_id = self.data_crypto.active_id
        identifier = self.data_crypto.prepare_rotation()
        changed = 0
        try:
            with self.lock, self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._verify_encrypted(conn)
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                for (table,) in tables:
                    quoted_table = '"' + table.replace('"', '""') + '"'
                    columns = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
                    for column in columns:
                        if str(column[2]).upper() != 'TEXT':
                            continue
                        name = column[1]
                        quoted_column = '"' + name.replace('"', '""') + '"'
                        rows = conn.execute(f"SELECT rowid,{quoted_column} FROM {quoted_table} WHERE {quoted_column} LIKE ?", (DataCrypto.PREFIX + '%',)).fetchall()
                        for row_id, payload in rows:
                            encrypted = self.data_crypto.reseal(payload, identifier)
                            conn.execute(f"UPDATE {quoted_table} SET {quoted_column}=? WHERE rowid=?", (encrypted, row_id))
                            changed += 1
                conn.execute("UPDATE storage_crypto_meta SET value=? WHERE name='key_id'", (identifier,))
                conn.execute("CREATE TABLE IF NOT EXISTS storage_crypto_events (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
                seq = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM storage_crypto_events").fetchone()[0]
                self.data_crypto.select(identifier)
                event = self.seal_value('key_rotation', '_system', str(seq), {
                    'at': now(), 'previous_key_id': old_id, 'key_id': identifier,
                    'encrypted_cells': changed, 'mode': 'offline', 'old_keys_retained': True,
                })
                conn.execute("INSERT INTO storage_crypto_events(id,payload) VALUES (?,?)", (seq, event))
        except Exception:
            self.data_crypto.select(old_id)
            raise
        # If this file replace fails, the transaction and both keys are already
        # durable. A reopen selects the committed database key, not this pointer.
        self.data_crypto.commit_rotation(identifier)
        return {'key_id': identifier, 'previous_key_id': old_id,
                'encrypted_cells': changed, 'old_keys_retained': True}

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def new_session(self) -> tuple[str, str, str]:
        cookie = secrets.token_urlsafe(32)
        owner = session_digest(cookie)
        csrf = secrets.token_urlsafe(32)
        with self.lock, self.connect() as conn:
            conn.execute("INSERT INTO sessions(id,csrf,created_at) VALUES (?, ?, ?)", (owner, self.seal_value('session_csrf', owner, owner, csrf), now()))
        return cookie, owner, csrf

    def session(self, cookie: str | None) -> tuple[str, str] | None:
        if not cookie or len(cookie) > 200:
            return None
        owner = session_digest(cookie)
        with self.connect() as conn:
            row = conn.execute("SELECT csrf FROM sessions WHERE id=?", (owner,)).fetchone()
        return (owner, self.open_value('session_csrf', owner, owner, row[0])) if row else None

    def list_visits(self, owner: str) -> list[Visit]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id,payload FROM visits WHERE owner=? ORDER BY updated_at DESC, id DESC", (owner,)).fetchall()
        return [self.decode_visit(owner, row[0], row[1]) for row in rows]

    def get(self, owner: str, visit_id: str) -> Visit | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM visits WHERE id=? AND owner=?", (visit_id, owner)).fetchone()
        return self.decode_visit(owner, visit_id, row[0]) if row else None

    def put(self, owner: str, visit: Visit) -> None:
        with self.lock, self.connect() as conn:
            if self.enforce_privacy:
                conn.execute('BEGIN IMMEDIATE')
                self.check_write_consent(conn, owner)
            conn.execute("INSERT INTO visits(id,owner,payload,updated_at) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at WHERE visits.owner=excluded.owner", (visit.id, owner, self.encrypt_visit(owner, visit), visit.updated_at))

    def delete(self, owner: str, visit_id: str) -> bool:
        with self.lock, self.connect() as conn:
            return bool(conn.execute("DELETE FROM visits WHERE id=? AND owner=?", (visit_id, owner)).rowcount)

    def clear(self, owner: str) -> None:
        with self.lock, self.connect() as conn:
            conn.execute("DELETE FROM visits WHERE owner=?", (owner,))
            conn.execute("DELETE FROM bookmarks WHERE owner=?", (owner,))
            # Optional task tables are created by TaskStore after Store itself.
            for table in ('task_audit', 'task_audit_heads'):
                if conn.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone():
                    conn.execute(f'DELETE FROM {table} WHERE owner=?', (owner,))

    def bookmarks(self, owner: str) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT source_id FROM bookmarks WHERE owner=? ORDER BY source_id", (owner,)).fetchall()
        return [row[0] for row in rows]

    def bookmark(self, owner: str, source_id: str, saved: bool) -> list[str]:
        with self.lock, self.connect() as conn:
            if saved:
                if self.enforce_privacy:
                    conn.execute('BEGIN IMMEDIATE')
                    self.check_write_consent(conn, owner)
                conn.execute("INSERT OR IGNORE INTO bookmarks(owner,source_id) VALUES(?,?)", (owner,source_id))
            else:
                conn.execute("DELETE FROM bookmarks WHERE owner=? AND source_id=?", (owner,source_id))
        return self.bookmarks(owner)

    def expire(self, cutoff: str) -> None:
        """Remove inactive records and expired visitor sessions from the live database."""
        with self.lock, self.connect() as conn:
            has_accounts = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone())
            has_privacy = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='privacy_current'").fetchone())
            # Registered account principals are durable ownership anchors, not
            # expiring anonymous cookies. Their individually consented retention
            # is enforced by PrivacyService instead of this global visitor TTL.
            protected = []
            if has_accounts:
                protected.append('SELECT data_owner FROM accounts WHERE data_owner IS NOT NULL')
            if has_privacy:
                protected.append('SELECT owner FROM privacy_current')
            protected_query = ' UNION '.join(protected)
            owner_guard = f' AND id NOT IN ({protected_query})' if protected_query else ''
            visit_guard = f' AND owner NOT IN ({protected_query})' if protected_query else ''
            expired_owners = 'SELECT id FROM sessions WHERE created_at < ?' + owner_guard
            expiry_parameters = [cutoff]
            if has_privacy:
                # Empty anonymous consent anchors have a bounded 30-day tail.
                # A recent agreement/withdrawal or any still-retained health
                # record prevents retirement; account anchors remain durable.
                account_clause = ' AND s.id NOT IN (SELECT data_owner FROM accounts)' if has_accounts else ''
                hard_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec='seconds')
                retired = ('SELECT s.id FROM sessions s JOIN privacy_current p ON p.owner=s.id '
                           'WHERE s.created_at<? AND NOT EXISTS (SELECT 1 FROM visits v WHERE v.owner=s.id) '
                           'AND NOT EXISTS (SELECT 1 FROM privacy_events e WHERE e.owner=s.id AND e.created_at>=?)'
                           + account_clause)
                expired_owners += ' UNION ' + retired
                expiry_parameters.extend([hard_cutoff, hard_cutoff])
            # Freeze ownership decisions before deleting the joined consent rows.
            # Re-evaluating the query after each delete could change eligibility.
            owners = [row[0] for row in conn.execute(expired_owners, expiry_parameters)]
            conn.execute(f"DELETE FROM visits WHERE updated_at < ?{visit_guard}", (cutoff,))
            optional = [table for table in ('task_audit', 'task_audit_heads', 'privacy_current', 'privacy_events', 'auth_sessions')
                        if conn.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()]
            for offset in range(0, len(owners), 200):
                batch = owners[offset:offset + 200]
                placeholders = ','.join('?' for _ in batch)
                for table in ('visits', 'bookmarks', *optional):
                    conn.execute(f'DELETE FROM {table} WHERE owner IN ({placeholders})', batch)
                conn.execute(f'DELETE FROM sessions WHERE id IN ({placeholders})', batch)

    def close(self) -> None:
        # Connections are short-lived, including read connections.
        pass
