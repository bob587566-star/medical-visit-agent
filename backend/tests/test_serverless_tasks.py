"""Fictional SQL fixtures + a controllable Redis double; no cloud/model calls."""
import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.production_runtime import RuntimeUnavailable
from backend.serverless_tasks import DELIVERY_LUA, FORGET_LUA, delivery_key, run_owned_task
from backend.storage import Store
from backend.tasks_api import register_task_routes
from backend.tests.test_persistent_worker import QueueDouble, admit, configure, prepare


class RedisClientDouble:
    def __init__(self, runtime):
        self.runtime, self.references = runtime, {}
        self.fail_forget = False

    async def get(self, key):
        return self.references.get(key)

    async def mget(self, keys):
        return [self.references.get(key) for key in keys]

    async def eval(self, script, count, *arguments):
        if script == DELIVERY_LUA:
            assert count == 2
            stream, key, task_id = arguments
            assert stream == self.runtime.stream
            if key not in self.references:
                self.references[key] = await self.runtime.enqueue_task(task_id)
            return self.references[key]
        assert script == FORGET_LUA and count == 1
        if self.fail_forget:
            raise RuntimeError('fictional cleanup interruption')
        key, message_id = arguments
        if self.references.get(key) == message_id:
            del self.references[key]
            return 1
        return 0


class RequestRuntimeDouble(QueueDouble):
    def __init__(self):
        super().__init__()
        self.namespace = 'fictional-serverless'
        self.stream = self.namespace + ':job-references'
        self.client = RedisClientDouble(self)
        self.messages, self.claims = {}, []
        self.fail_ack = False

    async def enqueue_task(self, task_id):
        message_id = await super().enqueue_task(task_id)
        self.messages[message_id] = {'task_id': task_id}
        return message_id

    async def claim(self, task_id, **kwargs):
        self.claims.append(task_id)
        if self.unavailable:
            raise RuntimeUnavailable('fictional queue unavailable')
        return await super().claim(task_id, **kwargs)

    async def acknowledge(self, message_id):
        if self.fail_ack:
            raise RuntimeUnavailable('fictional ACK interruption')
        await super().acknowledge(message_id)
        self.messages.pop(message_id, None)


def api_app(configured, *, vercel=True):
    app = FastAPI()
    app.state = configured.state
    app.state.vercel = vercel
    app.state.commit_lock = asyncio.Lock()
    app.state.task_workers = {}
    app.state.task_slots = asyncio.Semaphore(2)

    def owned(owner, visit_id):
        visit = app.state.store.get(owner, visit_id)
        if visit is None:
            raise HTTPException(404, '未找到这条记录。')
        return visit

    async def identity(x_test_owner: str = Header()):
        return x_test_owner, 'fictional-csrf'

    async def mutation(x_test_owner: str = Header(), x_test_csrf: str | None = Header(default=None)):
        if x_test_csrf != 'fictional-csrf':
            raise HTTPException(403, 'fictional CSRF gate')
        return x_test_owner

    @app.exception_handler(RuntimeUnavailable)
    async def runtime_error(request, error):
        return JSONResponse({'detail': '持久服务暂不可用。'}, status_code=503)

    register_task_routes(app, owned, identity, mutation, lambda owner: None, lambda visit: None)
    return app


@pytest.fixture
def fixture_app(tmp_path):
    app = configure(Store(tmp_path / 'fictional-request.sqlite3'), RequestRuntimeDouble())
    app.state.vercel = True
    yield app
    app.state.store.close()


def headers(owner, *, mutation=False):
    result = {'X-Test-Owner': owner}
    if mutation:
        result['X-Test-Csrf'] = 'fictional-csrf'
    return result


def assert_drained(app):
    runtime = app.state.queue_runtime
    assert not runtime.leases and not runtime.messages and not runtime.client.references
    with app.state.store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM persistent_task_inputs').fetchone()[0] == 0


def test_create_awaits_owned_job_and_returns_draft_without_detached_worker(fixture_app):
    configured = fixture_app
    owner, visit = prepare(configured)
    app = api_app(configured)
    original, calls = app.state.task_engine.run, []

    async def spy(*args, **kwargs):
        assert not app.state.commit_lock.locked()
        calls.append(args[0].id)
        return await original(*args, **kwargs)

    app.state.task_engine.run = spy
    with TestClient(app) as client:
        base = '/api/visits/' + visit.id + '/tasks'
        assert client.post(base, json={'goal': 'prepare_visit'}, headers=headers(owner)).status_code == 403
        response = client.post(base, json={'goal': 'prepare_visit'}, headers=headers(owner, mutation=True))
        assert response.status_code == 202
        job = response.json()['task']
        assert job['status'] == 'needs_input' and job['result']['summary'] and job['can_apply']
        assert client.get(base + '/' + job['id'], headers=headers(owner)).json()['result'] == job['result']
    assert calls == [visit.id] and not app.state.task_workers
    assert app.state.store.get(owner, visit.id).summary is None
    assert_drained(app)


