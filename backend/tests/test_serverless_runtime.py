"""Optional real PostgreSQL/Redis checks for awaited serverless requests.

Only dedicated MEDICAL_TEST_* URLs are used. Every test creates its own SQL
schema and Redis namespace containing fictional data and opaque queue IDs.
The ASGI fixture tests task HTTP handlers; it does not start main.py, open .env,
invoke a cloud model, or claim to verify the deployed Vercel transport itself.
"""
import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from backend.production_runtime import RedisRuntime, RuntimeUnavailable
from backend.serverless_tasks import delivery_key
from backend.tests.test_persistent_worker import admit, configure, prepare
from backend.tests.test_serverless_tasks import api_app, headers


@pytest.fixture
def real_serverless(tmp_path):
    database = os.environ.get('MEDICAL_TEST_DATABASE_URL')
    redis_url = os.environ.get('MEDICAL_TEST_REDIS_URL')
    if not database or not redis_url:
        pytest.skip('Dedicated fictional PostgreSQL/Redis test URLs not configured.')
    from sqlalchemy import create_engine, text
    from backend.postgres_storage import PostgreSQLStore

    schema = 'serverless_fixture_' + uuid4().hex
    namespace = 'serverless-test-' + uuid4().hex[:20]
    engine = create_engine(database, hide_parameters=True)
    runtime = RedisRuntime.from_url(redis_url, namespace=namespace)
    store = None
    loop = asyncio.new_event_loop()
    created = False
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE SCHEMA ' + schema))
        created = True
        store = PostgreSQLStore(database, data_dir=tmp_path, schema=schema)
        configured = configure(store, runtime)
        configured.state.vercel = True
        app = api_app(configured)
        loop.run_until_complete(runtime.initialize_queue())
        yield SimpleNamespace(app=app, loop=loop, runtime=runtime)
    finally:
        async def cleanup():
            try:
                # Match only this test's generated namespace. Never flush DB or
                # inspect/delete the application's default stream or records.
                keys = [key async for key in runtime.client.scan_iter(match=namespace + ':*')]
                if keys:
                    await runtime.client.delete(*keys)
            finally:
                await runtime.close()

        try:
            loop.run_until_complete(cleanup())
        finally:
            loop.close()
            if store is not None:
                store.close()
            try:
                if created:
                    with engine.begin() as conn:
                        conn.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
            finally:
                engine.dispose()


async def assert_real_delivery_drained(fixture, owner, visit_id, task_id):
    runtime, app = fixture.runtime, fixture.app
    assert await runtime.client.xlen(runtime.stream) == 0
    assert await runtime.client.get(delivery_key(runtime, task_id)) is None
    assert await runtime.client.get(runtime.namespace + ':lease:' + task_id) is None
    assert (await runtime.client.xpending(runtime.stream, runtime.group))['pending'] == 0
    with app.state.store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM persistent_task_inputs WHERE owner=? AND visit_id=?', (owner, visit_id)).fetchone()[0] == 0
    assert not app.state.task_workers


def test_real_serverless_post_completes_before_response_and_cleans_mapping(real_serverless):
    fixture = real_serverless
    owner, visit = prepare(fixture.app)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url='http://fictional.test') as client:
            response = await client.post('/api/visits/' + visit.id + '/tasks', json={'goal': 'prepare_visit'}, headers=headers(owner, mutation=True))
        assert response.status_code == 202
        job = response.json()['task']
        assert job['status'] == 'needs_input' and job['result']['summary'] and job['can_apply']
        saved = fixture.app.state.task_store.get(owner, visit.id, job['id'])
        assert saved['result'] == job['result'] and saved['status'] == job['status']
        assert fixture.app.state.store.get(owner, visit.id).summary is None
        assert fixture.app.state.task_store.audit(owner)['event_count'] == 3
        await assert_real_delivery_drained(fixture, owner, visit.id, job['id'])

    fixture.loop.run_until_complete(scenario())


def test_real_serverless_competing_owner_requests_share_lease_and_run_once(real_serverless):
    fixture = real_serverless
    owner, visit = prepare(fixture.app)
    job = admit(fixture.app, owner, visit)
    original = fixture.app.state.task_engine.run
    calls = []

    async def scenario():
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def controlled(snapshot, goal, **kwargs):
            calls.append(snapshot.id)
            entered.set()
            await proceed.wait()
            return await original(snapshot, goal, **kwargs)

        fixture.app.state.task_engine.run = controlled
        url = '/api/visits/' + visit.id + '/tasks/' + job.id
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url='http://fictional.test') as client:
            first = asyncio.create_task(client.get(url, headers=headers(owner)))
            try:
                await asyncio.wait_for(entered.wait(), timeout=10)
                token = await fixture.runtime.client.get(fixture.runtime.namespace + ':lease:' + job.id)
                message_id = await fixture.runtime.client.get(delivery_key(fixture.runtime, job.id))
                assert token and len(token) == 32 and message_id
                entries = await fixture.runtime.client.xrange(fixture.runtime.stream)
                assert entries == [(message_id, {'task_id': job.id})]
                with fixture.app.state.store.connect() as conn:
                    row = conn.execute('SELECT state,claim_token,attempts,payload FROM persistent_task_inputs WHERE task_id=?', (job.id,)).fetchone()
                assert row[:3] == ('running', token, 1)
                assert '嗓子疼' not in row[3] and 'snapshot' not in row[3]
                contender = await client.get(url, headers=headers(owner))
                assert contender.status_code == 200 and contender.json()['status'] == 'running'
                assert calls == [visit.id]
                assert await fixture.runtime.client.get(delivery_key(fixture.runtime, job.id)) == message_id
                assert await fixture.runtime.client.xlen(fixture.runtime.stream) == 1
            finally:
                proceed.set()
                completed = await first
            assert completed.status_code == 200 and completed.json()['status'] == 'needs_input'
            # Owner polling a committed job remains cleanup/read only.
            repeated = await client.get(url, headers=headers(owner))
            assert repeated.status_code == 200 and repeated.json()['result'] == completed.json()['result']
        assert calls == [visit.id]
        assert fixture.app.state.task_store.audit(owner)['event_count'] == 3
        await assert_real_delivery_drained(fixture, owner, visit.id, job.id)

    fixture.loop.run_until_complete(scenario())


