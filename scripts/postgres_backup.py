"""Encrypted pg_dump/restore drill for the ISOLATED fixture database only.

No production URL is accepted, existing databases cannot be restored over, and
keys are included inside the password-encrypted archive. This is not a PITR or
remote backup service. The dump is produced under an exported read snapshot.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.data_crypto import DataCrypto
from scripts.backup import BackupError, MAX_ITEM, _cipher

MAGIC = b"ZHIHEPGBACKUP1\n"
CONTAINER = "zhihe-engineering-check-postgres-1"


def _fixture(config: Path):
    from sqlalchemy.engine import make_url
    values = dict(line.split("=", 1) for line in config.read_text(encoding="utf-8-sig").splitlines() if "=" in line and not line.startswith("#"))
    url = make_url(values["DATABASE_URL"])
    if url.host != "127.0.0.1" or url.port != 18432 or url.database != "medical_fixture" or url.username != "zhihe_fixture":
        raise BackupError("Only the isolated fixture connection is accepted.")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _container():
    result = subprocess.run(["docker", "inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}', CONTAINER],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=10)
    if result.stdout.strip() != b"zhihe-engineering-check":
        raise BackupError("Unexpected container project; no dump was performed.")


def _run(arguments, *, input=None) -> bytes:
    result = subprocess.run(["docker", "exec", "-i", CONTAINER, *arguments], input=input,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if result.returncode or len(result.stdout) > MAX_ITEM:
        raise BackupError("Fixture dump/restore process failed or exceeded size limit; raw stderr was not printed.")
    return result.stdout


def backup_fixture(config: Path, key_directory: Path, output: Path, password: bytes, *,
                   data_key_file: Path | None = None, task_key_file: Path | None = None) -> dict:
    import psycopg
    from psycopg import sql
    _container()
    if output.exists() or not output.parent.is_dir():
        raise BackupError("Choose a new archive in an existing private directory.")
    prefix = key_directory / "postgres.records"
    key_paths = {"data.key": data_key_file or prefix.with_suffix(".data.keys.json"),
                 "tasks.key": task_key_file or prefix.with_suffix(".tasks.key")}
    files = {name: path.read_bytes() for name, path in key_paths.items()}
    crypto = DataCrypto(prefix, key_path=key_paths["data.key"], encrypted_database=True)
    from cryptography.fernet import Fernet
    task_cipher = Fernet(files["tasks.key"].strip())
    with closing(psycopg.connect(_fixture(config))) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        if conn.execute("SELECT pg_database_size(current_database())").fetchone()[0] > MAX_ITEM:
            raise BackupError("Fixture database exceeds the finite backup limit.")
        metadata = dict(conn.execute("SELECT name,value FROM storage_crypto_meta").fetchall())
        crypto.select(metadata["key_id"])
        for owner, csrf in conn.execute("SELECT id,csrf FROM sessions"):
            crypto.open("session_csrf", owner, owner, csrf)
        for identifier, owner, payload in conn.execute("SELECT id,owner,payload FROM visits"):
            crypto.open("visit", owner, identifier, payload)
        for identifier, owner, visit_id, payload in conn.execute("SELECT id,owner,visit_id,payload FROM tasks"):
            value = json.loads(task_cipher.decrypt(payload.encode()))
            if value["owner"] != owner or value["task"]["id"] != identifier or value["task"]["visit_id"] != visit_id:
                raise BackupError("Task payload binding mismatch.")
        counts = {table: conn.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
                  for table in ("sessions", "visits", "tasks", "bookmarks")}
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        if not re.fullmatch(r"[0-9A-Fa-f-]{10,80}", snapshot):
            raise BackupError("Unexpected snapshot identifier.")
        files["database.dump"] = _run(["pg_dump", "-U", "zhihe_fixture", "-d", "medical_fixture", "-Fc", "--no-owner", "--no-acl", "--snapshot=" + snapshot])
    if any(path.read_bytes() != files[name] for name, path in key_paths.items()):
        raise BackupError("Key changed during dump; archive was not published.")
    manifest = {"format": 1, "engine": "postgresql", "scope": "isolated_fixture", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "schema_version": version, "counts": counts,
                "files": {name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()} for name, value in files.items()}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True).encode())
    salt = os.urandom(16)
    encrypted = MAGIC + base64.urlsafe_b64encode(salt) + b"\n" + _cipher(password, salt).encrypt(buffer.getvalue())
    with output.open("xb") as file:
        file.write(encrypted)
        file.flush()
        os.fsync(file.fileno())
    output.chmod(0o600)
    return manifest


def verify_fixture_archive(backup: Path, password: bytes) -> tuple[dict, dict[str, bytes]]:
    raw = backup.read_bytes()
    if len(raw) > 2 * MAX_ITEM or not raw.startswith(MAGIC):
        raise BackupError("Unexpected fixture archive.")
    try:
        salt_text, token = raw[len(MAGIC):].split(b"\n", 1)
        salt = base64.urlsafe_b64decode(salt_text)
        if len(salt) != 16:
            raise ValueError("salt")
        plain = _cipher(password, salt).decrypt(token)
        with zipfile.ZipFile(io.BytesIO(plain)) as archive:
            expected = {"manifest.json", "database.dump", "data.key", "tasks.key"}
            if len(archive.namelist()) != 4 or set(archive.namelist()) != expected or any(item.file_size > MAX_ITEM for item in archive.infolist()):
                raise ValueError("archive entries")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest["format"] != 1 or manifest["engine"] != "postgresql" or manifest["scope"] != "isolated_fixture" or set(manifest["files"]) != expected - {"manifest.json"}:
                raise ValueError("manifest")
            files = {name: archive.read(name) for name in expected - {"manifest.json"}}
            for name, value in files.items():
                if len(value) != manifest["files"][name]["size"] or hashlib.sha256(value).hexdigest() != manifest["files"][name]["sha256"]:
                    raise ValueError("manifest hash")
        return manifest, files
    except Exception as error:
        raise BackupError("Fixture archive/password authentication failed.") from error


def restore_fixture(config: Path, backup: Path, key_directory: Path, password: bytes) -> dict:
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url
    from backend.postgres_storage import PostgreSQLStore
    from backend.task_storage import TaskStore
    _container()
    if key_directory.exists():
        raise BackupError("Restore key directory must be new; existing artifacts are never overwritten.")
    manifest, files = verify_fixture_archive(backup, password)
    target = "zhihe_restore_" + secrets.token_hex(8)
    # Only a newly generated database is created; no DROP/ALTER existing DB.
    with closing(psycopg.connect(_fixture(config), autocommit=True)) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target)))
    _run(["pg_restore", "-U", "zhihe_fixture", "-d", target, "--no-owner", "--no-acl", "--exit-on-error"], input=files["database.dump"])
    key_directory.mkdir(parents=True)
    prefix = key_directory / "postgres.records"
    for name, destination in (("data.key", prefix.with_suffix(".data.keys.json")), ("tasks.key", prefix.with_suffix(".tasks.key"))):
        with destination.open("xb") as file:
            file.write(files[name])
        destination.chmod(0o600)
    url = make_url(_fixture(config)).set(drivername="postgresql+psycopg", database=target).render_as_string(hide_password=False)
    store = PostgreSQLStore(url, data_dir=key_directory)
    task_store = TaskStore(store)
    try:
        with store.connect() as conn:
            counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in manifest["counts"]}
            owners = [row[0] for row in conn.execute("SELECT id FROM sessions").fetchall()]
        if counts != manifest["counts"]:
            raise BackupError("Restored fixture row counts differ.")
        for owner in owners:
            for visit in store.list_visits(owner):
                task_store.list(owner, visit.id)
            task_store.audit(owner)
        return {"restored": True, "new_database": target, "counts_match": True, "counts": counts,
                "encrypted_records_authenticated": True, "task_audit_verified": True,
                "notice": "New isolated fixture DB and keys retained for inspection. No production DB was overwritten; this does not establish remote backups/PITR."}
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("backup", "verify", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--password-file", type=Path, required=True)
        command.add_argument("--backup", type=Path, required=True)
        if name != "verify":
            command.add_argument("--config", type=Path, default=Path("data/runtime-check.env"))
            command.add_argument("--key-directory", type=Path, required=True)
        if name == "backup":
            command.add_argument("--data-key-file", type=Path)
            command.add_argument("--task-key-file", type=Path)
    args = parser.parse_args()
    try:
        password = args.password_file.read_bytes().rstrip(b"\r\n")
        if args.action == "backup":
            result = backup_fixture(args.config, args.key_directory, args.backup, password,
                                    data_key_file=args.data_key_file, task_key_file=args.task_key_file)
        elif args.action == "restore":
            result = restore_fixture(args.config, args.backup, args.key_directory, password)
        else:
            result, _ = verify_fixture_archive(args.backup, password)
            result = {"authenticated_archive": True, **result}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print("Fixture PostgreSQL backup/restore failed: " + type(error).__name__ + "; no credentials, keys, or record contents printed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
