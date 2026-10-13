"""Independent consent-race fixtures; mocked transports never use the network."""
import asyncio

import httpx
import pytest

from backend.main import create_app
from backend.model import ModelService
from backend.privacy import NOTICE_VERSION


@pytest.mark.parametrize('phase', ['before_dispatch', 'already_in_flight'])
def test_live_external_revocation_blocks_unsent_request_or_discards_sent_result(tmp_path, phase):
    class BlockingRetriever:
        def __init__(self, knowledge):
            self.knowledge = knowledge
            self.started, self.release = asyncio.Event(), asyncio.Event()
        async def search(self, query):
            self.started.set()
            if phase == 'before_dispatch':
                await self.release.wait()
            return self.knowledge.search(query)
    async def scenario():
        calls, sent, release_cloud = [], asyncio.Event(), asyncio.Event()
        async def cloud(request):
            assert request.url.host == 'api.deepseek.com'
            calls.append('mocked_dispatch')
            sent.set()
            if phase == 'already_in_flight':
                await release_cloud.wait()
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"symptoms":["咳嗽"],"onset":"","severity":""}'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(cloud), trust_env=False) as cloud_client:
            model = ModelService('fictional-test-key-never-used-on-network', client=cloud_client)
            app = create_app(tmp_path / 'fictional.sqlite3', model, consent_required=True)
            async with app.router.lifespan_context(app):
                blocker = BlockingRetriever(app.state.knowledge)
                app.state.executor.retriever = blocker
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                    headers = {'X-CSRF-Token': (await client.get('/api/bootstrap')).json()['csrf_token']}
                    initial = {'notice_version': NOTICE_VERSION, 'local_storage': True, 'external_ai': True, 'retention_days': 7}
                    assert (await client.post('/api/privacy/consent', json=initial, headers=headers)).status_code == 200
                    visit = (await client.post('/api/visits', json={}, headers=headers)).json()
                    message = asyncio.create_task(client.post('/api/visits/' + visit['id'] + '/messages',
                        json={'content': '我咳嗽三天了', 'use_model': True}, headers=headers))
                    await asyncio.wait_for(blocker.started.wait() if phase == 'before_dispatch' else sent.wait(), timeout=3)
                    changed = await client.post('/api/privacy/consent', json={**initial, 'external_ai': False}, headers=headers)
                    assert changed.status_code == 200
                    blocker.release.set()
                    release_cloud.set()
                    response = await message
                    assert len(calls) == (0 if phase == 'before_dispatch' else 1)
                    assert response.status_code == 403
                    assert (await client.get('/api/visits/' + visit['id'])).json()['messages'] == []
    asyncio.run(scenario())


def test_queued_local_creation_rechecks_consent_after_prior_withdrawal(tmp_path):
    class ObservableLock:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.entered = [asyncio.Event(), asyncio.Event()]
            self.count = 0
        async def __aenter__(self):
            index = self.count
            self.count += 1
            if index < len(self.entered):
                self.entered[index].set()
            await self.lock.acquire()
            return self
        async def __aexit__(self, *args):
            self.lock.release()
    async def scenario():
        app = create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                headers = {'X-CSRF-Token': (await client.get('/api/bootstrap')).json()['csrf_token']}
                grant = {'notice_version': NOTICE_VERSION, 'local_storage': True, 'external_ai': False, 'retention_days': 7}
                assert (await client.post('/api/privacy/consent', json=grant, headers=headers)).status_code == 200
                gate = ObservableLock()
                app.state.commit_lock = gate
                await gate.lock.acquire()
                withdrawal = asyncio.create_task(client.delete('/api/privacy/consent', headers=headers))
                await asyncio.wait_for(gate.entered[0].wait(), timeout=3)
                creation = asyncio.create_task(client.post('/api/visits', json={'title': '虚构迟到创建'}, headers=headers))
                await asyncio.wait_for(gate.entered[1].wait(), timeout=3)
                gate.lock.release()
                assert (await withdrawal).status_code == 200
                assert (await creation).status_code == 403
                assert (await client.get('/api/privacy/export')).json()['visits'] == []
    asyncio.run(scenario())


def test_non_ascii_csrf_header_is_rejected_without_server_error(tmp_path):
    async def scenario():
        app = create_app(tmp_path / 'fictional.sqlite3', ModelService(), consent_required=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                await client.get('/api/bootstrap')
                response = await client.post('/api/privacy/consent',
                    headers=[(b'X-CSRF-Token', b'\xff-not-a-token')],
                    json={'notice_version': NOTICE_VERSION, 'local_storage': True})
                assert response.status_code == 403
                assert response.headers['x-content-type-options'] == 'nosniff'
    asyncio.run(scenario())