@pytest.mark.parametrize('poll', ['list', 'detail'])
def test_owner_poll_executes_persisted_outbox_without_external_worker(fixture_app, poll):
    owner, visit = prepare(fixture_app)
    job = admit(fixture_app, owner, visit)
    app = api_app(fixture_app)
    url = '/api/visits/' + visit.id + '/tasks' + ('/' + job.id if poll == 'detail' else '')
    with TestClient(app) as client:
        response = client.get(url, headers=headers(owner))
        assert response.status_code == 200
        saved = response.json() if poll == 'detail' else response.json()['items'][0]
        assert saved['status'] == 'needs_input'
    assert app.state.queue_runtime.enqueued == [job.id]
    assert_drained(app)


def test_foreign_visit_or_task_never_touches_queue_or_engine(fixture_app):
    owner, visit = prepare(fixture_app)
    job = admit(fixture_app, owner, visit)
    stranger, other_visit = prepare(fixture_app)
    app = api_app(fixture_app)
    with TestClient(app) as client:
        paths = ['/api/visits/' + visit.id + '/tasks', '/api/visits/' + visit.id + '/tasks/' + job.id,
                 '/api/visits/' + other_visit.id + '/tasks/' + job.id]
        for path in paths:
            assert client.get(path, headers=headers(stranger)).status_code == 404
        assert client.get('/api/internal/tasks/' + job.id, headers=headers(stranger)).status_code == 404
    assert not app.state.queue_runtime.claims and not app.state.queue_runtime.enqueued


def test_nonvercel_persistent_api_still_only_enqueues_for_separate_worker(fixture_app):
    owner, visit = prepare(fixture_app)
    app = api_app(fixture_app, vercel=False)

    async def forbidden(*args, **kwargs):
        pytest.fail('non-Vercel persistent API must retain external-worker behavior')

    app.state.task_engine.run = forbidden
    base = '/api/visits/' + visit.id + '/tasks'
    with TestClient(app) as client:
        response = client.post(base, json={'goal': 'prepare_visit'}, headers=headers(owner, mutation=True))
        job = response.json()['task']
        assert response.status_code == 202 and job['status'] == 'queued'
        assert client.get(base, headers=headers(owner)).json()['items'][0]['status'] == 'queued'
        assert client.get(base + '/' + job['id'], headers=headers(owner)).json()['status'] == 'queued'
    assert app.state.queue_runtime.enqueued == [job['id']] and not app.state.queue_runtime.claims


def test_cancelled_request_reopens_and_reuses_one_delivery(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    app.state.task_engine.run = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_owned_task(app, owner, visit.id, job.id))
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'queued'
    runtime = app.state.queue_runtime
    assert len(runtime.messages) == 1 and not runtime.leases
    reopened = configure(Store(Path(app.state.store.path)), runtime)
    try:
        assert asyncio.run(run_owned_task(reopened, owner, visit.id, job.id))
        assert runtime.enqueued == [job.id] and runtime.acked == ['1-0']
        assert_drained(reopened)
    finally:
        reopened.state.store.close()


def test_outer_request_budget_requeues_then_owner_retry_finishes(fixture_app, monkeypatch):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    original = app.state.task_engine.run

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    app.state.task_engine.run = blocked
    monkeypatch.setattr('backend.serverless_tasks.REQUEST_DELIVERY_TIMEOUT', .025)
    assert not asyncio.run(run_owned_task(app, owner, visit.id, job.id))
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'queued'
    assert not app.state.queue_runtime.leases and len(app.state.queue_runtime.messages) == 1
    app.state.task_engine.run = original
    monkeypatch.setattr('backend.serverless_tasks.REQUEST_DELIVERY_TIMEOUT', 90)
    assert asyncio.run(run_owned_task(app, owner, visit.id, job.id))
    assert app.state.queue_runtime.enqueued == [job.id]
    assert_drained(app)


