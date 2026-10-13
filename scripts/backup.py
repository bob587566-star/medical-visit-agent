"""Authenticated SQLite backup and restore into a NEW, offline database.

The online backup API includes committed WAL data. Database and required keys
are bundled in memory, then encrypted with a separate backup password. This is
not a remote vault, a KMS, or proof that backups run automatically.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import zipfile

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.data_crypto import DataCrypto, DataIntegrityError, key_id
from backend.migrations import migration_status

MAGIC = b"ZHIHEBACKUP1\n"
MAX_ITEM = 128 * 1024 * 1024
MAX_ARCHIVE = 350 * 1024 * 1024


class BackupError(ValueError):
    pass


def _cipher(password: bytes, salt: bytes) -> Fernet:
    if not isinstance(password, bytes) or not 16 <= len(password) <= 4096:
        raise BackupError("Use a separate backup password containing at least 16 bytes.")
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password)
    return Fernet(base64.urlsafe_b64encode(key))


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_database(database: bytes, keys: dict[str, bytes]) -> dict:
    if len(database) > MAX_ITEM:
        raise BackupError("Database exceeds this backup tool's 128 MiB limit.")
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.deserialize(database)
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or conn.execute("PRAGMA foreign_key_check").fetchone():
            raise BackupError("Database integrity or foreign key verification failed.")
        tables = _tables(conn)
        if not {"sessions", "visits", "bookmarks"} <= tables:
            raise BackupError("This is not the expected application database.")
        schema = migration_status(conn)
        encrypted = bool("storage_crypto_meta" in tables and conn.execute("SELECT 1 FROM storage_crypto_meta WHERE name='version'").fetchone())
        encrypted = encrypted or bool(conn.execute("SELECT 1 FROM visits WHERE payload LIKE ? LIMIT 1", (DataCrypto.PREFIX + "%",)).fetchone())
        if encrypted:
            if "data" not in keys:
                raise BackupError("The record encryption key is required for this snapshot.")
            raw = keys["data"].strip()
            try:
                ring = json.loads(raw) if raw.startswith(b"{") else {"version": 1, "active": key_id(raw), "keys": {key_id(raw): raw.decode("ascii")}}
                if set(ring) != {"version", "active", "keys"} or ring["version"] != 1:
                    raise ValueError("invalid ring")
                crypto = DataCrypto.__new__(DataCrypto)
                crypto.ciphers = {identifier: Fernet(value.encode("ascii")) for identifier, value in ring["keys"].items() if identifier == key_id(value.encode("ascii"))}
                if not crypto.ciphers or len(crypto.ciphers) != len(ring["keys"]) or ring["active"] not in crypto.ciphers:
                    raise ValueError("invalid key identities")
                metadata = dict(conn.execute("SELECT name,value FROM storage_crypto_meta"))
                if metadata.get("version") != "1" or metadata.get("key_id") not in crypto.ciphers:
                    raise ValueError("missing active database key")
                for owner, csrf in conn.execute("SELECT id,csrf FROM sessions"):
                    crypto.open("session_csrf", owner, owner, csrf)
                for identifier, owner, payload in conn.execute("SELECT id,owner,payload FROM visits"):
                    crypto.open("visit", owner, identifier, payload)
                # Account/consent/audit modules may add encrypted fields later.
                # Authenticate every versioned ciphertext without printing or
                # serializing its plaintext into the verification report.
                for table in tables:
                    quoted_table = '"' + table.replace('"', '""') + '"'
                    for column in conn.execute(f"PRAGMA table_info({quoted_table})").fetchall():
                        if str(column[2]).upper() != "TEXT":
                            continue
                        quoted_column = '"' + column[1].replace('"', '""') + '"'
                        for (payload,) in conn.execute(f"SELECT {quoted_column} FROM {quoted_table} WHERE {quoted_column} LIKE ?", (DataCrypto.PREFIX + "%",)):
                            identifier, token = payload[len(DataCrypto.PREFIX):].split(":", 1)
                            value = json.loads(crypto.ciphers[identifier].decrypt(token.encode("ascii")))
                            if set(value) != {"version", "purpose", "owner", "id", "value"} or value["version"] != 1:
                                raise ValueError("invalid encrypted envelope")
            except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError, AttributeError, DataIntegrityError) as error:
                raise BackupError("Snapshot record keys or authenticated payloads could not be verified.") from error
        has_tasks = "tasks" in tables and bool(conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone())
        has_audit = "task_audit_heads" in tables and bool(conn.execute("SELECT 1 FROM task_audit_heads LIMIT 1").fetchone())
        if has_tasks or has_audit:
            if "tasks" not in keys:
                raise BackupError("The task encryption/audit key is required for this snapshot.")
            try:
                task_cipher = Fernet(keys["tasks"].strip())
                for identifier, owner, visit_id, payload in conn.execute("SELECT id,owner,visit_id,payload FROM tasks"):
                    value = json.loads(task_cipher.decrypt(payload.encode("ascii")))
                    if value["owner"] != owner or value["task"]["id"] != identifier or value["task"]["visit_id"] != visit_id:
                        raise ValueError("task binding")
                audit_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"zhihe-task-audit-v1").derive(base64.urlsafe_b64decode(keys["tasks"].strip()))
                for owner, expected_count, expected_mac in conn.execute("SELECT owner,seq,mac FROM task_audit_heads"):
                    rows = conn.execute("SELECT seq,event,mac FROM task_audit WHERE owner=? ORDER BY seq", (owner,)).fetchall()
                    previous = ""
                    for sequence, (seq, event, mac) in enumerate(rows, 1):
                        value = json.loads(event)
                        computed = hmac.new(audit_key, (previous + "\n" + event).encode(), hashlib.sha256).hexdigest()
                        if seq != sequence or value.get("owner") != owner or value.get("previous") != previous or not hmac.compare_digest(computed, mac):
                            raise ValueError("audit binding")
                        previous = mac
                    if expected_count != len(rows) or not hmac.compare_digest(previous, expected_mac):
                        raise ValueError("audit truncation")
            except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError) as error:
                raise BackupError("Snapshot task keys, payloads, or audit chain could not be verified.") from error
        return {"schema_version": schema["current"], "records_encrypted": encrypted,
                "counts": {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("sessions", "visits", "bookmarks", "tasks") if table in tables}}


def _snapshot(database: Path) -> bytes:
    database = database.resolve(strict=True)
    deadline = time.monotonic() + 30

    def progress(status, remaining, total):
        if total * 4096 > MAX_ITEM or time.monotonic() > deadline:
            raise BackupError("Backup size/time limit reached; no partial backup was published.")

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)) as source, closing(sqlite3.connect(":memory:")) as target:
        source.backup(target, pages=128, progress=progress, sleep=0.05)
        result = target.serialize()
    # SQLite's online backup has already folded committed WAL pages into this
    # image. Its deserialize API requires rollback-format header bytes; SQLite
    # explicitly documents normalizing bytes 18/19 for an in-memory WAL image.
    # https://www.sqlite.org/c3ref/deserialize.html
    if result.startswith(b"SQLite format 3\x00"):
        result = result[:18] + b"\x01\x01" + result[20:]
    if len(result) > MAX_ITEM:
        raise BackupError("Database exceeds this backup tool's 128 MiB limit.")
    return result


def create_backup(database: Path, output: Path, password: bytes, *, data_key_file: Path | None = None,
                  task_key_file: Path | None = None, data_key: bytes | str | None = None,
                  task_key: bytes | str | None = None) -> dict:
    """Read-only online snapshot; output is created exclusively, never overwritten."""
    database = Path(database).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists() or output == database or not output.parent.is_dir():
        raise BackupError("Backup output must be a new file in an existing directory.")
    configured_key = data_key if data_key is not None else os.environ.get("MEDICAL_DATA_KEY")
    configured_path = data_key_file or (Path(os.environ["MEDICAL_DATA_KEY_FILE"]) if os.environ.get("MEDICAL_DATA_KEY_FILE") else None)
    if configured_key and configured_path:
        raise BackupError("Choose one record key source.")
    configured_task_key = task_key if task_key is not None else os.environ.get("MEDICAL_TASK_KEY")
    configured_task_path = task_key_file or (Path(os.environ["MEDICAL_TASK_KEY_FILE"]) if os.environ.get("MEDICAL_TASK_KEY_FILE") else None)
    if configured_task_key and configured_task_path:
        raise BackupError("Choose one task key source.")
    paths = {} if configured_task_key else {"tasks": configured_task_path or database.with_suffix(".tasks.key")}
    if not configured_key:
        paths["data"] = configured_path or database.with_suffix(".data.keys.json")
    keys = {}
    if configured_key:
        raw = configured_key.encode("ascii") if isinstance(configured_key, str) else configured_key
        keys["data"] = DataCrypto._ring_bytes({key_id(raw): raw}, key_id(raw))
    if configured_task_key:
        raw = configured_task_key.encode("ascii") if isinstance(configured_task_key, str) else configured_task_key
        Fernet(raw)
        keys["tasks"] = raw
    for role, path in paths.items():
        if path.exists():
            raw = path.read_bytes()
            if len(raw) > 64 * 1024:
                raise BackupError("Unexpected key file size.")
            keys[role] = raw
        elif (role == "data" and configured_path) or (role == "tasks" and configured_task_path):
            raise BackupError("An explicitly configured key file is missing.")
    snapshot = _snapshot(database)
    for role, path in paths.items():
        if role in keys and (not path.exists() or path.read_bytes() != keys[role]):
            raise BackupError("Key file changed during backup. Pause key rotation and retry.")
    details = _validate_database(snapshot, keys)
    files = {"database.sqlite3": snapshot, **{role + ".key": value for role, value in keys.items()}}
    manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **details, "key_roles": sorted(keys),
                "files": {name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()} for name, value in files.items()}}
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        archive.writestr("manifest.json", _canonical(manifest).encode())
    salt = os.urandom(16)
    encrypted = MAGIC + base64.urlsafe_b64encode(salt) + b"\n" + _cipher(password, salt).encrypt(archive_buffer.getvalue())
    created = False
    try:
        with output.open("xb") as file:
            created = True
            file.write(encrypted)
            file.flush()
            os.fsync(file.fileno())
            output.chmod(0o600)
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    return {**manifest, "backup_bytes": len(encrypted), "backup_sha256": hashlib.sha256(encrypted).hexdigest()}


def _open_backup(path: Path, password: bytes) -> tuple[dict, dict[str, bytes]]:
    if path.stat().st_size > MAX_ARCHIVE:
        raise BackupError("Backup exceeds this tool's bounded archive size.")
    raw = path.read_bytes()
    try:
        if not raw.startswith(MAGIC):
            raise ValueError("format")
        salt_text, token = raw[len(MAGIC):].split(b"\n", 1)
        salt = base64.urlsafe_b64decode(salt_text)
        if len(salt) != 16:
            raise ValueError("salt")
        plain = _cipher(password, salt).decrypt(token)
        with zipfile.ZipFile(io.BytesIO(plain)) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names) or not set(names) <= {"manifest.json", "database.sqlite3", "data.key", "tasks.key"}:
                raise ValueError("archive names")
            if not {"manifest.json", "database.sqlite3"} <= set(names) or any(item.file_size > MAX_ITEM for item in archive.infolist()):
                raise ValueError("size")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest["format"] != 1 or set(manifest["files"]) != set(names) - {"manifest.json"}:
                raise ValueError("manifest")
            files = {}
            for name, metadata in manifest["files"].items():
                content = archive.read(name)
                if len(content) != metadata["size"] or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), metadata["sha256"]):
                    raise ValueError("hash")
                files[name] = content
        keys = {role: files[role + ".key"] for role in manifest["key_roles"]}
        details = _validate_database(files["database.sqlite3"], keys)
        if any(manifest[name] != value for name, value in details.items()):
            raise ValueError("snapshot details")
        return manifest, files
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError, zipfile.BadZipFile, sqlite3.Error) as error:
        raise BackupError("Backup password, authentication, format, or snapshot verification failed.") from error


def verify_backup(path: Path, password: bytes) -> dict:
    manifest, _ = _open_backup(Path(path), password)
    return {"verified": True, **manifest}


def restore_backup(path: Path, target: Path, password: bytes) -> dict:
    """NEW target only; publish keys first and the verified database last."""
    target = Path(target).resolve()
    if not target.parent.is_dir():
        raise BackupError("Create an empty offline restore directory first.")
    candidates = [target, Path(str(target) + "-wal"), Path(str(target) + "-shm"),
                  target.with_suffix(".data.keys.json"), target.with_suffix(".tasks.key")]
    if any(item.exists() for item in candidates):
        raise BackupError("Restore refuses any existing database, WAL, or key destination.")
    manifest, files = _open_backup(Path(path), password)
    destinations = {"data.key": target.with_suffix(".data.keys.json"), "tasks.key": target.with_suffix(".tasks.key"), "database.sqlite3": target}
    created = []
    try:
        for name in ("data.key", "tasks.key", "database.sqlite3"):
            if name not in files:
                continue
            destination = destinations[name]
            with destination.open("xb") as file:
                created.append(destination)
                file.write(files[name])
                file.flush()
                os.fsync(file.fileno())
            destination.chmod(0o600)
    except Exception:
        for destination in reversed(created):
            destination.unlink(missing_ok=True)
        raise
    return {"restored": True, "target_is_new": True, **manifest,
            "notice": "Offline target created. Verify matching application, key configuration and counts before switching service. Existing databases were not overwritten."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("backup")
    create.add_argument("--db", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    create.add_argument("--data-key-file", type=Path)
    create.add_argument("--task-key-file", type=Path)
    for action in ("verify", "restore"):
        command = commands.add_parser(action)
        command.add_argument("--backup", type=Path, required=True)
        if action == "restore":
            command.add_argument("--target", type=Path, required=True)
    for command in commands.choices.values():
        command.add_argument("--password-file", type=Path, required=True, help="Separate private password file; contents never printed.")
    args = parser.parse_args()
    try:
        password = args.password_file.read_bytes().rstrip(b"\r\n")
        if args.action == "backup":
            result = create_backup(args.db, args.out, password, data_key_file=args.data_key_file, task_key_file=args.task_key_file)
        elif args.action == "verify":
            result = verify_backup(args.backup, password)
        else:
            result = restore_backup(args.backup, args.target, password)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BackupError, DataIntegrityError, OSError, sqlite3.Error):
        print("Backup operation failed. No password, key, or clinical payload is included in this error.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
