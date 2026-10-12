import asyncio
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.model import ModelService
from backend.tests.test_api import WaitingModel


def client_and_headers(client):
    boot = client.get("/api/bootstrap").json()
    return {"X-CSRF-Token": boot["csrf_token"]}


def test_v2_candidate_is_a_confirmation_not_automatic_fact(tmp_path):
    app = create_app(tmp_path / "db.sqlite3", ModelService())
    with TestClient(app) as client:
        headers = client_and_headers(client)
        visit = client.post("/api/visits", json={}, headers=headers).json()
        base = f"/api/visits/{visit['id']}"
        result = client.post(base + "/messages", json={"content": "我咳嗽三天了"}, headers=headers).json()
        assert result["assistant_message"]["intake_candidates"]
        assert result["visit"]["intake"]["chief_complaint"] == ""
        candidate = next(c for c in result["visit"]["intake_candidates"] if c["field"] == "chief_complaint")
        saved = client.post(base + f"/candidates/{candidate['id']}/confirm", json={}, headers=headers)
        assert saved.status_code == 200
        assert saved.json()["intake"]["chief_complaint"] == candidate["value"]
        assert saved.json()["summary"] is None
        assert saved.json()["collection"]["stage"] == "collecting"
        assert client.post(base + f"/candidates/{candidate['id']}/confirm", json={}, headers=headers).status_code == 404


def test_observations_plan_and_export_are_owned_self_reports(tmp_path):
    app = create_app(tmp_path / "db.sqlite3", ModelService())
    with TestClient(app) as client:
        headers = client_and_headers(client)
        visit = client.post("/api/visits", json={}, headers=headers).json()
        base = f"/api/visits/{visit['id']}"
        saved = client.post(base + "/observations", json={"kind": "temperature", "value": "37.4", "unit": "°C", "recorded_at": "2026-10-12T08:00:00+08:00", "notes": "虚构测量"}, headers=headers)
        assert saved.status_code == 200
        observation = saved.json()["observations"][0]
        plan = client.post(base + "/plan", json={}, headers=headers)
        assert plan.status_code == 200
        assert plan.json()["plan"]["source"] == "self_report"
        assert any(step["id"] == "timeline" for step in plan.json()["plan"]["steps"])
        summary = client.post(base + "/summary", json={}, headers=headers).json()
        assert "37.4" in summary["summary"]["text"]
        assert "37.4" in client.get(base + "/export").text
        removed = client.delete(base + f"/observations/{observation['id']}", headers=headers)
        assert removed.status_code == 200
        assert removed.json()["observations"] == []
        assert removed.json()["summary"] is None
        assert removed.json()["plan"] is None


def test_library_filters_bookmarks_and_workspace_are_per_visitor(tmp_path):
    app = create_app(tmp_path / "db.sqlite3", ModelService())
    with TestClient(app) as client:
        headers = client_and_headers(client)
        library = client.get("/api/library").json()
        source = library["items"][0]
        assert client.put(f"/api/library/bookmarks/{source['id']}", json={"saved": True}).status_code == 403
        assert client.put(f"/api/library/bookmarks/{source['id']}", json={"saved": True}, headers=headers).status_code == 200
        assert client.get("/api/library?bookmarked=true").json()["total"] == 1
        assert client.get("/api/workspace").json()["bookmarks_count"] == 1
        assert client.get("/api/library/evidence?q=不存在的医院地址").json()["items"] == []
        assert client.get("/api/library/no-such-source").status_code == 404
        assert client.delete("/api/data", headers=headers).status_code == 204
        assert client.get("/api/library?bookmarked=true").json()["total"] == 0


def test_v2_routes_cannot_access_another_visitor(tmp_path):
    app = create_app(tmp_path / "db.sqlite3", ModelService())
    with TestClient(app) as client:
        headers = client_and_headers(client)
        visit = client.post("/api/visits", json={}, headers=headers).json()
        base = f"/api/visits/{visit['id']}"
        client.cookies.clear()
        stranger = client_and_headers(client)
        routes = [
            ("PATCH", base + "/title", {"title": "越权修改"}),
            ("POST", base + "/plan", {}),
            ("POST", base + "/candidates/missing/confirm", {}),
            ("POST", base + "/observations", {"kind": "note", "value": "虚构", "recorded_at": "2026-10-12T08:00:00+08:00"}),
            ("DELETE", base + "/observations/missing", None),
        ]
        for method, url, body in routes:
            assert client.request(method, url, json=body, headers=stranger).status_code == 404