def test_abrupt_dead_request_resumes_running_snapshot_after_lease_expiry(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    runtime = app.state.queue_runtime

    async def abandoned():
        token = await runtime.claim(job.id)
        app.state.persistent_tasks.claim(job.id, token, consent_required=True)
        await runtime.client.eval(DELIVERY_LUA, 2, runtime.stream, delivery_key(runtime, job.id), job.id)

    asyncio.run(abandoned())
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'running'
    # Only the fictional lease expires; no real time/server/Redis is changed.
    runtime.leases.pop(job.id)
    assert asyncio.run(run_owned_task(app, owner, visit.id, job.id))
    assert runtime.enqueued == [job.id]
    assert app.state.task_store.audit(owner)['event_count'] == 4
    assert_drained(app)


def test_live_lease_poll_does_not_enqueue_or_execute_again(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    asyncio.run(app.state.queue_runtime.claim(job.id))
    assert not asyncio.run(run_owned_task(app, owner, visit.id, job.id))
    assert not app.state.queue_runtime.enqueued
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'queued'


@pytest.mark.parametrize('failure', ['ack', 'forget'])
def test_committed_delivery_cleanup_retry_never_reexecutes(fixture_app, failure):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    runtime = app.state.queue_runtime
    runtime.fail_ack = failure == 'ack'
    runtime.client.fail_forget = failure == 'forget'
    with pytest.raises(RuntimeUnavailable):
        asyncio.run(run_owned_task(app, owner, visit.id, job.id))
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'needs_input'
    assert runtime.client.references

    async def forbidden(*args, **kwargs):
        pytest.fail('fenced committed result must not execute again during ACK cleanup')

    app.state.task_engine.run = forbidden
    runtime.fail_ack = runtime.client.fail_forget = False
    with TestClient(api_app(app)) as client:
        response = client.get('/api/visits/' + visit.id + '/tasks', headers=headers(owner))
        assert response.status_code == 200 and response.json()['items'][0]['status'] == 'needs_input'
    assert runtime.enqueued == [job.id]
    assert_drained(app)


def test_saved_create_survives_queue_outage_and_owner_list_recovers(fixture_app):
    owner, visit = prepare(fixture_app)
    app = api_app(fixture_app)
    app.state.queue_runtime.unavailable = True
    base = '/api/visits/' + visit.id + '/tasks'
    with TestClient(app) as client:
        failed = client.post(base, json={'goal': 'prepare_visit'}, headers=headers(owner, mutation=True))
        assert failed.status_code == 503 and '任务已保存' in failed.json()['detail']
        jobs = app.state.task_store.list(owner, visit.id)
        assert len(jobs) == 1 and jobs[0]['status'] == 'queued'
        assert client.post(base, json={'goal': 'prepare_visit'}, headers=headers(owner, mutation=True)).status_code == 409
        app.state.queue_runtime.unavailable = False
        assert client.get(base, headers=headers(owner)).json()['items'][0]['status'] == 'needs_input'
    assert_drained(app)


@pytest.mark.parametrize('invalidate', ['revision', 'external_consent'])
def test_owner_retry_rechecks_snapshot_and_external_permission_before_model(fixture_app, invalidate):
    from backend.privacy import ConsentRequest, NOTICE_VERSION

    app = fixture_app
    owner, visit = prepare(app, external=True)
    job = admit(app, owner, visit, use_model=True)
    if invalidate == 'revision':
        latest = app.state.store.get(owner, visit.id)
        latest.intake.onset = 'fictional updated onset'
        app.state.store.put(owner, latest)
    else:
        app.state.privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True, external_ai=False))

    async def forbidden(*args, **kwargs):
        pytest.fail('stale or revoked snapshot must not invoke the engine/model')

    app.state.task_engine.run = forbidden
    assert asyncio.run(run_owned_task(app, owner, visit.id, job.id))
    saved = app.state.task_store.get(owner, visit.id, job.id)
    assert saved['status'] == ('superseded' if invalidate == 'revision' else 'failed')
    assert saved['result'] is None
    assert_drained(app)


def test_delete_during_owned_execution_drops_result_and_acknowledges(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    original = app.state.task_engine.run

    async def deleted(*args, **kwargs):
        result = await original(*args, **kwargs)
        app.state.store.delete(owner, visit.id)
        return result

    app.state.task_engine.run = deleted
    assert asyncio.run(run_owned_task(app, owner, visit.id, job.id))
    assert app.state.task_store.get(owner, visit.id, job.id) is None
    assert app.state.store.get(owner, visit.id) is None
    assert_drained(app)


def test_vercel_task_never_falls_back_to_background_sqlite(fixture_app):
    owner, visit = prepare(fixture_app)
    app = api_app(fixture_app)
    app.state.queue_runtime = None
    with TestClient(app) as client:
        response = client.post('/api/visits/' + visit.id + '/tasks', json={'goal': 'prepare_visit'}, headers=headers(owner, mutation=True))
        assert response.status_code == 503
    assert not app.state.task_workers and not app.state.task_store.list(owner, visit.id)


def test_redis_request_delivery_contains_only_opaque_ids(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    runtime = app.state.queue_runtime
    runtime.fail_ack = True
    with pytest.raises(RuntimeUnavailable):
        asyncio.run(run_owned_task(app, owner, visit.id, job.id))
    assert runtime.messages == {'1-0': {'task_id': job.id}}
    assert runtime.client.references == {delivery_key(runtime, job.id): '1-0'}
    assert '嗓子疼' not in str(runtime.messages) + str(runtime.client.references)
