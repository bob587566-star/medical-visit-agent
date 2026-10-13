import base64
import hashlib
import io
import json
import os
import zipfile

import pytest

from scripts.backup import BackupError, _cipher
from scripts.postgres_backup import MAGIC, _fixture, verify_fixture_archive

PASSWORD = b"fictional-pg-backup-password-only"


def archive(tmp_path, *, unexpected=False, bad_hash=False):
    files = {"database.dump": b"synthetic-dump-not-clinical", "data.key": b"synthetic-key", "tasks.key": b"synthetic-task-key"}
    manifest = {"format": 1, "engine": "postgresql", "scope": "isolated_fixture",
                "files": {name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()} for name, value in files.items()}}
    if bad_hash:
        manifest["files"]["database.dump"]["sha256"] = "changed"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, value in files.items():
            zipped.writestr(name, value)
        zipped.writestr("manifest.json", json.dumps(manifest))
        if unexpected:
            zipped.writestr("../should-not-extract", "synthetic")
    salt = os.urandom(16)
    path = tmp_path / "synthetic.pg-zhihe"
    path.write_bytes(MAGIC + base64.urlsafe_b64encode(salt) + b"\n" + _cipher(PASSWORD, salt).encrypt(buffer.getvalue()))
    return path


def test_fixture_archive_authenticates_and_checks_manifest(tmp_path):
    manifest, files = verify_fixture_archive(archive(tmp_path), PASSWORD)
    assert manifest["engine"] == "postgresql" and files["database.dump"] == b"synthetic-dump-not-clinical"


@pytest.mark.parametrize("mode", ("password", "unexpected", "hash", "tamper"))
def test_bad_fixture_archive_is_rejected(tmp_path, mode):
    path = archive(tmp_path, unexpected=mode == "unexpected", bad_hash=mode == "hash")
    password = b"incorrect-fictional-password" if mode == "password" else PASSWORD
    if mode == "tamper":
        value = bytearray(path.read_bytes())
        value[-30] ^= 1
        path.write_bytes(value)
    with pytest.raises(BackupError):
        verify_fixture_archive(path, password)
    assert not (tmp_path.parent / "should-not-extract").exists()


def test_non_fixture_pg_target_rejected_before_any_io(tmp_path):
    config = tmp_path / "not-fixture.env"
    config.write_text("DATABASE_URL=postgresql+psycopg://fictional:synthetic@database.example/real_database\n")
    with pytest.raises(BackupError):
        _fixture(config)
