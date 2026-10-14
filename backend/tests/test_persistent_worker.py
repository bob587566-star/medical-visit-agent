"""Persistent delivery tests use isolated encrypted records and opaque queue IDs.

Optional real PostgreSQL/Redis acceptance creates its own schema/namespace and
never opens the application's existing visitor records or default Redis stream.
"""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.agent import Executor
from backend.knowledge import Knowledge
from backend.model import ModelService
from backend.privacy import ConsentRequest, NOTICE_VERSION, Privacy
from backend.production_runtime import QueueDelivery, RedisRuntime, RuntimeUnavailable
from backend.schemas import Intake, Risk, Visit
from backend.storage import Store, now
from backend.task_agent import TaskEngine
from backend.task_storage import TaskStore
from backend.tasks_api import PersistentTaskRepository, TaskJob, process_delivery, reconcile_outbox, revision


class QueueDouble:
    """Metadata-only test transport, not evidence that a real Redis ran."""
    def __init__(self):
        self.enqueued, self.acked, self.leases = [], [], {}
        self.unavailable = False

    async def enqueue_task(self, task_id):
        if self.unavailable:
            raise RuntimeUnavailable('fictional queue unavailable')
        assert len(task_id) == 32 and all(ch in '0123456789abcdef' for ch in task_id)
        self.enqueued.append(task_id)
        return str(len(self.enqueued)) + '-0'

    async def claim(self, task_id, **kwargs):
        if task_id in self.leases:
            return None
        token = uuid4().hex
        self.leases[task_id] = token
        return token

    async def release(self, task_id, token):
        if self.leases.get(task_id) == token:
            del self.leases[task_id]
        return True

    async def acknowledge(self, message_id):
        self.acked.append(message_id)

    async def close(self):
        pass


def configure(store, runtime=None):
    tasks = TaskStore(store)
    knowledge = Knowledge(Path(__file__).resolve().parents[2] / 'knowledge' / 'articles.json')
    model = ModelService()
    app = SimpleNamespace(state=SimpleNamespace(store=store, task_store=tasks, privacy=Privacy(store, required=True), queue_runtime=runtime or QueueDouble(), task_engine=TaskEngine(Executor(knowledge, model), model)))
    app.state.persistent_tasks = PersistentTaskRepository(store, tasks)
    return app


def prepare(app, *, external=False, risk=None):
    _, owner, _ = app.state.store.new_session()
    app.state.privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True, external_ai=external))
    visit = Visit(id=uuid4().hex, title='虚构队列验收', created_at=now(), updated_at=now(), intake=Intake(chief_complaint='虚构嗓子疼两天'), risk=risk or Risk(message='虚构无紧急提醒'))
    app.state.store.put(owner, visit)
    return owner, visit


def admit(app, owner, visit, use_model=False):
    job = TaskJob(id=uuid4().hex, visit_id=visit.id, goal='prepare_visit', created_at=now(), updated_at=now(), source_revision=revision(visit))
    app.state.persistent_tasks.admit(owner, job, visit, use_model, consent_required=True)
    return job


@pytest.fixture
def fixture_app(tmp_path):
    app = configure(Store(tmp_path / 'fictional.sqlite3'))
    yield app
    app.state.store.close()


