"""Independent persistent task worker: python -m backend.worker.

It shares the configured database/key material and Redis namespace with the API.
Redis contains only task IDs; execution snapshots remain encrypted in SQL.
Duplicate delivery is expected, and acknowledgment follows a fenced DB commit.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
import json

from .production_runtime import RuntimeUnavailable
from .tasks_api import persistent_repository, process_delivery, reconcile_outbox


async def consume(app, *, consumer: str | None = None, stop: asyncio.Event | None = None):
    runtime = getattr(app.state, 'queue_runtime', None)
    if runtime is None:
        raise RuntimeUnavailable('The worker requires configured persistent Redis runtime.')
    consumer = consumer or 'worker-' + secrets.token_hex(12)
    stop = stop or asyncio.Event()
    await runtime.initialize_queue()
    persistent_repository(app)
    last_reconcile, cursor = -30.0, '0-0'
    while not stop.is_set():
        try:
            if time.monotonic() - last_reconcile >= 30:
                await reconcile_outbox(app)
                last_reconcile = time.monotonic()
            cursor, reclaimed = await runtime.recover(consumer, min_idle_ms=120_000, start_id=cursor)
            deliveries = reclaimed[:2] or await runtime.dequeue(consumer, count=2, block_ms=1000)
            if deliveries:
                outcomes = await asyncio.gather(*(process_delivery(app, delivery) for delivery in deliveries), return_exceptions=True)
                if any(isinstance(outcome, Exception) for outcome in outcomes):
                    print(json.dumps({'event': 'persistent_worker_delivery_retry', 'count': sum(isinstance(outcome, Exception) for outcome in outcomes)}), flush=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not print driver errors: they can contain DB credentials or input.
            # A queue failure keeps pending/outbox state; no in-memory substitute.
            print(json.dumps({'event': 'persistent_worker_runtime_retry'}), flush=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass


async def run():
    os.environ['WORKER_MODE'] = 'true'
    from pathlib import Path
    from .model import read_project_env
    config = read_project_env(Path(__file__).resolve().parents[1] / '.env')
    if not all(os.environ.get(key) or config.get(key) for key in ('DATABASE_URL', 'REDIS_URL')):
        raise RuntimeUnavailable('Configure DATABASE_URL and REDIS_URL before starting the worker; local records were not opened.')
    from .main import create_app
    app = create_app()
    async with app.router.lifespan_context(app):
        await consume(app)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit('Persistent worker stopped safely; check its configuration and service health. No credentials or clinical input were logged.') from None


if __name__ == '__main__':
    main()
