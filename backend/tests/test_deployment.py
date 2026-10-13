"""Cloud configuration and request boundaries, using only fictional fixtures."""
import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.deployment import DeploymentConfigurationError, RuntimeDeployment


DATA_KEY = base64.urlsafe_b64encode(b"r" * 32).decode()
TASK_KEY = base64.urlsafe_b64encode(b"t" * 32).decode()
ORIGIN = "https://zhihe-fixture.vercel.app"


def cloud_config(**overrides):
    return {
        "DATABASE_URL": "postgresql+psycopg://fictional:fixture@db.example.test/fixture?sslmode=require",
        "REDIS_URL": "rediss://fictional:fixture@queue.example.test:6379/0",
        "MEDICAL_DATA_KEY": DATA_KEY,
        "MEDICAL_TASK_KEY": TASK_KEY,
        "PUBLIC_APP_ORIGIN": ORIGIN,
        **overrides,
    }


def cloud_environment(**overrides):
    return {"VERCEL": "1", "VERCEL_URL": "zhihe-preview-fixture.vercel.app",
            "VERCEL_PROJECT_PRODUCTION_URL": "zhihe-fixture.vercel.app", **overrides}


def deployment(config=None, environment=None, **kwargs):
    return RuntimeDeployment.from_config(cloud_config() if config is None else config, Path("/fictional-app"),
                                         environment=cloud_environment() if environment is None else environment, **kwargs)


@pytest.mark.parametrize("flag", [None, "true", "0", ""])
def test_only_explicit_vercel_one_enables_cloud(flag):
    result = deployment({}, {} if flag is None else {"VERCEL": flag})
    assert not result.vercel
    assert result.runtime_dir == Path("/fictional-app/data")
    assert result.allowed_hosts == ("127.0.0.1", "localhost", "testserver", "[::1]")
    assert not result.secure_cookie and result.public_origin is None


@pytest.mark.parametrize("name", ["DATABASE_URL", "REDIS_URL", "MEDICAL_DATA_KEY", "MEDICAL_TASK_KEY", "PUBLIC_APP_ORIGIN"])
def test_cloud_rejects_missing_required_configuration(name):
    config = cloud_config()
    del config[name]
    with pytest.raises(DeploymentConfigurationError):
        deployment(config)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_provider_postgres_urls_use_installed_driver_and_preserve_tls(scheme, sslmode):
    provider_url = f"{scheme}://fictional:fictional%40password@db.example.test/fixture?sslmode={sslmode}&channel_binding=require"
    result = deployment(cloud_config(DATABASE_URL=provider_url))
    from sqlalchemy.engine import make_url
    normalized = make_url(result.database_url)
    assert normalized.drivername == "postgresql+psycopg"
    assert normalized.password == "fictional@password"
    assert normalized.query == {"sslmode": sslmode, "channel_binding": "require"}
    assert "fictional@password" not in repr(result)
    assert "fictional%40password" not in repr(result)


@pytest.mark.parametrize("query", ["", "?sslmode=disable", "?sslmode=allow", "?sslmode=prefer",
                                 "?sslmode=require&sslmode=disable"])
def test_cloud_database_tls_is_mandatory(query):
    url = "postgresql://fictional:fixture@db.example.test/fixture" + query
    with pytest.raises(DeploymentConfigurationError) as caught:
        deployment(cloud_config(DATABASE_URL=url))
    assert url not in str(caught.value)


def test_local_database_configuration_is_not_normalized_or_required_to_use_tls():
    url = "postgresql://fictional:fixture@localhost/fixture?sslmode=disable"
    result = deployment({"DATABASE_URL": url}, {})
    assert result.database_url == url


