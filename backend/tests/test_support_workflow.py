"""Independent workflow probes for indirect distress and summary consistency."""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.tests.demo_factory import create_demo_app as create_app
from backend.model import ModelService
from backend.schemas import ExtractedDetails


class NoExternal:
    available = True

    async def extract(self, payload):
        pytest.fail('support priority must not send any model request')


def prepare(client):
    headers = {'X-CSRF-Token': client.get('/api/bootstrap').json()['csrf_token']}
    visit = client.post('/api/visits', json={}, headers=headers).json()
    return headers, f"/api/visits/{visit['id']}"


@pytest.mark.parametrize('text,level', [('活着没意思', 'prompt'), ('不想再撑下去了', 'prompt'), ('我不想活了，已经想好怎么做了', 'emergency'), ('现在胸口疼，冒冷汗', 'emergency')])
def test_human_support_short_circuits_and_blocks_all_preparation_channels(tmp_path, text, level):
    with TestClient(create_app(tmp_path / 'test.sqlite3', NoExternal())) as client:
        headers, base = prepare(client)
        reply = client.post(base + '/messages', json={'content': text, 'use_model': True}, headers=headers)
        assert reply.status_code == 200
        answer = reply.json()['assistant_message']
        assert answer['risk']['level'] == level
        assert answer['content'] and not answer['followup_field']
        assert not answer['intake_candidates'] and not answer['suggested_questions']
        assert len(answer['trace']) == 1
        before = client.get(base).json()
        assert before['summary'] is None
        assert client.post(base + '/summary', json={'use_model': True}, headers=headers).status_code == 409
        assert client.post(base + '/plan', json={}, headers=headers).status_code == 409
        assert client.get(base).json() == before
        queued = client.post(base + '/tasks', json={'goal': 'prepare_visit', 'use_model': True}, headers=headers)
        assert queued.status_code == 202
        identifier = queued.json()['task']['id']
        for _ in range(100):
            task = client.get(base + '/tasks/' + identifier).json()
            if task['result'] is not None:
                break
            time.sleep(.01)
        assert task['result'] is not None
        expected = 'support_required' if level == 'prompt' else 'emergency'
        assert task['status'] == task['result']['status'] == expected
        assert task['can_apply'] is False
        assert len(task['result']['steps']) == 1
        assert task['result']['summary'] is None
        assert client.post(base + f'/tasks/{identifier}/apply', json={}, headers=headers).status_code == 409
        assert client.get(base).json() == before


@pytest.mark.parametrize('text', ['活着没意思', '我不想活了', '右边手脚突然没力气'])
def test_form_warning_blocks_summary_before_external_io(tmp_path, text):
    with TestClient(create_app(tmp_path / 'test.sqlite3', NoExternal())) as client:
        headers, base = prepare(client)
        assert client.patch(base + '/intake', json={'chief_complaint': text}, headers=headers).status_code == 200
        before = client.get(base).json()
        assert client.post(base + '/summary', json={'use_model': True}, headers=headers).status_code == 409
        assert client.get(base).json() == before


def test_a_new_crisis_cannot_be_overwritten_by_an_inflight_summary(tmp_path):
    async def scenario():
        class WaitingModel:
            available = True
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()
            async def extract(self, payload):
                self.started.set()
                await self.release.wait()
                return ExtractedDetails(symptoms=['咳嗽'])
        model = WaitingModel()
        app = create_app(tmp_path / 'test.sqlite3', model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                headers = {'X-CSRF-Token': (await client.get('/api/bootstrap')).json()['csrf_token']}
                visit = (await client.post('/api/visits', json={}, headers=headers)).json()
                base = f"/api/visits/{visit['id']}"
                await client.patch(base + '/intake', json={'chief_complaint': '虚构病例：咳嗽'}, headers=headers)
                pending = asyncio.create_task(client.post(base + '/summary', json={'use_model': True}, headers=headers))
                await asyncio.wait_for(model.started.wait(), 2)
                reply = await client.post(base + '/messages', json={'content': '活着没意思', 'use_model': False}, headers=headers)
                assert reply.status_code == 200
                model.release.set()
                assert (await pending).status_code == 409
                current = (await client.get(base)).json()
                assert current['summary'] is None and current['risk']['kind'] == 'self_harm'
    asyncio.run(scenario())
