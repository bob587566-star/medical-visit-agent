"""Versioned SQLite baseline migrations, run inside the caller's transaction.

Existing installations are adopted without rewriting clinical payloads. A saved
checksum prevents editing an already applied migration; incompatible/future
versions fail closed instead of attempting an implicit downgrade.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


class MigrationError(ValueError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        return hashlib.sha256("\n".join(self.statements).encode()).hexdigest()


MIGRATIONS = (
    Migration(1, "visitor_records_baseline", (
        "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS visits (id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id), payload TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS visits_owner ON visits(owner)",
        "CREATE TABLE IF NOT EXISTS bookmarks (owner TEXT NOT NULL REFERENCES sessions(id), source_id TEXT NOT NULL, PRIMARY KEY(owner,source_id))",
    )),
    Migration(2, "encrypted_identity_consent_and_task_records", (
        "CREATE TABLE IF NOT EXISTS storage_crypto_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS storage_crypto_events (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, data_owner TEXT UNIQUE NOT NULL REFERENCES sessions(id), username_hash TEXT UNIQUE NOT NULL, profile TEXT NOT NULL, password_hash TEXT NOT NULL, recovery_digest TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','reviewer','admin')), created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS auth_sessions (id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, owner TEXT NOT NULL REFERENCES sessions(id), csrf TEXT NOT NULL, expires_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS auth_attempts (id TEXT PRIMARY KEY, window_start TEXT NOT NULL, attempts INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS privacy_events (id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id), payload TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS privacy_current (owner TEXT PRIMARY KEY REFERENCES sessions(id), event_id TEXT NOT NULL REFERENCES privacy_events(id), payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id), visit_id TEXT NOT NULL REFERENCES visits(id) ON DELETE CASCADE, payload TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS tasks_owner_visit ON tasks(owner,visit_id)",
        "CREATE TABLE IF NOT EXISTS task_audit (owner TEXT NOT NULL REFERENCES sessions(id), seq INTEGER NOT NULL, event TEXT NOT NULL, mac TEXT NOT NULL, PRIMARY KEY(owner,seq))",
        "CREATE TABLE IF NOT EXISTS task_audit_heads (owner TEXT PRIMARY KEY REFERENCES sessions(id), seq INTEGER NOT NULL, mac TEXT NOT NULL)",
    )),
    Migration(3, "persistent_task_outbox_and_fencing", (
        "CREATE TABLE IF NOT EXISTS persistent_task_inputs (task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, owner TEXT NOT NULL REFERENCES sessions(id), visit_id TEXT NOT NULL REFERENCES visits(id) ON DELETE CASCADE, payload TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','running')), claim_token TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, dispatched_at TEXT)",
        "CREATE INDEX IF NOT EXISTS persistent_task_active ON persistent_task_inputs(owner,visit_id,state)",
    )),
)

REQUIRED_COLUMNS = {
    "sessions": {"id", "csrf", "created_at"},
    "visits": {"id", "owner", "payload", "updated_at"},
    "bookmarks": {"owner", "source_id"},
    "storage_crypto_meta": {"name", "value"},
    "storage_crypto_events": {"id", "payload"},
    "accounts": {"id", "data_owner", "username_hash", "profile", "password_hash", "recovery_digest", "role", "created_at"},
    "auth_sessions": {"id", "account_id", "owner", "csrf", "expires_at"},
    "auth_attempts": {"id", "window_start", "attempts"},
    "privacy_events": {"id", "owner", "payload", "created_at"},
    "privacy_current": {"owner", "event_id", "payload"},
    "tasks": {"id", "owner", "visit_id", "payload", "updated_at"},
    "task_audit": {"owner", "seq", "event", "mac"},
    "task_audit_heads": {"owner", "seq", "mac"},
    "persistent_task_inputs": {"task_id", "owner", "visit_id", "payload", "state", "claim_token", "attempts", "updated_at", "dispatched_at"},
}


def migration_status(conn: sqlite3.Connection) -> dict:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
    rows = conn.execute("SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version").fetchall() if exists else []
    known = {item.version: item for item in MIGRATIONS}
    for version, name, checksum, _ in rows:
        if version not in known or name != known[version].name or checksum != known[version].checksum:
            raise MigrationError("Schema history differs from this application. Use the matching application version; no downgrade was attempted.")
    versions = [row[0] for row in rows]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError("Schema history has a missing migration.")
    return {"current": versions[-1] if versions else 0, "target": MIGRATIONS[-1].version,
            "applied": [{"version": row[0], "name": row[1], "checksum": row[2], "applied_at": row[3]} for row in rows]}


def apply_migrations(conn: sqlite3.Connection) -> dict:
    """Idempotent upgrade; never commits the caller's surrounding transaction."""
    conn.execute("SAVEPOINT medical_schema_upgrade")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        state = migration_status(conn)
        for migration in MIGRATIONS:
            if migration.version <= state["current"]:
                continue
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES(?,?,?,?)", (
                migration.version, migration.name, migration.checksum,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))
        for table, required in REQUIRED_COLUMNS.items():
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not required <= actual:
                raise MigrationError("An existing table has an incompatible schema; payloads were not rewritten.")
        conn.execute("RELEASE medical_schema_upgrade")
        return migration_status(conn)
    except Exception:
        conn.execute("ROLLBACK TO medical_schema_upgrade")
        conn.execute("RELEASE medical_schema_upgrade")
        raise