def test_real_serverless_committed_ack_retry_cleans_without_second_engine_run(real_serverless, monkeypatch):
    fixture = real_serverless
    owner, visit = prepare(fixture.app)
    original_run, original_ack = fixture.app.state.task_engine.run, fixture.runtime.acknowledge
    engine_calls, ack_calls = [], []

    async def spy(*args, **kwargs):
        engine_calls.append(args[0].id)
        return await original_run(*args, **kwargs)

    async def fail_first_ack(message_id):
        ack_calls.append(message_id)
        if len(ack_calls) == 1:
            raise RuntimeUnavailable('Fictional interruption after the real SQL commit.')
        await original_ack(message_id)

    monkeypatch.setattr(fixture.app.state.task_engine, 'run', spy)
    monkeypatch.setattr(fixture.runtime, 'acknowledge', fail_first_ack)

    async def scenario():
        base = '/api/visits/' + visit.id + '/tasks'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url='http://fictional.test') as client:
            failed = await client.post(base, json={'goal': 'prepare_visit'}, headers=headers(owner, mutation=True))
            assert failed.status_code == 503 and '任务已保存' in failed.json()['detail']
            saved = fixture.app.state.task_store.list(owner, visit.id)[0]
            assert saved['status'] == 'needs_input' and saved['result']['summary']
            task_id = saved['id']
            message_id = await fixture.runtime.client.get(delivery_key(fixture.runtime, task_id))
            assert message_id == ack_calls[0]
            assert await fixture.runtime.client.xrange(fixture.runtime.stream) == [(message_id, {'task_id': task_id})]
            with fixture.app.state.store.connect() as conn:
                assert conn.execute('SELECT COUNT(*) FROM persistent_task_inputs WHERE task_id=?', (task_id,)).fetchone()[0] == 0
            # List polling exercises the real MGET cleanup branch after commit.
            retry = await client.get(base, headers=headers(owner))
            assert retry.status_code == 200 and retry.json()['items'][0]['result'] == saved['result']
            repeated = await client.get(base + '/' + task_id, headers=headers(owner))
            assert repeated.status_code == 200 and repeated.json()['result'] == saved['result']
        assert engine_calls == [visit.id] and ack_calls == [message_id, message_id]
        assert fixture.app.state.task_store.audit(owner)['event_count'] == 3
        await assert_real_delivery_drained(fixture, owner, visit.id, task_id)

    fixture.loop.run_until_complete(scenario())


def test_real_serverless_cancelled_request_reuses_persisted_delivery_on_owner_poll(real_serverless, monkeypatch):
    fixture = real_serverless
    owner, visit = prepare(fixture.app)
    job = admit(fixture.app, owner, visit)
    original = fixture.app.state.task_engine.run

    async def scenario():
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(fixture.app.state.task_engine, 'run', blocked)
        url = '/api/visits/' + visit.id + '/tasks/' + job.id
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture.app), base_url='http://fictional.test') as client:
            interrupted = asyncio.create_task(client.get(url, headers=headers(owner)))
            try:
                await asyncio.wait_for(entered.wait(), timeout=10)
                message_id = await fixture.runtime.client.get(delivery_key(fixture.runtime, job.id))
                assert message_id and await fixture.runtime.client.xlen(fixture.runtime.stream) == 1
            finally:
                interrupted.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await interrupted
            assert fixture.app.state.task_store.get(owner, visit.id, job.id)['status'] == 'queued'
            assert await fixture.runtime.client.get(delivery_key(fixture.runtime, job.id)) == message_id
            assert await fixture.runtime.client.get(fixture.runtime.namespace + ':lease:' + job.id) is None
            delivered = []
            original_ack = fixture.runtime.acknowledge

            async def ack_spy(identifier):
                delivered.append(identifier)
                await original_ack(identifier)

            monkeypatch.setattr(fixture.runtime, 'acknowledge', ack_spy)
            monkeypatch.setattr(fixture.app.state.task_engine, 'run', original)
            resumed = await client.get(url, headers=headers(owner))
            assert resumed.status_code == 200 and resumed.json()['status'] == 'needs_input'
            assert delivered == [message_id]
        assert fixture.app.state.task_store.audit(owner)['event_count'] == 5
        await assert_real_delivery_drained(fixture, owner, visit.id, job.id)

    fixture.loop.run_until_complete(scenario())