@pytest.mark.parametrize("override", [
    {"DATABASE_URL": "sqlite:///fictional.sqlite3"},
    {"DATABASE_URL": "postgresql://fixture@localhost/fixture"},
    {"DATABASE_URL": "malformed-fictional-credential"},
    {"REDIS_URL": "redis://queue.example.test:6379/0"},
    {"REDIS_URL": "rediss://localhost:6379/0"},
    {"REDIS_URL": "rediss://queue.example.test:invalid/0"},
    {"MEDICAL_DATA_KEY": "invalid-fictional-key"},
    {"MEDICAL_TASK_KEY": "invalid-fictional-key"},
    {"MEDICAL_DATA_KEY_FILE": "/fictional/record.key"},
    {"MEDICAL_TASK_KEY_FILE": "/fictional/task.key"},
    {"VISIT_DATA_PATH": "/tmp/fictional.sqlite3"},
])
def test_cloud_rejects_local_services_files_and_invalid_keys_without_echoing_values(override):
    with pytest.raises(DeploymentConfigurationError) as caught:
        deployment(cloud_config(**override))
    assert next(iter(override.values())) not in str(caught.value)


def test_cloud_rejects_explicit_sqlite_override_before_startup(tmp_path):
    with pytest.raises(DeploymentConfigurationError):
        deployment(db_path=tmp_path / "fictional.sqlite3")
    assert not list(tmp_path.iterdir())


