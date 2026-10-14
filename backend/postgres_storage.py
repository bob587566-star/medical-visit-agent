"""Pooled PostgreSQL storage for the application's bounded SQL contracts.

The adapter translates qmark binds, not user values. It supports reviewed
metadata/DDL differences explicitly; unsupported SQLite operations fail closed.
SQLite backup/rotation tools are deliberately not reused for a PostgreSQL URL.
"""
from __future__ import annotations

from contextlib import contextmanager
import re
import sqlite3
import threading
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url

from .data_crypto import DataCrypto, DataIntegrityError
from .migrations import apply_migrations
from .schemas import Visit
from .storage import Store


class DataConflictError(ValueError):
    pass


def bind_qmarks(sql: str, parameters=()) -> tuple[str, dict]:
    """Replace positional placeholders outside SQL literals/identifiers only."""
    result, count, index, quote = [], 0, 0, None
    while index < len(sql):
        char = sql[index]
        if quote:
            result.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    result.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            result.append(char)
        elif sql.startswith('--', index) or sql.startswith('/*', index) or char == '$':
            # Application SQL has no comments/dollar-quoted literals. Accepting
            # them without a complete parser would risk translating their binds.
            raise ValueError('Unsupported SQL quoting/comment syntax.')
        elif char == '?':
            result.append(':p' + str(count))
            count += 1
        elif char == ';' and sql[index + 1:].strip():
            raise ValueError('Only one SQL statement is supported.')
        else:
            result.append(char)
        index += 1
    if quote or len(parameters) != count:
        raise ValueError('Unbalanced SQL quote or parameter count mismatch.')
    return ''.join(result), {'p' + str(i): value for i, value in enumerate(parameters)}


class EmptyResult:
    rowcount = 0
    def __init__(self, rows=()):
        self.rows = list(rows)
    def fetchone(self):
        return self.rows.pop(0) if self.rows else None
    def fetchall(self):
        result, self.rows = self.rows, []
        return result
    def __iter__(self):
        return iter(self.rows)


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql: str, parameters=()):
        sql = sql.strip()
        if sql.upper().startswith('PRAGMA'):
            if sql.upper() in {'PRAGMA FOREIGN_KEYS=ON', 'PRAGMA SECURE_DELETE=ON', 'PRAGMA JOURNAL_MODE=WAL'}:
                return EmptyResult()
            match = re.fullmatch(r'PRAGMA table_info\((?:"([\w]+)"|([\w]+))\)', sql, re.I)
            if match:
                table = match.group(1) or match.group(2)
                rows = self.connection.execute(text('SELECT ordinal_position-1,column_name,data_type,is_nullable,column_default '
                    'FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=:table ORDER BY ordinal_position'), {'table': table}).fetchall()
                return EmptyResult([(row[0], row[1], row[2], row[3] == 'NO', row[4], 0) for row in rows])
            raise ValueError('Unsupported SQLite PRAGMA on PostgreSQL.')
        if 'sqlite_master' in sql:
            exact = sql.replace(' ', '').lower()
            if exact in {"select1fromsqlite_masterwheretype=?andname=?", "select1fromsqlite_masterwheretype='table'andname=?"}:
                if len(parameters) == 2:
                    if parameters[0] != 'table':
                        raise ValueError('Unsupported metadata entity.')
                    table = parameters[1]
                else:
                    table = parameters[0]
                return self.connection.execute(text('SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=:table'), {'table': table})
            match = re.fullmatch(r"SELECT 1 FROM sqlite_master WHERE type='table' AND name='([\w]+)'", sql, re.I)
            if match and not parameters:
                return self.connection.execute(text('SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=:table'), {'table': match.group(1)})
            raise ValueError('Unsupported SQLite metadata query on PostgreSQL.')
        if sql.upper() == 'BEGIN IMMEDIATE':
            return EmptyResult()  # connect() already owns a real locked transaction.
        if sql.upper().startswith('VACUUM') or re.search(r'\browid\b', sql, re.I):
            raise ValueError('SQLite maintenance cannot be used on PostgreSQL.')
        if sql.upper().startswith('CREATE TABLE'):
            sql = re.sub(r'\bINTEGER PRIMARY KEY AUTOINCREMENT\b', 'BIGSERIAL PRIMARY KEY', sql, flags=re.I)
        if sql.upper().startswith('INSERT OR IGNORE INTO '):
            sql = re.sub(r'^INSERT OR IGNORE INTO ', 'INSERT INTO ', sql, flags=re.I)
            if 'ON CONFLICT' in sql.upper():
                raise ValueError('Conflicting insert syntax.')
            sql += ' ON CONFLICT DO NOTHING'
        converted, binds = bind_qmarks(sql, parameters)
        try:
            return self.connection.execute(text(converted), binds)
        except IntegrityError:
            # Auth code can share its existing constraint handling. Driver errors
            # and parameters are not forwarded to the application's response.
            raise sqlite3.IntegrityError('Database constraint rejected operation.') from None

    def executemany(self, sql: str, rows):
        rows = list(rows)
        if not rows:
            return EmptyResult()
        converted, _ = bind_qmarks(sql, rows[0])
        values = [bind_qmarks(sql, row)[1] for row in rows]
        return self.connection.execute(text(converted), values)


