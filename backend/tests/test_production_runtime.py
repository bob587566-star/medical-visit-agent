import asyncio

import pytest

from backend.production_runtime import JobReference, RedisRuntime, RuntimeConfigurationError, RuntimeUnavailable


class RedisDouble:
    def __init__(self):
        self.calls = []
        self.unavailable = False
        self.already = False

    async def eval(self, *args):
        self.calls.append(("eval", args))
        if self.unavailable:
            raise OSError("fictional Redis unavailable")
        return [0, 2500, 0]

    async def xgroup_create(self, *args, **kwargs):
        self.calls.append(("group", args, kwargs))
        if self.already:
            raise ValueError("BUSYGROUP Consumer Group name already exists")
        if self.unavailable:
            raise OSError("offline")

    async def xadd(self, *args, **kwargs):
        self.calls.append(("add", args, kwargs))
        return "1-0"

    async def xreadgroup(self, *args, **kwargs):
        self.calls.append(("read", args, kwargs))
        return []

    async def xautoclaim(self, *args, **kwargs):
        self.calls.append(("reclaim", args, kwargs))
        return ["0-0", [], []]

    async def xack(self, *args):
        self.calls.append(("ack", args))

    async def xdel(self, *args):
        self.calls.append(("delete", args))


def ref(**overrides):
    fields = dict(job_id="a" * 32, visit_id="b" * 32, owner="c" * 64,
                  source_revision="d" * 64, goal="prepare_visit")
    return JobReference(**(fields | overrides))


def test_rate_calls_atomic_script_and_propagates_retry_interval():
    client = RedisDouble()
    result = asyncio.run(RedisRuntime(client).rate_limit("a" * 64, "message"))
    assert not result.allowed and result.retry_after_ms == 2500
    assert "redis.call('TIME')" in client.calls[0][1][0]
    assert client.calls[0][1][1] == 1


def test_rate_unavailable_never_falls_back_to_memory():
    client = RedisDouble()
    client.unavailable = True
    with pytest.raises(RuntimeUnavailable):
        asyncio.run(RedisRuntime(client).rate_limit("a" * 64, "message"))


@pytest.mark.parametrize("override", ({"owner": "patient-phone"}, {"goal": "arbitrary_code"}, {"visit_id": "raw-health-text"}, {"use_model": "yes"}))
def test_queue_reference_rejects_non_metadata(override):
    with pytest.raises(RuntimeConfigurationError):
        ref(**override).fields()


def test_queue_exposes_real_stream_group_reclaim_and_postcommit_ack_contract():
    client = RedisDouble()
    runtime = RedisRuntime(client)

    async def sequence():
        await runtime.initialize_queue()
        assert await runtime.enqueue(ref()) == "1-0"
        await runtime.read("worker-1")
        await runtime.reclaim("worker-2")
        await runtime.acknowledge("1-0")

    asyncio.run(sequence())
    assert [item[0] for item in client.calls] == ["group", "add", "read", "reclaim", "ack", "delete"]
    assert set(client.calls[1][1][1]) == {"job_id", "owner", "visit_id", "goal", "source_revision", "use_model"}
    assert client.calls[1][2] == {}  # Never silently trim an unfinished queue.


def test_existing_group_is_idempotent_but_network_failure_is_not():
    client = RedisDouble()
    client.already = True
    asyncio.run(RedisRuntime(client).initialize_queue())
    client.already, client.unavailable = False, True
    with pytest.raises(RuntimeUnavailable):
        asyncio.run(RedisRuntime(client).initialize_queue())


def test_remote_plaintext_redis_rejected_before_import_or_io():
    with pytest.raises(RuntimeConfigurationError):
        RedisRuntime.from_url("redis://example.com:6379/0")
