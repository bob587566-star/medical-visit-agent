import sqlite3

from cryptography.fernet import Fernet
import pytest

from backend.data_crypto import DataCrypto
from backend.schemas import Risk, Visit
from backend.storage import Store, now
from backend.task_storage import TaskStore
from scripts.backup import BackupError, create_backup, restore_backup, verify_backup

PASSWORD = b"fictional-backup-password-only-20261013"


def seeded(tmp_path, *, external=False, environment=False):
    database = tmp_path / "original.sqlite3"
    kwargs = {}
    key_file = tmp_path / "separate-vault.key"
    raw = Fernet.generate_key()
    if external:
        key_file.write_bytes(raw)
        kwargs["key_path"] = key_file
    elif environment:
        kwargs["data_key"] = raw
    store = Store(database, **kwargs)
    cookie, owner, csrf = store.new_session()
    stamp = now()
    visit = Visit(id="fictional-visit", title="虚构备份练习", created_at=stamp, updated_at=stamp,
                  risk=Risk(message="虚构测试"))
    visit.intake.chief_complaint = "虚构备份测试自述，不能出现在加密归档中"
    store.put(owner, visit)
    tasks = TaskStore(store)
    task = {"id": "fictional-task", "visit_id": visit.id, "status": "completed", "updated_at": stamp,
            "draft": "虚构任务正文"}
    assert tasks.put(owner, task)
    return database, store, cookie, owner, csrf, visit, task, raw, key_file


def test_encrypted_online_backup_round_trip_preserves_records_keys_and_audit(tmp_path):
    database, store, cookie, owner, csrf, visit, task, _, _ = seeded(tmp_path)
    # A concurrent connection stays open; committed WAL rows must be in snapshot.
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO bookmarks VALUES (?,?)", (owner, "public-source"))
    writer.commit()
    output = tmp_path / "backup.zhihe"
    report = create_backup(database, output, PASSWORD)
    assert report["records_encrypted"] and report["key_roles"] == ["data", "tasks"]
    assert report["counts"]["bookmarks"] == 1
    raw = output.read_bytes()
    assert visit.intake.chief_complaint.encode() not in raw and b"fictional-task" not in raw
    assert store.data_crypto.key_path.read_bytes() not in raw
    assert verify_backup(output, PASSWORD)["verified"]
    target = tmp_path / "restored.sqlite3"
    restore_backup(output, target, PASSWORD)
    restored = Store(target)
    assert restored.session(cookie) == (owner, csrf)
    assert restored.get(owner, visit.id) == visit
    assert restored.bookmarks(owner) == ["public-source"]
    restored_tasks = TaskStore(restored)
    assert restored_tasks.get(owner, visit.id, task["id"]) == task
    assert restored_tasks.audit(owner)["verified"]
    writer.close()


@pytest.mark.parametrize("source", ("external", "environment"))
def test_external_keys_restored_to_new_local_keyring(tmp_path, source):
    database, _, cookie, owner, csrf, visit, _, raw, key_file = seeded(tmp_path, external=source == "external", environment=source == "environment")
    options = {"data_key_file": key_file} if source == "external" else {"data_key": raw}
    output = tmp_path / "external.zhihe"
    create_backup(database, output, PASSWORD, **options)
    target = tmp_path / "new.sqlite3"
    restore_backup(output, target, PASSWORD)
    restored = Store(target)
    assert restored.session(cookie) == (owner, csrf)
    assert restored.get(owner, visit.id) == visit


def test_missing_or_wrong_record_key_never_publishes_backup(tmp_path):
    database, store, *_ = seeded(tmp_path)
    key_path = store.data_crypto.key_path
    original = key_path.read_bytes()
    key_path.unlink()
    with pytest.raises(BackupError):
        create_backup(database, tmp_path / "missing.zhihe", PASSWORD)
    assert not (tmp_path / "missing.zhihe").exists()
    key_path.write_bytes(Fernet.generate_key())
    with pytest.raises(BackupError):
        create_backup(database, tmp_path / "wrong.zhihe", PASSWORD)
    key_path.write_bytes(original)


def test_missing_task_key_is_rejected_even_when_record_key_is_valid(tmp_path):
    database, *_ = seeded(tmp_path)
    database.with_suffix(".tasks.key").unlink()
    with pytest.raises(BackupError):
        create_backup(database, tmp_path / "missing-task.zhihe", PASSWORD)