def test_old_observation_does_not_become_a_current_warning(tmp_path):
    app = create_app(tmp_path / "db.sqlite3", ModelService())
    with TestClient(app) as client:
        headers = client_and_headers(client)
        visit = client.post("/api/visits", json={}, headers=headers).json()
        base = f"/api/visits/{visit['id']}"
        body = {"kind": "symptom", "value": "胸口疼伴冷汗", "recorded_at": "2025-01-01T08:00:00+08:00"}
        result = client.post(base + "/observations", json=body, headers=headers)
        assert result.status_code == 200
        assert result.json()["risk"]["level"] != "emergency"
        body["recorded_at"] = datetime.now(timezone.utc).isoformat()
        result = client.post(base + "/observations", json=body, headers=headers)
        assert result.json()["risk"]["level"] == "emergency"


def test_old_saved_visit_payload_defaults_to_v2(tmp_path):
    app = create_app(tmp_path / "db.sqlite3", ModelService())
    with TestClient(app) as client:
        headers = client_and_headers(client)
        visit = client.post("/api/visits", json={}, headers=headers).json()
        for name in ("collection", "observations", "intake_candidates", "plan"):
            visit.pop(name)
        import json
        with app.state.store.connect() as conn:
            conn.execute("UPDATE visits SET payload=? WHERE id=?", (json.dumps(visit), visit["id"]))
        loaded = client.get(f"/api/visits/{visit['id']}")
        assert loaded.status_code == 200
        assert loaded.json()["observations"] == []
        assert loaded.json()["collection"]["stage"] == "start"


def test_saving_intake_cannot_silently_dismiss_chat_emergency(tmp_path):
    app = create_app(tmp_path / "db.sqlite3", ModelService())
    with TestClient(app) as client:
        headers = client_and_headers(client)
        visit = client.post("/api/visits", json={}, headers=headers).json()
        base = f"/api/visits/{visit['id']}"
        urgent = client.post(base + "/messages", json={"content": "现在胸痛冒冷汗"}, headers=headers)
        assert urgent.json()["visit"]["risk"]["level"] == "emergency"
        edited = client.patch(base + "/intake", json={"chief_complaint": "虚构咳嗽"}, headers=headers)
        assert edited.json()["risk"]["level"] == "emergency"
        result = client.post(base + "/summary", json={}, headers=headers)
        assert result.status_code == 409
        current = client.get(base).json()
        assert current['risk']['level'] == 'emergency'
        assert current['summary'] is None


def test_actual_vite_development_origin_can_save_with_csrf(tmp_path):
    app = create_app(tmp_path / "db.sqlite3", ModelService())
    with TestClient(app) as client:
        headers = client_and_headers(client)
        for origin in ("http://127.0.0.1:5174", "http://localhost:5174"):
            result = client.post("/api/visits", json={}, headers={**headers, "Origin": origin})
            assert result.status_code == 201
        assert client.post("/api/visits", json={}, headers={**headers, "Origin": "https://untrusted.invalid"}).status_code == 403


def test_slow_model_cannot_downgrade_new_form_warning(tmp_path):
    async def scenario():
        model = WaitingModel()
        app = create_app(tmp_path / "db.sqlite3", model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                token = (await client.get("/api/bootstrap")).json()["csrf_token"]
                headers = {"X-CSRF-Token": token}
                record = (await client.post("/api/visits", json={}, headers=headers)).json()
                base = f"/api/visits/{record['id']}"
                pending = asyncio.create_task(client.post(base + "/messages", json={"content": "虚构：咳嗽", "use_model": True}, headers=headers))
                await asyncio.wait_for(model.started.wait(), 2)
                updated = await client.patch(base + "/intake", json={"chief_complaint": "虚构：咳嗽", "age_group": "儿童"}, headers=headers)
                assert updated.json()["risk"]["level"] == "prompt"
                model.release.set()
                response = (await pending).json()
                assert response["visit"]["intake"]["age_group"] == "儿童"
                assert response["visit"]["risk"]["level"] == "prompt"
                assert response["assistant_message"]["risk"]["level"] == "prompt"
                assert response["visit"]["summary"] is None
    asyncio.run(scenario())