def test_private_snapshot_is_encrypted_and_queue_outbox_contains_only_opaque_id(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    with app.state.store.connect() as conn:
        ciphertext = conn.execute('SELECT payload FROM persistent_task_inputs').fetchone()[0]
        assert '嗓子疼' not in ciphertext and 'snapshot' not in ciphertext
    assert asyncio.run(reconcile_outbox(app)) == 1
    assert app.state.queue_runtime.enqueued == [job.id]
    assert asyncio.run(reconcile_outbox(app)) == 0
    assert asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    saved = app.state.task_store.get(owner, visit.id, job.id)
    assert saved['status'] == 'needs_input' and saved['result']['summary']
    assert app.state.store.get(owner, visit.id).summary is None  # Worker never applies a draft.
    with app.state.store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM persistent_task_inputs').fetchone()[0] == 0


def test_snapshot_and_queued_job_survive_store_reopen_then_execute_once(tmp_path):
    path = tmp_path / 'fictional.sqlite3'
    app = configure(Store(path))
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    app.state.store.close()
    reopened = configure(Store(path))
    original = reopened.state.task_engine.run
    calls = []
    async def spy(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)
    reopened.state.task_engine.run = spy
    try:
        assert asyncio.run(process_delivery(reopened, QueueDelivery('1-0', job.id)))
        assert asyncio.run(process_delivery(reopened, QueueDelivery('2-0', job.id)))
        assert len(calls) == 1
        assert reopened.state.task_store.audit(owner)['event_count'] == 3
    finally:
        reopened.state.store.close()


@pytest.mark.parametrize('delete', ['visit', 'withdraw'])
def test_deleted_or_withdrawn_job_cannot_resurrect_or_invoke_model(fixture_app, delete):
    app = fixture_app
    owner, visit = prepare(app, external=True)
    job = admit(app, owner, visit, True)
    if delete == 'visit':
        app.state.store.delete(owner, visit.id)
    else:
        app.state.privacy.withdraw(owner)
    async def forbidden(*args, **kwargs):
        pytest.fail('deleted records must not execute tools/model')
    app.state.task_engine.run = forbidden
    assert asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    assert app.state.task_store.get(owner, visit.id, job.id) is None
    assert app.state.store.get(owner, visit.id) is None


def test_external_consent_is_checked_again_after_queue_wait(fixture_app):
    app = fixture_app
    owner, visit = prepare(app, external=True)
    job = admit(app, owner, visit, True)
    app.state.privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True, external_ai=False))
    async def forbidden(*args, **kwargs):
        pytest.fail('revoked external consent must not reach model')
    app.state.task_engine.run = forbidden
    assert asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    saved = app.state.task_store.get(owner, visit.id, job.id)
    assert saved['status'] == 'failed' and saved['result'] is None and '同意已撤回' in saved['error']


def test_changed_record_before_execution_is_superseded_without_sending_old_snapshot(fixture_app):
    app = fixture_app
    owner, visit = prepare(app, external=True)
    job = admit(app, owner, visit, True)
    changed = app.state.store.get(owner, visit.id)
    changed.intake.onset = '虚构新记录'
    app.state.store.put(owner, changed)
    async def forbidden(*args, **kwargs):
        pytest.fail('stale snapshot must not execute')
    app.state.task_engine.run = forbidden
    assert asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'superseded'


def test_urgent_task_uses_local_shortcut_without_external_consent(fixture_app):
    app = fixture_app
    owner, visit = prepare(app, risk=Risk(level='emergency', kind='medical', message='虚构立即求助', reasons=['虚构危险表现']))
    job = admit(app, owner, visit, True)
    original = app.state.task_engine.run
    choices = []
    async def spy(*args, **kwargs):
        choices.append(kwargs['use_model'])
        return await original(*args, **kwargs)
    app.state.task_engine.run = spy
    assert asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    assert choices == [False]
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'emergency'