def test_cloud_temp_paths_secure_cookies_and_exact_hosts(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.deployment.tempfile.gettempdir", lambda: str(tmp_path))
    result = deployment(cloud_config(COOKIE_SECURE="false"))
    assert result.vercel and result.secure_cookie
    assert result.runtime_dir == tmp_path / "zhihe-runtime"
    assert result.allowed_hosts == ("zhihe-preview-fixture.vercel.app", "zhihe-fixture.vercel.app")
    assert result.public_origin == ORIGIN
    assert not result.runtime_dir.exists()  # Configuration itself performs no file I/O.


@pytest.mark.parametrize("host", ["https://evil.vercel.app", "evil.vercel.app:443", "*.vercel.app",
                                 "evil.vercel.app.attacker.test", "evil.vercel.app/path"])
def test_generated_domains_cannot_widen_the_host_allowlist(host):
    with pytest.raises(DeploymentConfigurationError):
        deployment(environment=cloud_environment(VERCEL_URL=host))


def test_custom_domain_requires_explicit_allowlist():
    config = cloud_config(APP_ALLOWED_HOSTS="health.example.test", PUBLIC_APP_ORIGIN="https://health.example.test")
    result = deployment(config, cloud_environment(VERCEL_URL="health.example.test"))
    assert result.public_origin == "https://health.example.test"
    with pytest.raises(DeploymentConfigurationError):
        deployment(cloud_config(APP_ALLOWED_HOSTS="*.example.test"))


@pytest.mark.parametrize("origin", ["http://zhihe-fixture.vercel.app", "https://attacker.test",
                                   ORIGIN + "/path", ORIGIN + "?redirect=1", ORIGIN + "#fragment",
                                   "https://fictional@zhihe-fixture.vercel.app", ORIGIN + ":8443",
                                   ORIGIN + "?", "\n" + ORIGIN])
def test_public_origin_must_be_one_https_origin_on_the_allowlist(origin):
    with pytest.raises(DeploymentConfigurationError):
        deployment(cloud_config(PUBLIC_APP_ORIGIN=origin))


@pytest.fixture
def isolated_app_module(monkeypatch):
    # Never read the workspace .env or inherit configured runtime credentials.
    names = ("VERCEL", "VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL", "DATABASE_URL", "REDIS_URL",
             "MEDICAL_DATA_KEY", "MEDICAL_TASK_KEY", "MEDICAL_DATA_KEY_FILE", "MEDICAL_TASK_KEY_FILE",
             "VISIT_DATA_PATH", "APP_ALLOWED_HOSTS", "PUBLIC_APP_ORIGIN", "COOKIE_SECURE", "VISITOR_COOKIE_NAME",
             "LOCAL_SAFETY_ENABLED", "LOCAL_SAFETY_MODEL", "DEEPSEEK_API_KEY", "METRICS_TOKEN", "WORKER_MODE")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    import backend.model
    monkeypatch.setattr(backend.model, "read_project_env", lambda path: {})
    import backend.main
    monkeypatch.setattr(backend.main, "read_project_env", lambda path: {})
    return backend.main


def test_vercel_never_reads_project_env_or_opens_sqlite(isolated_app_module, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Cloud configuration attempted local configuration or SQLite access.")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(isolated_app_module, "read_project_env", forbidden)
    monkeypatch.setattr(isolated_app_module, "Store", forbidden)
    with pytest.raises(DeploymentConfigurationError):
        isolated_app_module.create_app()


def test_cloud_startup_cookie_origin_and_lexical_fallback(isolated_app_module, monkeypatch, tmp_path):
    main = isolated_app_module
    for name, value in cloud_environment().items():
        monkeypatch.setenv(name, value)
    provider_url = "postgres://fictional:fixture@db.example.test/fixture?sslmode=require"
    for name, value in cloud_config(DATABASE_URL=provider_url, COOKIE_SECURE="false", LOCAL_SAFETY_ENABLED="true").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("backend.deployment.tempfile.gettempdir", lambda: str(tmp_path))
    captured = {}
    from backend.storage import Store
    from backend.model import ModelService
    import backend.postgres_storage
    import backend.retrieval

    def fictional_postgres(url, *, data_dir, key_path, data_key):
        captured.update(url=url, data_dir=data_dir, key_path=key_path, data_key=data_key)
        return Store(tmp_path / "fictional.sqlite3", data_key=data_key)

    class QueueDouble:
        client = None
        def __init__(self):
            self.client = self
        async def initialize_queue(self):
            pass
        async def ping(self):
            return True
        async def close(self):
            pass

    class RateDouble:
        def close(self):
            pass

    def forbidden_ollama(*args, **kwargs):
        pytest.fail("Cloud startup attempted to construct a local Ollama retriever.")

    monkeypatch.setattr(backend.postgres_storage, "PostgreSQLStore", fictional_postgres)
    monkeypatch.setattr(main.RedisRuntime, "from_url", lambda url: QueueDouble())
    monkeypatch.setattr(main.RedisRateLimiter, "from_url", lambda url: RateDouble())
    monkeypatch.setattr(backend.retrieval, "HybridRetriever", forbidden_ollama)
    app = main.create_app(model_service=ModelService())
    with TestClient(app, base_url=ORIGIN) as client:
        result = client.get("/api/bootstrap")
        assert result.status_code == 200
        assert "Secure" in result.headers["set-cookie"]
        assert result.json()["retrieval"]["mode"] == "lexical_fallback"
        assert not result.json()["retrieval"]["embedding_available"]
        assert result.json()["privacy"]["storage_mode"] == "cloud"
        assert not app.state.safety_review.enabled
        assert app.state.retriever is None
        assert captured["data_dir"] == tmp_path / "zhihe-runtime" / "postgres"
        assert captured["url"].startswith("postgresql+psycopg://")
        assert captured["url"].endswith("?sslmode=require")
        assert captured["key_path"] is None and captured["data_key"] == DATA_KEY
        headers = {"X-CSRF-Token": result.json()["csrf_token"], "Origin": ORIGIN}
        consent = {"notice_version": result.json()["privacy"]["notice_version"], "local_storage": True,
                   "external_ai": False, "retention_days": 7}
        assert client.post("/api/privacy/consent", headers=headers, json=consent).status_code == 200
        forged = {**headers, "Origin": "https://attacker.test", "X-Forwarded-Host": "attacker.test",
                  "X-Forwarded-Proto": "https"}
        assert client.post("/api/visits", headers=forged, json={"title": "fictional"}).status_code == 403
        assert client.post("/api/visits", headers={**headers, "Origin": "https://zhihe-preview-fixture.vercel.app"},
                           json={"title": "fictional"}).status_code == 403
        assert client.post("/api/visits", headers={**headers, "X-Forwarded-Proto": "http"},
                           json={"title": "fictional"}).status_code == 201
        assert client.get("/healthz", headers={"Host": "attacker.test"}).status_code == 400
        registered = client.post("/api/account/register", headers=headers, json={
            "username": "fictional.cloud", "password": "Fictional-cloud-passphrase-1234",
        })
        assert registered.status_code == 201
        assert "Secure" in registered.headers["set-cookie"]
