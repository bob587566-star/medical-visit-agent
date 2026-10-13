import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.observability import RequestMetrics, attach_observability


def application(token=None, logger=None):
    app = FastAPI()

    @app.get("/fictional/{record_id}")
    async def read_record(record_id: str):
        return {"ok": True}

    @app.post("/fictional/{record_id}")
    async def write_record(record_id: str):
        return {"ok": True}

    attach_observability(app, metrics_token=token, logger=logger)
    return app


def test_metrics_default_deny_nonloopback_and_dont_trust_forwarded_for():
    with TestClient(application(), client=("198.51.100.8", 5000)) as client:
        response = client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
        assert response.status_code == 404


def test_local_metrics_use_route_template_not_record_id_or_query():
    app = application()
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        client.get("/fictional/fictional-person-123?text=private-symptom")
        client.get("/does-not-exist/fictional-person-456?text=private-symptom")
        body = client.get("/metrics").text
    assert 'route="/fictional/{record_id}"' in body and 'route="unmatched"' in body
    assert "fictional-person" not in body and "private-symptom" not in body
    assert "zhihe_http_request_duration_seconds_bucket" in body


def test_configured_token_required_even_for_loopback():
    token = "fictional-metrics-token-32-characters-long"
    with TestClient(application(token), client=("127.0.0.1", 5000)) as client:
        assert client.get("/metrics").status_code == 404
        assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
        assert client.get("/metrics", headers={"Authorization": "Bearer " + token}).status_code == 200


def test_non_ascii_bearer_is_denied_without_server_error():
    with TestClient(application('fictional-metrics-token-32-characters-long'), client=('127.0.0.1', 5000)) as client:
        assert client.get('/metrics', headers=[(b'Authorization', b'Bearer \xff')]).status_code == 404


def test_logs_contain_metadata_only(caplog):
    logger = logging.getLogger("zhihe.test_explicit_metadata")
    caplog.set_level(logging.INFO, logger=logger.name)
    with TestClient(application(logger=logger), client=("127.0.0.1", 5000)) as client:
        client.post("/fictional/fictional-account?text=private-query", json={"symptom": "private-health-text"},
                    headers={"Authorization": "Bearer private-secret", "Cookie": "secret-cookie"})
    entries = [json.loads(item.message) for item in caplog.records if item.name == logger.name]
    assert entries
    assert set(entries[-1]) == {"event", "method", "route", "status", "duration_ms"}
    assert entries[-1]["route"] == "/fictional/{record_id}"
    serialized = json.dumps(entries)
    assert all(word not in serialized for word in ("private-query", "private-health-text", "fictional-account", "private-secret", "secret-cookie"))


def test_histogram_counts_are_cumulative_and_sum_duration():
    metrics = RequestMetrics()
    metrics.observe("GET", "/readyz", 200, .2)
    metrics.observe("GET", "/readyz", 503, 2)
    output = metrics.render()
    assert 'le="0.3"} 1' in output
    assert 'le="3"} 2' in output
    assert 'le="+Inf"} 2' in output
    assert '_sum{method="GET",route="/readyz"} 2.200000000' in output


def test_duplicate_attach_or_short_token_rejected():
    with pytest.raises(ValueError):
        attach_observability(application())
    with pytest.raises(ValueError):
        application("short")


def test_default_logger_emits_json_info_without_duplicate_handlers():
    import io
    application()
    logger = logging.getLogger("zhihe.request_metadata")
    count = len(logger.handlers)
    application()
    assert len(logger.handlers) == count
    assert logger.level == logging.INFO and not logger.propagate
    assert any(getattr(handler, "_zhihe_metadata", False) for handler in logger.handlers)
