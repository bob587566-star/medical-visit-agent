"""Await owner-scoped persistent deliveries within a serverless request.

SQL remains the encrypted source of truth. Redis stores only an opaque task ID,
its existing lease, and a stream message ID that survives an aborted request.
No request consumes the shared stream or starts a detached worker.
"""
from __future__ import annotations

import asyncio
import re

from fastapi import HTTPException

from .production_runtime import QueueDelivery, RuntimeUnavailable


REQUEST_DELIVERY_TIMEOUT = 90

# Enqueue and retain its reference in one Redis operation. A request dying after
# XADD must not lose the ID and enqueue another message on every owner poll.
DELIVERY_LUA = """
local existing = redis.call('GET', KEYS[2])
if existing then return existing end
local message = redis.call('XADD', KEYS[1], '*', 'task_id', ARGV[1])
redis.call('SET', KEYS[2], message)
return message
"""

FORGET_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def delivery_key(runtime, task_id: str) -> str:
    if not re.fullmatch(r'[a-f0-9]{32}', task_id):
        raise RuntimeUnavailable('Invalid persisted task identifier.')
    return runtime.namespace + ':request-delivery:' + task_id


async def _message(runtime, task_id: str, *, create: bool) -> str | None:
    key = delivery_key(runtime, task_id)
    try:
        value = (await runtime.client.eval(DELIVERY_LUA, 2, runtime.stream, key, task_id)
                 if create else await runtime.client.get(key))
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]+-[0-9]+', value):
            raise ValueError('Invalid delivery reference.')
        return value
    except Exception as error:
        raise RuntimeUnavailable('Request delivery reference is unavailable.') from error


async def _forget(runtime, task_id: str, message_id: str) -> None:
    try:
        await runtime.client.eval(FORGET_LUA, 1, delivery_key(runtime, task_id), message_id)
    except Exception as error:
        raise RuntimeUnavailable('Request delivery reference cleanup failed.') from error


async def _run(app, owner: str, visit_id: str, task_id: str) -> bool:
    from .tasks_api import persistent_repository, process_delivery

    runtime = getattr(app.state, 'queue_runtime', None)
    if runtime is None:
        raise RuntimeUnavailable('Serverless tasks require the persistent runtime.')
    # Check both owned objects before touching Redis. GET routes have already
    # authenticated the session; this second check closes deletion/route races.
    if app.state.store.get(owner, visit_id) is None:
        raise HTTPException(404, '未找到这条记录。')
    job = app.state.task_store.get(owner, visit_id, task_id)
    if not job:
        raise HTTPException(404, '未找到这项任务。')
    token = await runtime.claim(task_id, lease_seconds=120)
    if not token:
        return False  # Another request still owns the same persisted execution.
    processing = False
    try:
        # Re-read after taking the lease: another request may have committed or
        # deleted the task while this request waited for Redis.
        job = app.state.task_store.get(owner, visit_id, task_id)
        if not job:
            message_id = await _message(runtime, task_id, create=False)
            if message_id:
                await runtime.acknowledge(message_id)
                await _forget(runtime, task_id, message_id)
            return True
        active = job['status'] in {'queued', 'running'}
        message_id = await _message(runtime, task_id, create=active)
        if not message_id:
            return True
        if not active:
            # A crash/ACK failure after the fenced SQL commit is cleanup only;
            # completed results must never invoke the engine a second time.
            await runtime.acknowledge(message_id)
            await _forget(runtime, task_id, message_id)
            return True
        repository = persistent_repository(app)
        repository.dispatched(task_id)
        processing = True  # process_delivery takes responsibility for release.
        finished = await process_delivery(app, QueueDelivery(message_id, task_id), lease_token=token)
        if finished:
            await _forget(runtime, task_id, message_id)
        return finished
    finally:
        if not processing:
            await runtime.release(task_id, token)


async def run_owned_task(app, owner: str, visit_id: str, task_id: str) -> bool:
    """Bounded, awaited progress for exactly one authenticated owner's task.

    The inner engine still has its existing 85-second timeout. The outer budget
    also bounds queue calls and safety retries. Cancellation during execution is
    persistently requeued by process_delivery, and Redis references stay intact.
    """
    try:
        async with asyncio.timeout(REQUEST_DELIVERY_TIMEOUT):
            return await _run(app, owner, visit_id, task_id)
    except TimeoutError:
        return False


async def run_owned_visit_tasks(app, owner: str, visit_id: str) -> None:
    """Progress the single active task and clean up committed ACK retries.

    Inspect only Redis references for IDs already returned by the owner's SQL
    query. There is no stream scan, consumer-group read, or cross-owner work.
    One overall budget bounds even a visit with its maximum 50 historical jobs.
    """
    try:
        async with asyncio.timeout(REQUEST_DELIVERY_TIMEOUT):
            jobs = app.state.task_store.list(owner, visit_id)
            for job in jobs:
                if job['status'] in {'queued', 'running'}:
                    await _run(app, owner, visit_id, job['id'])
                    break
            historical = [job for job in jobs if job['status'] not in {'queued', 'running'}]
            if historical:
                runtime = app.state.queue_runtime
                try:
                    references = await runtime.client.mget([delivery_key(runtime, job['id']) for job in historical])
                except Exception as error:
                    raise RuntimeUnavailable('Request delivery reference lookup failed.') from error
                for job, reference in zip(historical, references):
                    if reference is not None:
                        await _run(app, owner, visit_id, job['id'])
    except TimeoutError:
        return
