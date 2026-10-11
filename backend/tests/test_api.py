import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.model import ModelService
from backend.schemas import ExtractedDetails


def test_session_csrf_and_owner(tmp_path):
    app = create_app(tmp_path / "test.sqlite3", ModelService())
    with TestClient(app) as alice, TestClient(app) as bob:
        a = alice.get("/api/bootstrap")
        token = a.json()["csrf_token"]
        assert "HttpOnly" in a.headers["set-cookie"]
        assert "SameSite=strict" in a.headers["set-cookie"]
        assert a.headers["cache-control"] == "no-store"
        assert alice.post("/api/visits", json={}).status_code == 403
        created = alice.post("/api/visits", json={}, headers={"X-CSRF-Token": token}).json()
        bob.get("/api/bootstrap")
        assert bob.get(f"/api/visits/{created['id']}").status_code == 404
        assert alice.post("/api/visits", json={}, headers={"X-CSRF-Token": token, "Origin": "https://evil.test"}).status_code == 403


def test_record_retention_and_validation_redaction(tmp_path):
    app = create_app(tmp_path / "test.sqlite3", ModelService())
    with TestClient(app) as client:
        boot = client.get("/api/bootstrap").json()
        headers = {"X-CSRF-Token": boot["csrf_token"]}
        error = client.post("/api/visits", json={"title": "", "secret": "不能回显的虚构资料"}, headers=headers)
        assert error.status_code == 422
        assert "不能回显" not in error.text
        assert client.post("/api/visits", content=b"x" * 65537, headers=headers).status_code == 413
        visit = client.post("/api/visits", json={}, headers=headers).json()
        store = app.state.store
        cutoff = datetime.now(timezone.utc) - timedelta(days=8)
        with store.connect() as conn:
            conn.execute("UPDATE visits SET updated_at=? WHERE id=?", (cutoff.isoformat(), visit["id"]))
        store.expire((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
        assert client.get(f"/api/visits/{visit['id']}").status_code == 404


class WaitingModel:
    available = True

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(self, payload):
        self.started.set()
        await self.release.wait()
        return ExtractedDetails(symptoms=["咳嗽"])


def test_slow_model_does_not_block_emergency_or_resurrect_deleted_visit(tmp_path):
    async def scenario():
        model = WaitingModel()
        app = create_app(tmp_path / "test.sqlite3", model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                token = (await client.get("/api/bootstrap")).json()["csrf_token"]
                headers = {"X-CSRF-Token": token}
                record = (await client.post("/api/visits", json={}, headers=headers)).json()
                message_url = f"/api/visits/{record['id']}/messages"
                pending = asyncio.create_task(client.post(message_url, json={"content": "虚构病例：咳嗽两天", "use_model": True}, headers=headers))
                await asyncio.wait_for(model.started.wait(), 2)
                urgent = await asyncio.wait_for(client.post(message_url, json={"content": "现在胸口疼，还冒冷汗，怎么办？", "use_model": True}, headers=headers), 2)
                assert urgent.status_code == 200
                assert urgent.json()["assistant_message"]["risk"]["level"] == "emergency"
                assert (await client.delete(f"/api/visits/{record['id']}", headers=headers)).status_code == 204
                model.release.set()
                assert (await pending).status_code == 404
                assert (await client.get("/api/bootstrap")).json()["visits"] == []
    asyncio.run(scenario())


def test_intake_changed_during_model_request_cannot_restore_stale_summary(tmp_path):
    async def scenario():
        model = WaitingModel()
        app = create_app(tmp_path / "test.sqlite3", model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                token = (await client.get("/api/bootstrap")).json()["csrf_token"]
                headers = {"X-CSRF-Token": token}
                record = (await client.post("/api/visits", json={}, headers=headers)).json()
                base = f"/api/visits/{record['id']}"
                pending = asyncio.create_task(client.post(base + "/messages", json={"content": "虚构：咳嗽", "use_model": True}, headers=headers))
                await asyncio.wait_for(model.started.wait(), 2)
                updated = await client.patch(base + "/intake", json={"chief_complaint": "虚构：新补充的资料", "onset": "今天"}, headers=headers)
                assert updated.status_code == 200
                model.release.set()
                response = (await pending).json()
                assert response["visit"]["intake"]["onset"] == "今天"
                assert response["visit"]["summary"] is None
                assert len(response["visit"]["messages"]) == 2
    asyncio.run(scenario())
