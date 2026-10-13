"""Finite checks against the isolated, password-protected local Redis service.

Only generated opaque task IDs are enqueued. Cleanup is limited to this run's
random namespace; no FLUSHDB/FLUSHALL or application data mutation.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.production_runtime import RedisRateLimiter, RedisRuntime


def configuration(path: Path) -> dict:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if not values.get("REDIS_URL", "").startswith("redis://:") or "@127.0.0.1:18379/" not in values["REDIS_URL"]:
        raise ValueError("Expected the isolated fixture Redis configuration.")
    return values


async def check_queue(url: str, namespace: str, cases: list[dict]) -> None:
    runtime = RedisRuntime.from_url(url, namespace=namespace)
    await runtime.initialize_queue()
    try:
        task = secrets.token_hex(16)
        message = await runtime.enqueue_task(task)
        first = await runtime.dequeue("worker-before-restart")
        assert len(first) == 1 and first[0].task_id == task and first[0].message_id == message
        cases.append({"case": "persistent_stream_delivery", "passed": True})
        # Simulate a dead worker using Redis's documented idle argument rather
        # than sleeping for the production reclaim timeout.
        await runtime.client.xclaim(runtime.stream, runtime.group, "dead-worker", 0, [message], idle=60_000, justid=True)
        await runtime.close()
        runtime = RedisRuntime.from_url(url, namespace=namespace)
        await runtime.initialize_queue()
        _, recovered = await runtime.recover("worker-after-restart", min_idle_ms=30_000)
        assert len(recovered) == 1 and recovered[0].task_id == task
        cases.append({"case": "fresh_worker_reclaims_unacknowledged_job", "passed": True})
        lease = await runtime.claim(task)
        assert lease and await runtime.claim(task) is None
        assert not await runtime.release(task, secrets.token_hex(16))
        assert await runtime.release(task, lease)
        cases.append({"case": "job_lease_rejects_duplicate_worker_and_wrong_token", "passed": True})
        await runtime.acknowledge(message)
        assert await runtime.client.xlen(runtime.stream) == 0
        assert (await runtime.client.xpending(runtime.stream, runtime.group))["pending"] == 0
        cases.append({"case": "postcommit_acknowledgement_removes_pending_and_stream_item", "passed": True})
        assert await runtime.dequeue("idle-worker", block_ms=100) == []
    finally:
        keys = [key async for key in runtime.client.scan_iter(match=namespace + ":*")]
        if keys:
            await runtime.client.unlink(*keys)
        await runtime.close()


def run(config: Path) -> dict:
    from redis import Redis
    from redis.exceptions import AuthenticationError
    url = configuration(config)["REDIS_URL"]
    namespace = "zhihe-check-" + secrets.token_hex(8)
    cases = []
    first = RedisRateLimiter.from_url(url, namespace=namespace)
    second = RedisRateLimiter.from_url(url, namespace=namespace)
    stamp = time.perf_counter()
    try:
        assert first.ping() and second.ping()
        cases.append({"case": "two_independent_connections_ready", "passed": True})
        with ThreadPoolExecutor(max_workers=8) as workers:
            decisions = list(workers.map(lambda index: (first if index % 2 else second).check_rate("fictional-shared-subject", limit=10, window_seconds=10), range(30)))
        assert sum(item.allowed for item in decisions) == 10
        assert all(0 < item.retry_after_ms <= 10_000 for item in decisions if not item.allowed)
        cases.append({"case": "atomic_limit_across_two_clients_30_requests_10_admitted", "passed": True})
        assert first.check_rate("different-fictional-subject", limit=10, window_seconds=10).allowed
        cases.append({"case": "other_subject_independent", "passed": True})
        unauthenticated = Redis(host="127.0.0.1", port=18379, socket_timeout=2)
        try:
            try:
                unauthenticated.ping()
                raise AssertionError("Unauthenticated Redis connection was accepted")
            except AuthenticationError:
                cases.append({"case": "unauthenticated_connection_refused", "passed": True})
        finally:
            unauthenticated.close()
        asyncio.run(check_queue(url, namespace, cases))
        return {"scope": "isolated local Redis; no clinical text or application namespace", "cases": cases,
                "passed": len(cases), "duration_ms": round((time.perf_counter() - stamp) * 1000),
                "notice": "Live Redis primitives passed. This does not establish application multi-instance consistency or Redis high availability."}
    finally:
        keys = list(first.client.scan_iter(match=namespace + ":*"))
        if keys:
            first.client.unlink(*keys)
        first.close()
        second.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("data/runtime-check.env"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/redis-runtime-check-20261013.json"))
    args = parser.parse_args()
    try:
        result = run(args.config)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": result["passed"], "duration_ms": result["duration_ms"], "report": str(args.out)}))
        return 0
    except Exception as error:
        print("Isolated Redis check failed: " + type(error).__name__ + "; credentials and queue contents were not printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
