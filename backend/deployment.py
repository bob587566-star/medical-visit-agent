"""Validate cloud configuration without opening records, keys, or connections."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class DeploymentConfigurationError(ValueError):
    """An incomplete deployment must fail before opening any storage."""


_LOCAL_HOSTS = "127.0.0.1,localhost,testserver,[::1]"
_HOST = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "postgres", "redis", "host.docker.internal"}


def is_vercel(environment: Mapping[str, str] | None = None) -> bool:
    return (os.environ if environment is None else environment).get("VERCEL") == "1"


def _hostname(value: str) -> str:
    host = value.lower()
    if not _HOST.fullmatch(host):
        raise DeploymentConfigurationError("Deployment hosts must be exact hostnames without URLs, ports, or wildcards.")
    return host


def _public_origin(value: str, hosts: tuple[str, ...]) -> str:
    try:
        parsed = urlsplit(value)
        host = _hostname(parsed.hostname or "")
        valid = (parsed.scheme == "https" and parsed.port in {None, 443}
                 and parsed.username is None and parsed.password is None
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                 and host in hosts
                 and value.lower() in {"https://" + host, "https://" + host + "/",
                                       "https://" + host + ":443", "https://" + host + ":443/"})
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise DeploymentConfigurationError("PUBLIC_APP_ORIGIN must be one exact HTTPS origin whose hostname is allowed.")
    return "https://" + host


def _durable_services(config: Mapping[str, str]) -> str:
    required = ("DATABASE_URL", "REDIS_URL", "MEDICAL_DATA_KEY", "MEDICAL_TASK_KEY")
    if any(not config.get(name) for name in required):
        raise DeploymentConfigurationError("Vercel requires DATABASE_URL, REDIS_URL, MEDICAL_DATA_KEY, and MEDICAL_TASK_KEY; local storage is not a fallback.")
    if any(config.get(name) for name in ("MEDICAL_DATA_KEY_FILE", "MEDICAL_TASK_KEY_FILE", "VISIT_DATA_PATH")):
        raise DeploymentConfigurationError("Vercel requires environment keys and PostgreSQL; local record or key files are not supported.")
    try:
        database = make_url(config["DATABASE_URL"])
        if (database.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}
                or not database.host or not database.database or database.host.lower() in _LOOPBACK):
            raise ValueError("unsupported database")
    except (ValueError, TypeError, ArgumentError):
        raise DeploymentConfigurationError("DATABASE_URL must identify a remote PostgreSQL database.") from None
    if database.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise DeploymentConfigurationError("Vercel DATABASE_URL requires sslmode=require, verify-ca, or verify-full.")
    try:
        redis = urlsplit(config["REDIS_URL"])
        if (redis.scheme != "rediss" or not redis.hostname or redis.hostname.lower() in _LOOPBACK
                or redis.fragment or (redis.port is not None and not 1 <= redis.port <= 65535)):
            raise ValueError("unsupported queue")
    except (ValueError, TypeError):
        raise DeploymentConfigurationError("REDIS_URL must identify a remote Redis service over TLS.") from None
    for name in ("MEDICAL_DATA_KEY", "MEDICAL_TASK_KEY"):
        try:
            Fernet(config[name].encode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            raise DeploymentConfigurationError(f"{name} must be a valid stable Fernet key.") from None
    return database.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


@dataclass(frozen=True)
class RuntimeDeployment:
    vercel: bool
    allowed_hosts: tuple[str, ...]
    public_origin: str | None
    secure_cookie: bool
    runtime_dir: Path
    database_url: str | None = field(default=None, repr=False)

    @classmethod
    def from_config(cls, config: Mapping[str, str], root: Path, *, db_path: Path | None = None,
                    environment: Mapping[str, str] | None = None) -> "RuntimeDeployment":
        environment = os.environ if environment is None else environment
        cloud = is_vercel(environment)
        if not cloud:
            hosts = tuple(host.strip() for host in config.get("APP_ALLOWED_HOSTS", _LOCAL_HOSTS).split(",") if host.strip())
            if not hosts or "*" in hosts:
                raise DeploymentConfigurationError("必须显式配置服务主机名，不能使用无限制Host白名单。")
            origin = _public_origin(config["PUBLIC_APP_ORIGIN"], hosts) if config.get("PUBLIC_APP_ORIGIN") else None
            return cls(False, hosts, origin, config.get("COOKIE_SECURE", "false").lower() == "true", root / "data",
                       database_url=config.get("DATABASE_URL"))
        if db_path is not None:
            raise DeploymentConfigurationError("Vercel cannot use a local SQLite override.")
        database_url = _durable_services(config)
        explicit = tuple(_hostname(host.strip()) for host in config.get("APP_ALLOWED_HOSTS", "").split(",") if host.strip())
        hosts = list(explicit)
        for name in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
            if environment.get(name):
                host = _hostname(environment[name])
                if not host.endswith(".vercel.app") and host not in explicit:
                    raise DeploymentConfigurationError("Vercel deployment domains must end in .vercel.app or be explicitly allowed.")
                if host not in hosts:
                    hosts.append(host)
        if not hosts:
            raise DeploymentConfigurationError("Configure an exact deployment hostname before starting Vercel.")
        if not config.get("PUBLIC_APP_ORIGIN"):
            raise DeploymentConfigurationError("Vercel requires PUBLIC_APP_ORIGIN for origin validation.")
        allowed = tuple(hosts)
        return cls(True, allowed, _public_origin(config["PUBLIC_APP_ORIGIN"], allowed), True,
                   Path(tempfile.gettempdir()) / "zhihe-runtime", database_url=database_url)
