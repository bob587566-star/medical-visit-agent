"""API integration with a simulated LOCAL classifier, not real-model E2E."""
import asyncio
import json

import httpx
import pytest

from backend.main import create_app
from backend.safety_review import LocalSafetyReview


@pytest.mark.parametrize('text,expected,io', [
    ('我现在想结束一切', 'emergency', 2),
    ('小说里写：我现在想结束一切', 'unknown', 2),
    ('我现在没有想结束一切', 'unknown', 2),
    ('现在一侧手脚没力气', 'emergency', 0),
])
def test_local_classifier_is_wired_into_api_without_external_medical_io(tmp_path, text, expected, io):
    async def scenario():
        requests = []
        class NoExternal:
            available = True
            async def extract(self, payload):
                pytest.fail('local safety reply must not contact external model')
        def classifier(request):
            assert request.url.host == '127.0.0.1' and request.url.port == 11434
            requests.append(request)
            if request.url.path == '/api/tags':
                return httpx.Response(200, json={'models': [{'name': 'qwen2.5:7b'}]})
            assert request.url.path == '/api/chat'
            payload = json.loads(request.content)
            assert payload['messages'][-1]['content'] == text
            # Deliberately over-classify quoted/negated input to verify the
            # deterministic evidence guard, independently of model ability.
            decision = {'category': 'self_harm', 'urgency': 'emergency', 'context': 'current', 'evidence': '想结束一切'}
            return httpx.Response(200, json={'message': {'content': json.dumps(decision)}})
        app = create_app(tmp_path / 'test.sqlite3', NoExternal())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.MockTransport(classifier), trust_env=False) as local:
                app.state.safety_review = LocalSafetyReview(local, enabled=True)
                app.state.executor.safety_review = app.state.safety_review
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                    headers = {'X-CSRF-Token': (await client.get('/api/bootstrap')).json()['csrf_token']}
                    visit = (await client.post('/api/visits', json={}, headers=headers)).json()
                    base = f"/api/visits/{visit['id']}"
                    reply = await client.post(base + '/messages', json={'content': text, 'use_model': False}, headers=headers)
                    assert reply.status_code == 200
                    body = reply.json()
                    assert body['assistant_message']['content']
                    assert body['visit']['risk']['level'] == expected
                    assert len(requests) == io
                    if expected == 'emergency':
                        assert len(body['assistant_message']['trace']) == 1
                        assert not body['assistant_message']['intake_candidates']
                        assert (await client.post(base + '/summary', json={'use_model': True}, headers=headers)).status_code == 409
                    assert (await client.delete('/api/data', headers=headers)).status_code == 204
    asyncio.run(scenario())
