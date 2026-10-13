import sqlite3

import pytest

from backend.migrations import MigrationError, apply_migrations, migration_status


def test_baseline_creates_and_is_idempotent(tmp_path):
    with sqlite3.connect(tmp_path / "schema.db") as conn:
        first = apply_migrations(conn)
        assert first["current"] == first["target"] == 3
        assert apply_migrations(conn) == first
        assert {row[1] for row in conn.execute("PRAGMA table_info(visits)")} == {"id", "owner", "payload", "updated_at"}


def test_adopts_original_schema_without_rewriting_payload(tmp_path):
    with sqlite3.connect(tmp_path / "old.db") as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,csrf TEXT NOT NULL,created_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE visits(id TEXT PRIMARY KEY,owner TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO sessions VALUES('fictional-owner','csrf-test','2026-10-13')")
        conn.execute("INSERT INTO visits VALUES('fictional-visit','fictional-owner','opaque-encrypted-or-old-payload','2026-10-13')")
        apply_migrations(conn)
        assert conn.execute("SELECT payload FROM visits").fetchone()[0] == "opaque-encrypted-or-old-payload"


def test_tampered_migration_rejected(tmp_path):
    with sqlite3.connect(tmp_path / "tampered.db") as conn:
        apply_migrations(conn)
        conn.execute("UPDATE schema_migrations SET checksum='changed'")
        with pytest.raises(MigrationError):
            apply_migrations(conn)


def test_future_migration_rejected(tmp_path):
    with sqlite3.connect(tmp_path / "future.db") as conn:
        apply_migrations(conn)
        conn.execute("INSERT INTO schema_migrations VALUES(4,'future','unknown','2026-10-13')")
        with pytest.raises(MigrationError):
            apply_migrations(conn)


def test_incompatible_old_schema_rolls_back_registry(tmp_path):
    with sqlite3.connect(tmp_path / "bad.db") as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        with pytest.raises(MigrationError):
            apply_migrations(conn)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        assert [row[1] for row in conn.execute("PRAGMA table_info(sessions)")] == ["id"]


def test_upgrade_version_one_keeps_checksum_payload_and_adds_identity_queue(tmp_path):
    from backend.migrations import MIGRATIONS
    with sqlite3.connect(tmp_path / "v1.db") as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,applied_at TEXT NOT NULL)")
        for statement in MIGRATIONS[0].statements:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_migrations VALUES(?,?,?,'2026-10-12')", (1, MIGRATIONS[0].name, MIGRATIONS[0].checksum))
        conn.execute("INSERT INTO sessions VALUES('fictional-owner','old-csrf','2026-10-12')")
        conn.execute("INSERT INTO visits VALUES('fictional-visit','fictional-owner','old-opaque-payload','2026-10-12')")
        result = apply_migrations(conn)
        assert result["current"] == 3
        assert result["applied"][0]["applied_at"] == "2026-10-12"
        assert conn.execute("SELECT payload FROM visits").fetchone()[0] == "old-opaque-payload"
        assert {"claim_token", "dispatched_at"} <= {row[1] for row in conn.execute("PRAGMA table_info(persistent_task_inputs)")}
