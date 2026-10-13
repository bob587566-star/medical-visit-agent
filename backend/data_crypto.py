"""Authenticated record encryption with context binding and a versioned keyring.

This protects an offline database copy. Local key files are not a KMS and do not
protect against an attacker who can read both the database and the key file.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class DataIntegrityError(ValueError):
    """A record/key cannot be authenticated; callers must fail closed."""


class DataConsentError(ValueError):
    """Live storage consent changed before the guarded write transaction."""


def key_id(key: bytes) -> str:
    # Validate without disclosing the key in exceptions or logs.
    try:
        Fernet(key)
    except (ValueError, TypeError) as error:
        raise DataIntegrityError("数据加密密钥格式无效。") from error
    return hashlib.sha256(key).hexdigest()[:20]


def atomic_private_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("xb") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


class DataCrypto:
    PREFIX = "zhihe:v1:"

    def __init__(self, database: Path, *, key_path: Path | None = None,
                 data_key: str | bytes | None = None, encrypted_database: bool = False):
        configured = data_key if data_key is not None else os.environ.get("MEDICAL_DATA_KEY")
        configured_path = key_path or (Path(os.environ["MEDICAL_DATA_KEY_FILE"])
                                      if os.environ.get("MEDICAL_DATA_KEY_FILE") else None)
        if configured and configured_path:
            raise DataIntegrityError("请只配置一种数据密钥来源。")
        self.key_path: Path | None = None
        if configured:
            key = configured.encode("ascii") if isinstance(configured, str) else configured
            self.keys = {key_id(key): key}
            self.active_id = next(iter(self.keys))
            self.key_source = "environment"
        else:
            self.key_path = configured_path or database.with_suffix(".data.keys.json")
            self.key_source = "external_file" if configured_path else "local_file"
            if not self.key_path.exists():
                if encrypted_database:
                    raise DataIntegrityError("数据解密密钥缺失，未覆盖已有记录。请恢复原密钥。")
                if configured_path:
                    raise DataIntegrityError("已配置的数据密钥文件不存在。")
                key = Fernet.generate_key()
                # O_EXCL keeps two initializers from silently replacing each other's key.
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with self.key_path.open("xb") as file:
                        file.write(self._ring_bytes({key_id(key): key}, key_id(key)))
                        file.flush()
                        os.fsync(file.fileno())
                    self.key_path.chmod(0o600)
                except FileExistsError:
                    pass
            self.keys, self.active_id = self._read_key_file(self.key_path)
        self.ciphers = {identifier: Fernet(key) for identifier, key in self.keys.items()}

    @staticmethod
    def _ring_bytes(keys: dict[str, bytes], active: str) -> bytes:
        return json.dumps({"version": 1, "active": active,
                           "keys": {identifier: key.decode("ascii") for identifier, key in keys.items()}},
                          sort_keys=True, separators=(",", ":")).encode("ascii")

    @staticmethod
    def _read_key_file(path: Path) -> tuple[dict[str, bytes], str]:
        try:
            raw = path.read_bytes().strip()
            if not raw.startswith(b"{"):
                return {key_id(raw): raw}, key_id(raw)
            ring = json.loads(raw)
            if set(ring) != {"version", "active", "keys"} or ring["version"] != 1:
                raise ValueError("invalid ring")
            keys = {identifier: value.encode("ascii") for identifier, value in ring["keys"].items()}
            if not keys or any(identifier != key_id(value) for identifier, value in keys.items()):
                raise ValueError("invalid key identity")
            if ring["active"] not in keys:
                raise ValueError("unknown active key")
            return keys, ring["active"]
        except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
            raise DataIntegrityError("数据密钥文件无法校验。") from error

    def select(self, identifier: str) -> None:
        if identifier not in self.ciphers:
            raise DataIntegrityError("数据需要的原密钥不在当前密钥集中。")
        self.active_id = identifier

    def seal(self, purpose: str, owner: str, record_id: str, value) -> str:
        envelope = {"version": 1, "purpose": purpose, "owner": owner,
                    "id": record_id, "value": value}
        payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return self.PREFIX + self.active_id + ":" + self.ciphers[self.active_id].encrypt(payload).decode("ascii")

    def open(self, purpose: str, owner: str, record_id: str, payload: str):
        try:
            if not isinstance(payload, str) or not payload.startswith(self.PREFIX):
                raise ValueError("unencrypted record")
            identifier, token = payload[len(self.PREFIX):].split(":", 1)
            value = json.loads(self.ciphers[identifier].decrypt(token.encode("ascii")))
            if (set(value) != {"version", "purpose", "owner", "id", "value"}
                    or value["version"] != 1 or value["purpose"] != purpose
                    or value["owner"] != owner or value["id"] != record_id):
                raise ValueError("record context mismatch")
            return value["value"]
        except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError) as error:
            raise DataIntegrityError("记录完整性或归属校验失败，未读取未经校验的数据。") from error

    def prepare_rotation(self) -> str:
        if self.key_path is None:
            raise DataIntegrityError("环境单密钥不支持本机自动轮换，请使用版本化密钥文件。")
        key = Fernet.generate_key()
        identifier = key_id(key)
        self.keys[identifier] = key
        # Keep the original active key until the database transaction commits.
        atomic_private_write(self.key_path, self._ring_bytes(self.keys, self.active_id))
        self.ciphers[identifier] = Fernet(key)
        return identifier

    def commit_rotation(self, identifier: str) -> None:
        self.select(identifier)
        if self.key_path is None:
            raise DataIntegrityError("未配置可轮换的密钥文件。")
        atomic_private_write(self.key_path, self._ring_bytes(self.keys, identifier))

    def reseal(self, payload: str, identifier: str) -> str:
        """Authenticate an existing envelope and retain its context during rotation."""
        try:
            if not isinstance(payload, str) or not payload.startswith(self.PREFIX):
                raise ValueError('invalid ciphertext prefix')
            old_id, token = payload[len(self.PREFIX):].split(":", 1)
            value = json.loads(self.ciphers[old_id].decrypt(token.encode("ascii")))
            if set(value) != {"version", "purpose", "owner", "id", "value"} or value['version'] != 1:
                raise ValueError("invalid envelope")
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            return self.PREFIX + identifier + ":" + self.ciphers[identifier].encrypt(encoded).decode("ascii")
        except (InvalidToken, KeyError, TypeError, ValueError, UnicodeError) as error:
            raise DataIntegrityError("轮换遇到无法校验的数据，未提交部分更新。") from error