def test_fencing_blocks_old_worker_after_new_attempt_claimed(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    repo = app.state.persistent_tasks
    _, first, snapshot, _ = repo.claim(job.id, 'first-token', consent_required=True)
    _, second, _, _ = repo.claim(job.id, 'second-token', consent_required=True)
    result = asyncio.run(app.state.task_engine.run(snapshot, job.goal, use_model=False))
    result.id = job.id
    assert repo.finish(owner, first, 'first-token', result, consent_required=True)[0] == 'fenced'
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'running'
    assert repo.finish(owner, second, 'second-token', result, consent_required=True)[0] == 'done'
    assert app.state.task_store.audit(owner)['event_count'] == 4


def test_cancelled_attempt_is_persistently_requeued_and_can_restart(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    original = app.state.task_engine.run
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError
    app.state.task_engine.run = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'queued'
    assert not app.state.queue_runtime.acked
    app.state.task_engine.run = original
    assert asyncio.run(process_delivery(app, QueueDelivery('2-0', job.id)))


def test_input_tampering_rejected_and_record_not_changed(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    with app.state.store.connect() as conn:
        conn.execute('UPDATE persistent_task_inputs SET payload=? WHERE task_id=?', ('tampered-input', job.id))
    assert asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    saved = app.state.task_store.get(owner, visit.id, job.id)
    assert saved['status'] == 'failed' and '完整性' in saved['error']
    assert app.state.store.get(owner, visit.id).intake == visit.intake


def test_outbox_recovers_enqueue_failure_without_losing_saved_job(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    app.state.queue_runtime.unavailable = True
    with pytest.raises(RuntimeUnavailable):
        asyncio.run(reconcile_outbox(app))
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'queued'
    app.state.queue_runtime.unavailable = False
    assert asyncio.run(reconcile_outbox(app)) == 1


def test_admission_is_atomic_and_same_visit_cannot_have_two_persistent_tasks(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    admit(app, owner, visit)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        admit(app, owner, visit)
    assert caught.value.status_code == 409
    assert len(app.state.task_store.list(owner, visit.id)) == 1
    assert app.state.task_store.audit(owner)['event_count'] == 1


def test_configured_queue_api_never_spawns_process_local_worker_and_enforces_consent(tmp_path):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    app = create_app(tmp_path / 'api.sqlite3', ModelService(), consent_required=True)
    with TestClient(app) as client:
        app.state.queue_runtime = QueueDouble()
        headers = {'X-CSRF-Token': client.get('/api/bootstrap').json()['csrf_token']}
        consent = {'notice_version': NOTICE_VERSION, 'local_storage': True, 'external_ai': False, 'retention_days': 7}
        assert client.post('/api/privacy/consent', json=consent, headers=headers).status_code == 200
        visit = client.post('/api/visits', json={}, headers=headers).json()
        base = '/api/visits/' + visit['id']
        assert client.post(base + '/tasks', json={'goal': 'prepare_visit', 'use_model': True}, headers=headers).status_code == 403
        queued = client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers)
        assert queued.status_code == 202
        assert app.state.task_workers == {}
        job = queued.json()['task']
        assert job['status'] == 'queued' and app.state.queue_runtime.enqueued == [job['id']]
        assert client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers).status_code == 409
        second = client.post('/api/visits', json={}, headers=headers).json()
        app.state.queue_runtime.unavailable = True
        response = client.post('/api/visits/' + second['id'] + '/tasks', json={'goal': 'prepare_visit'}, headers=headers)
        assert response.status_code == 503 and '任务已保存' in response.json()['detail']
        assert len(client.get('/api/visits/' + second['id'] + '/tasks').json()['items']) == 1


def test_late_danger_overrules_prepared_output_before_commit(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    original = app.state.task_engine.run
    calls = []
    async def late_danger(snapshot, goal, **kwargs):
        calls.append(kwargs['use_model'])
        result = await original(snapshot, goal, **kwargs)
        if len(calls) == 1:
            latest = app.state.store.get(owner, visit.id)
            latest.risk = Risk(level='emergency', kind='medical', message='虚构新出现危险表现')
            app.state.store.put(owner, latest)
        return result
    app.state.task_engine.run = late_danger
    assert asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    saved = app.state.task_store.get(owner, visit.id, job.id)
    assert saved['status'] == 'emergency' and saved['result']['summary'] is None
    assert calls == [False, False]


def test_late_external_revocation_drops_result_without_extra_send(fixture_app):
    app = fixture_app
    owner, visit = prepare(app, external=True)
    job = admit(app, owner, visit, True)
    original = app.state.task_engine.run
    async def local_double(snapshot, goal, **kwargs):
        assert kwargs['use_model'] is True  # Permission was valid at claim.
        result = await original(snapshot, goal, use_model=False)  # No network in test.
        app.state.privacy.grant(owner, ConsentRequest(notice_version=NOTICE_VERSION, local_storage=True, external_ai=False))
        return result
    app.state.task_engine.run = local_double
    assert asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    saved = app.state.task_store.get(owner, visit.id, job.id)
    assert saved['status'] == 'failed' and saved['result'] is None


def test_delete_during_execution_drops_reply_and_task_not_recreated(fixture_app):
    app = fixture_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    original = app.state.task_engine.run
    async def delete_midway(*args, **kwargs):
        result = await original(*args, **kwargs)
        app.state.store.delete(owner, visit.id)
        return result
    app.state.task_engine.run = delete_midway
    assert asyncio.run(process_delivery(app, QueueDelivery('1-0', job.id)))
    assert app.state.task_store.get(owner, visit.id, job.id) is None
    assert app.state.store.get(owner, visit.id) is None


def test_global_persistent_admission_has_finite_backpressure(fixture_app):
    app = fixture_app
    from fastapi import HTTPException
    for _ in range(8):
        owner, visit = prepare(app)
        admit(app, owner, visit)
    owner, visit = prepare(app)
    with pytest.raises(HTTPException) as caught:
        admit(app, owner, visit)
    assert caught.value.status_code == 429
    assert not app.state.task_store.list(owner, visit.id)


@pytest.fixture
def real_app(tmp_path):
    database, redis_url = os.environ.get('MEDICAL_TEST_DATABASE_URL'), os.environ.get('MEDICAL_TEST_REDIS_URL')
    if not database or not redis_url:
        pytest.skip('Dedicated fictional PostgreSQL/Redis test URLs not configured.')
    from sqlalchemy import create_engine, text
    from backend.postgres_storage import PostgreSQLStore
    schema = 'worker_fixture_' + uuid4().hex
    engine = create_engine(database, hide_parameters=True)
    with engine.begin() as conn:
        conn.execute(text('CREATE SCHEMA ' + schema))
    runtime = RedisRuntime.from_url(redis_url, namespace='worker-test-' + uuid4().hex[:20])
    store = PostgreSQLStore(database, data_dir=tmp_path, schema=schema)
    app = configure(store, runtime)
    loop = asyncio.new_event_loop()
    app.worker_test_loop = loop
    yield app
    async def cleanup():
        keys = await runtime.client.keys(runtime.namespace + ':*')
        if keys:
            await runtime.client.delete(*keys)
        await runtime.close()
    loop.run_until_complete(cleanup())
    loop.close()
    store.close()
    with engine.begin() as conn:
        conn.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
    engine.dispose()


def test_real_postgres_redis_encrypted_outbox_worker_delivery_and_duplicate(real_app):
    app = real_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    async def run():
        await app.state.queue_runtime.initialize_queue()
        assert await reconcile_outbox(app) == 1
        raw = await app.state.queue_runtime.client.xrange(app.state.queue_runtime.stream)
        assert raw[0][1] == {'task_id': job.id}
        messages = await app.state.queue_runtime.dequeue('real-worker-one', block_ms=1)
        assert len(messages) == 1
        assert await process_delivery(app, messages[0])
        assert await app.state.queue_runtime.client.xlen(app.state.queue_runtime.stream) == 0
        duplicate_id = await app.state.queue_runtime.enqueue_task(job.id)
        duplicate = (await app.state.queue_runtime.dequeue('real-worker-two', block_ms=1))[0]
        assert duplicate.message_id == duplicate_id
        assert await process_delivery(app, duplicate)
    app.worker_test_loop.run_until_complete(run())
    saved = app.state.task_store.get(owner, visit.id, job.id)
    assert saved['status'] == 'needs_input' and saved['result']['summary']
    assert app.state.task_store.audit(owner)['event_count'] == 3
    assert app.state.store.get(owner, visit.id).summary is None


def test_real_postgres_redis_withdrawal_cascades_private_input_and_prevents_delivery(real_app):
    app = real_app
    owner, visit = prepare(app, external=True)
    job = admit(app, owner, visit, True)
    async def run():
        await app.state.queue_runtime.initialize_queue()
        await reconcile_outbox(app)
        delivery = (await app.state.queue_runtime.dequeue('real-worker-before-delete', block_ms=1))[0]
        app.state.privacy.withdraw(owner)
        assert await process_delivery(app, delivery)
    app.worker_test_loop.run_until_complete(run())
    assert app.state.task_store.get(owner, visit.id, job.id) is None
    with app.state.store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM persistent_task_inputs').fetchone()[0] == 0


def test_real_pending_stream_reclaim_reexecutes_after_simulated_dead_consumer(real_app):
    app = real_app
    owner, visit = prepare(app)
    job = admit(app, owner, visit)
    runtime = app.state.queue_runtime
    async def run():
        await runtime.initialize_queue()
        await reconcile_outbox(app)
        delivery = (await runtime.dequeue('dead-consumer', block_ms=1))[0]
        lease = await runtime.claim(job.id)
        app.state.persistent_tasks.claim(job.id, lease, consent_required=True)
        # Prefer advancing only this fixture's pending-message idle clock. Some
        # managed Redis services omit XCLAIM's IDLE option; only that explicit
        # capability error falls back to real elapsed idle time, keeping the
        # product's 120-second reclaim threshold unchanged.
        from redis.exceptions import ResponseError
        try:
            await runtime.client.xclaim(runtime.stream, runtime.group, 'dead-consumer', 0, [delivery.message_id], idle=121_000)
        except ResponseError as error:
            unsupported = 'IDLE,TIME,RETRYCOUNT,FORCE,LASTID options are not implemented'
            if str(error).strip().removeprefix('ERR ') != unsupported:
                raise
            await asyncio.sleep(121)
        await runtime.release(job.id, lease)
        _, reclaimed = await runtime.recover('replacement-consumer')
        assert len(reclaimed) == 1 and reclaimed[0].task_id == job.id
        assert await process_delivery(app, reclaimed[0])
    app.worker_test_loop.run_until_complete(run())
    assert app.state.task_store.get(owner, visit.id, job.id)['status'] == 'needs_input'
    assert app.state.task_store.audit(owner)['event_count'] == 4
