"""Optional Redis primitives; NOT wired into the single-process application.

Streams provide at-least-once delivery, not exactly-once execution. Workers must
check the persisted job state and current visit revision before committing.
No clinical text or model credentials may enter the queue.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import hashlib
from urllib.parse import urlsplit


class RuntimeConfigurationError(ValueError):
    pass


class RuntimeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_ms: int
    remaining: int


@dataclass(frozen=True)
class JobReference:
    job_id: str
    owner: str
    visit_id: str
    goal: str
    source_revision: str
    use_model: bool = False

    def fields(self) -> dict[str, str]:
        if not re.fullmatch(r"[a-f0-9]{32}", self.job_id) or not re.fullmatch(r"[a-f0-9]{32}", self.visit_id):
            raise RuntimeConfigurationError("Queue identifiers must be server-generated UUID hex values.")
        if not re.fullmatch(r"[a-f0-9]{64}", self.owner) or not re.fullmatch(r"[a-f0-9]{64}", self.source_revision):
            raise RuntimeConfigurationError("Queue ownership/revision must be opaque digests.")
        if self.goal not in {"prepare_visit", "understand_symptom", "organize_timeline"} or not isinstance(self.use_model, bool):
            raise RuntimeConfigurationError("Unknown queue goal or flag.")
        return {"job_id": self.job_id, "owner": self.owner, "visit_id": self.visit_id,
                "goal": self.goal, "source_revision": self.source_revision, "use_model": "1" if self.use_model else "0"}


@dataclass(frozen=True)
class QueueDelivery:
    message_id: str
    task_id: str


def _opaque_job(identifier: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise RuntimeConfigurationError("Task identifiers must be server-generated UUID hex values.")
    return identifier


def _connection_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or parsed.fragment:
        raise RuntimeConfigurationError("Invalid Redis connection URL.")
    if parsed.scheme != "rediss" and parsed.hostname not in {"127.0.0.1", "localhost", "::1", "redis"}:
        raise RuntimeConfigurationError("Remote Redis requires TLS.")


# Redis TIME is shared by all web instances; accepted entries only are retained.
RATE_LUA = """
local stamp = redis.call('TIME')
local now = stamp[1] * 1000 + math.floor(stamp[2] / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])
if count >= limit then
    local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    redis.call('PEXPIRE', KEYS[1], window)
    return {0, math.max(1, tonumber(first[2]) + window - now), 0}
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window)
return {1, 0, limit - count - 1}
"""


class RedisRuntime:
    def __init__(self, client, *, namespace: str = "zhihe", group: str = "medical-workers"):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,40}", namespace) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,40}", group):
            raise RuntimeConfigurationError("Invalid Redis namespace/group.")
        self.client, self.namespace, self.group = client, namespace, group
        self.stream = namespace + ":job-references"

    @classmethod
    def from_url(cls, url: str, **kwargs):
        _connection_url(url)
        try:
            from redis.asyncio import Redis
        except ImportError as error:
            raise RuntimeConfigurationError("Install the optional Redis client before enabling shared runtime.") from error
        return cls(Redis.from_url(url, decode_responses=True, socket_timeout=3,
                                 socket_connect_timeout=3, health_check_interval=20, protocol=2), **kwargs)

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception as error:
            raise RuntimeUnavailable("Shared runtime is unavailable; do not silently use a process-local substitute.") from error

    async def rate_limit(self, owner: str, action: str, *, limit: int = 60, window_ms: int = 60_000) -> RateDecision:
        if not re.fullmatch(r"[a-f0-9]{64}", owner) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,40}", action):
            raise RuntimeConfigurationError("Rate limits require an opaque owner and fixed action.")
        if not 1 <= limit <= 10_000 or not 1000 <= window_ms <= 3_600_000:
            raise RuntimeConfigurationError("Rate window/limit out of range.")
        try:
            values = await self.client.eval(RATE_LUA, 1, f"{self.namespace}:rate:{action}:{owner}", window_ms, limit, secrets.token_hex(16))
            if len(values) != 3:
                raise ValueError("invalid script reply")
            return RateDecision(bool(int(values[0])), int(values[1]), int(values[2]))
        except Exception as error:
            raise RuntimeUnavailable("Shared rate limit failed; request must fail closed.") from error

    async def initialize_queue(self) -> None:
        try:
            await self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as error:
            # Ignore only Redis's exact already-existing-group response.
            if "BUSYGROUP" not in str(error):
                raise RuntimeUnavailable("Persistent queue initialization failed.") from error

    async def enqueue(self, reference: JobReference) -> str:
        fields = reference.fields()
        try:
            # No MAXLEN trim: trimming can silently discard unprocessed jobs.
            # The application must enforce queue admission/backpressure using
            # persisted active-job counts before calling this primitive.
            return str(await self.client.xadd(self.stream, fields))
        except Exception as error:
            raise RuntimeUnavailable("Job was not admitted to the persistent queue.") from error

    async def enqueue_task(self, task_id: str) -> str:
        identifier = _opaque_job(task_id)
        try:
            return str(await self.client.xadd(self.stream, {"task_id": identifier}))
        except Exception as error:
            raise RuntimeUnavailable("Task was not admitted to the persistent queue.") from error

    @staticmethod
    def _deliveries(entries) -> list[QueueDelivery]:
        deliveries = []
        for message_id, fields in entries:
            if not re.fullmatch(r"[0-9]+-[0-9]+", message_id) or set(fields) != {"task_id"}:
                raise RuntimeUnavailable("Unexpected queue envelope; worker must not execute it.")
            deliveries.append(QueueDelivery(message_id, _opaque_job(fields["task_id"])))
        return deliveries

    async def dequeue(self, consumer: str, *, count: int = 1, block_ms: int = 1000) -> list[QueueDelivery]:
        items = await self.read(consumer, count=count, block_ms=block_ms)
        return self._deliveries([entry for _, entries in items for entry in entries])

    async def recover(self, consumer: str, *, min_idle_ms: int = 120_000, start_id: str = "0-0") -> tuple[str, list[QueueDelivery]]:
        result = await self.reclaim(consumer, min_idle_ms=min_idle_ms, start_id=start_id)
        return result[0], self._deliveries(result[1])

    async def claim(self, task_id: str, *, lease_seconds: int = 120) -> str | None:
        identifier = _opaque_job(task_id)
        if not 30 <= lease_seconds <= 600:
            raise RuntimeConfigurationError("Invalid task lease.")
        token = secrets.token_hex(16)
        try:
            acquired = await self.client.set(f"{self.namespace}:lease:{identifier}", token, nx=True, ex=lease_seconds)
            return token if acquired else None
        except Exception as error:
            raise RuntimeUnavailable("Task lease could not be acquired.") from error

    async def release(self, task_id: str, token: str) -> bool:
        identifier = _opaque_job(task_id)
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            raise RuntimeConfigurationError("Invalid task lease token.")
        script = "if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end"
        try:
            return bool(await self.client.eval(script, 1, f"{self.namespace}:lease:{identifier}", token))
        except Exception as error:
            raise RuntimeUnavailable("Task lease release failed.") from error

    async def read(self, consumer: str, *, count: int = 1, block_ms: int = 1000):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", consumer) or not 1 <= count <= 10 or not 0 <= block_ms <= 2000:
            raise RuntimeConfigurationError("Invalid queue consumer/batch.")
        try:
            return await self.client.xreadgroup(self.group, consumer, {self.stream: ">"}, count=count, block=block_ms or None)
        except Exception as error:
            raise RuntimeUnavailable("Persistent queue read failed.") from error

    async def reclaim(self, consumer: str, *, min_idle_ms: int = 120_000, start_id: str = "0-0"):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", consumer) or min_idle_ms < 30_000 or not re.fullmatch(r"[0-9]+-[0-9]+", start_id):
            raise RuntimeConfigurationError("Invalid queue reclaim request.")
        try:
            return await self.client.xautoclaim(self.stream, self.group, consumer, min_idle_ms, start_id=start_id, count=10)
        except Exception as error:
            raise RuntimeUnavailable("Pending queue reclaim failed.") from error

    async def acknowledge(self, message_id: str) -> None:
        if not re.fullmatch(r"[0-9]+-[0-9]+", message_id):
            raise RuntimeConfigurationError("Invalid stream message identifier.")
        try:
            # Call ONLY after the database commit. Retries must be idempotent.
            await self.client.xack(self.stream, self.group, message_id)
            await self.client.xdel(self.stream, message_id)
        except Exception as error:
            raise RuntimeUnavailable("Queue acknowledgment failed; duplicate delivery may occur.") from error

    async def close(self) -> None:
        await self.client.aclose()


class RedisRateLimiter:
    """Synchronous shared gate for existing sync dependencies.

    Async handlers should call check_rate through asyncio.to_thread so a Redis
    outage cannot block the event loop. Failure never falls back to memory.
    """
    def __init__(self, client, *, namespace: str = "zhihe"):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,40}", namespace):
            raise RuntimeConfigurationError("Invalid Redis namespace.")
        self.client, self.namespace = client, namespace

    @classmethod
    def from_url(cls, url: str, **kwargs):
        _connection_url(url)
        try:
            from redis import Redis
        except ImportError as error:
            raise RuntimeConfigurationError("Install the optional Redis client before enabling shared limits.") from error
        return cls(Redis.from_url(url, decode_responses=True, socket_timeout=3,
                                 socket_connect_timeout=3, health_check_interval=20, protocol=2), **kwargs)

    def check_rate(self, identity: str, *, limit: int = 60, window_seconds: int = 60,
                   action: str = "request") -> RateDecision:
        if not isinstance(identity, str) or not 1 <= len(identity) <= 256 or not re.fullmatch(r"[a-z][a-z0-9_-]{1,40}", action):
            raise RuntimeConfigurationError("Invalid rate identity/action.")
        if not 1 <= limit <= 10_000 or not 1 <= window_seconds <= 3600:
            raise RuntimeConfigurationError("Invalid rate limit/window.")
        owner = hashlib.sha256(identity.encode()).hexdigest()
        try:
            result = self.client.eval(RATE_LUA, 1, f"{self.namespace}:rate:{action}:{owner}", window_seconds * 1000, limit, secrets.token_hex(16))
            return RateDecision(bool(int(result[0])), int(result[1]), int(result[2]))
        except Exception as error:
            raise RuntimeUnavailable("Shared rate gate failed; request must fail closed.") from error

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception as error:
            raise RuntimeUnavailable("Shared rate gate is unavailable.") from error

    def close(self) -> None:
        self.client.close()