def test_wrong_password_tamper_and_restore_existing_target_fail_closed(tmp_path):
    database, *_ = seeded(tmp_path)
    output = tmp_path / "good.zhihe"
    create_backup(database, output, PASSWORD)
    with pytest.raises(BackupError):
        verify_backup(output, b"incorrect-backup-password")
    with pytest.raises(BackupError):
        restore_backup(output, database, PASSWORD)
    tampered = tmp_path / "tampered.zhihe"
    raw = bytearray(output.read_bytes())
    raw[-30] ^= 1
    tampered.write_bytes(raw)
    with pytest.raises(BackupError):
        restore_backup(tampered, tmp_path / "should-not-exist.sqlite3", PASSWORD)
    assert not (tmp_path / "should-not-exist.sqlite3").exists()


def test_target_wal_or_key_exists_restore_refuses(tmp_path):
    database, *_ = seeded(tmp_path)
    output = tmp_path / "good.zhihe"
    create_backup(database, output, PASSWORD)
    target = tmp_path / "offline.sqlite3"
    target.with_suffix(".tasks.key").write_bytes(b"already-there")
    with pytest.raises(BackupError):
        restore_backup(output, target, PASSWORD)
    assert target.with_suffix(".tasks.key").read_bytes() == b"already-there"
    assert not target.exists()


def test_damaged_authenticated_record_is_not_backed_up_as_usable(tmp_path):
    database, *_ = seeded(tmp_path)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE visits SET payload='zhihe:v1:missing:damaged'")
    with pytest.raises(BackupError):
        create_backup(database, tmp_path / "bad.zhihe", PASSWORD)


def test_legacy_snapshot_can_be_backed_up_before_encryption_migration(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,csrf TEXT NOT NULL,created_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE visits(id TEXT PRIMARY KEY,owner TEXT NOT NULL REFERENCES sessions(id),payload TEXT NOT NULL,updated_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE bookmarks(owner TEXT NOT NULL REFERENCES sessions(id),source_id TEXT NOT NULL,PRIMARY KEY(owner,source_id))")
        conn.execute("INSERT INTO sessions VALUES('legacy-test','fictional-csrf','2026-10-13')")
    output = tmp_path / "legacy.zhihe"
    report = create_backup(database, output, PASSWORD)
    assert not report["records_encrypted"] and report["key_roles"] == []
    assert verify_backup(output, PASSWORD)["verified"]


def test_short_password_or_existing_output_refused(tmp_path):
    database, *_ = seeded(tmp_path)
    with pytest.raises(BackupError):
        create_backup(database, tmp_path / "short.zhihe", b"short")
    output = tmp_path / "existing.zhihe"
    output.write_bytes(b"do not overwrite")
    with pytest.raises(BackupError):
        create_backup(database, output, PASSWORD)
    assert output.read_bytes() == b"do not overwrite"


@pytest.mark.parametrize("source", ("external", "environment"))
def test_task_key_external_or_environment_is_preserved(tmp_path, source):
    database, store, _, owner, _, visit, _, _, _ = seeded(tmp_path)
    original_task_key = database.with_suffix(".tasks.key")
    key = original_task_key.read_bytes()
    vault = tmp_path / "task-vault.key"
    original_task_key.rename(vault)
    options = {"task_key_file": vault} if source == "external" else {"task_key": key}
    backup = tmp_path / "task-external.zhihe"
    create_backup(database, backup, PASSWORD, **options)
    target = tmp_path / "task-restored.sqlite3"
    restore_backup(backup, target, PASSWORD)
    assert TaskStore(Store(target)).audit(owner)["verified"]


def test_backup_rejects_damaged_ciphertext_in_extension_table(tmp_path):
    database, store, *_ = seeded(tmp_path)
    with store.connect() as conn:
        conn.execute("CREATE TABLE fictional_extension(payload TEXT NOT NULL)")
        conn.execute("INSERT INTO fictional_extension VALUES(?)", (DataCrypto.PREFIX + "missing:damaged",))
    with pytest.raises(BackupError):
        create_backup(database, tmp_path / "extension-bad.zhihe", PASSWORD)


def test_backup_refuses_tampered_audit_chain(tmp_path):
    database, *_ = seeded(tmp_path)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE task_audit SET mac='changed'")
    with pytest.raises(BackupError):
        create_backup(database, tmp_path / "tampered-audit.zhihe", PASSWORD)