class PostgreSQLStore(Store):
    def __init__(self, database_url: str, *, data_dir: Path | None = None,
                 key_path: Path | None = None, data_key: str | bytes | None = None,
                 schema: str = 'public'):
        url = make_url(database_url)
        if url.drivername not in {'postgresql', 'postgresql+psycopg'}:
            raise ValueError('Only PostgreSQL with the psycopg driver is supported.')
        if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', schema):
            raise ValueError('Invalid database schema identifier.')
        if data_dir is None:
            raise ValueError('Configure a stable data directory for PostgreSQL key artifacts.')
        data_dir.mkdir(parents=True, exist_ok=True)
        # This is a key artifact prefix, never a SQLite DB or a URL credential.
        self.path = str(data_dir / 'postgres.records')
        self.lock = threading.RLock()
        self.enforce_privacy = False
        self.engine = create_engine(url.set(drivername='postgresql+psycopg'),
                                    pool_size=5, max_overflow=5, pool_timeout=10,
                                    pool_recycle=300, pool_pre_ping=True, hide_parameters=True,
                                    connect_args={'connect_timeout': 5})
        self.schema = schema
        with self.connect() as conn:
            # The configured schema must already exist; production migrations
            # never create arbitrary databases or switch a live SQLite store.
            apply_migrations(conn)
            conn.execute('CREATE TABLE IF NOT EXISTS storage_crypto_meta (name TEXT PRIMARY KEY,value TEXT NOT NULL)')
            version = conn.execute("SELECT value FROM storage_crypto_meta WHERE name='version'").fetchone()
            encrypted = bool(version) or bool(conn.execute('SELECT 1 FROM visits WHERE payload LIKE ? LIMIT 1', (DataCrypto.PREFIX + '%',)).fetchone())
            encrypted = encrypted or bool(conn.execute('SELECT 1 FROM sessions WHERE csrf LIKE ? LIMIT 1', (DataCrypto.PREFIX + '%',)).fetchone())
            if encrypted and not version:
                raise DataIntegrityError('Encrypted records require their original version registration.')
            self.data_crypto = DataCrypto(Path(self.path), key_path=key_path,
                                          data_key=data_key, encrypted_database=encrypted)
            if version:
                if version[0] != '1':
                    raise DataIntegrityError('Unsupported record encryption version.')
                identifier = conn.execute("SELECT value FROM storage_crypto_meta WHERE name='key_id'").fetchone()
                if not identifier:
                    raise DataIntegrityError('Record key version is missing.')
                self.data_crypto.select(identifier[0])
                self._verify_encrypted(conn)
            else:
                # New PostgreSQL profiles must start clean, avoiding claims that
                # UPDATE can erase legacy PostgreSQL MVCC/WAL plaintext copies.
                if conn.execute('SELECT 1 FROM visits LIMIT 1').fetchone() or conn.execute('SELECT 1 FROM sessions LIMIT 1').fetchone():
                    raise DataIntegrityError('Import existing records through a separately reviewed encrypted migration.')
                conn.execute("INSERT INTO storage_crypto_meta VALUES('version','1')")
                conn.execute("INSERT INTO storage_crypto_meta VALUES('key_id',?)", (self.data_crypto.active_id,))

    @contextmanager
    def connect(self):
        with self.engine.begin() as connection:
            # Transaction-local settings work through PgBouncer and do not leak
            # a schema or timeout into another pooled client's transaction.
            connection.execute(text("SELECT set_config('search_path', :schema, true)"),
                               {'schema': self.schema})
            connection.execute(text("SELECT set_config('statement_timeout', '10000', true)"))
            # Reviewed legacy CRUD contains multi-statement audit and identity
            # transitions. Serialize these short database transactions across
            # workers; external model/network execution is outside this lock.
            connection.execute(text('SELECT pg_advisory_xact_lock(:namespace,:database)'),
                               {'namespace': 173489, 'database': 6270})
            yield PostgresConnection(connection)

    def decode_visit(self, owner: str, visit_id: str, payload: str) -> Visit:
        visit = super().decode_visit(owner, visit_id, payload)
        object.__setattr__(visit, '_storage_cipher_revision', payload)
        return visit

    def put(self, owner: str, visit: Visit) -> None:
        expected = getattr(visit, '_storage_cipher_revision', None)
        encrypted = self.encrypt_visit(owner, visit)
        with self.lock, self.connect() as conn:
            self.check_write_consent(conn, owner)
            if expected:
                changed = conn.execute('UPDATE visits SET payload=?,updated_at=? WHERE id=? AND owner=? AND payload=?',
                                       (encrypted, visit.updated_at, visit.id, owner, expected)).rowcount
                if not changed:
                    raise DataConflictError('Record changed concurrently; reload before retrying.')
            else:
                changed = conn.execute('INSERT INTO visits(id,owner,payload,updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING',
                                       (visit.id, owner, encrypted, visit.updated_at)).rowcount
                if not changed:
                    raise DataConflictError('Existing records require a loaded revision before an update.')
        object.__setattr__(visit, '_storage_cipher_revision', encrypted)

    def update_loaded_visit(self, conn, owner: str, visit: Visit) -> int:
        """CAS inside TaskStore's existing visit/task/audit transaction."""
        self.check_write_consent(conn, owner)
        expected = getattr(visit, '_storage_cipher_revision', None)
        if not expected:
            raise DataConflictError('Load the current record before applying a draft.')
        encrypted = self.encrypt_visit(owner, visit)
        changed = conn.execute('UPDATE visits SET payload=?,updated_at=? WHERE id=? AND owner=? AND payload=?',
                               (encrypted, visit.updated_at, visit.id, owner, expected)).rowcount
        if not changed:
            raise DataConflictError('Record changed concurrently; reload before retrying.')
        return changed

    def rotate_data_key(self) -> dict:
        raise ValueError('SQLite key-rotation tooling is not a PostgreSQL key/backup protocol.')

    def close(self) -> None:
        self.engine.dispose()
