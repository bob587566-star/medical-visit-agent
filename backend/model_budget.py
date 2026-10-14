"""Shared admission for paid model dispatches; opaque metadata only."""
from __future__ import annotations

import re
import secrets

import httpx


class ModelCallLimitError(httpx.HTTPError):
    """The shared owner-minute or application-day allowance was exhausted."""


class ModelBudgetUnavailableError(httpx.HTTPError):
    """A paid request cannot proceed without verifiable shared admission."""


# One Redis operation checks both windows before adding either reservation.
# Redis TIME, rather than instance clocks, is common to all Vercel requests.
# Entries represent dispatch attempts, including provider failures/timeouts;
# refunding failures could allow unlimited potentially billed retries.
MODEL_BUDGET_LUA = """
local stamp = redis.call('TIME')
local now = stamp[1] * 1000 + math.floor(stamp[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60000)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - 86400000)
local owner_count = redis.call('ZCARD', KEYS[1])
local global_count = redis.call('ZCARD', KEYS[2])
if owner_count >= tonumber(ARGV[1]) or global_count >= tonumber(ARGV[2]) then
    return {0, owner_count, global_count}
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('ZADD', KEYS[2], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], 60000)
redis.call('PEXPIRE', KEYS[2], 86400000)
return {1, owner_count + 1, global_count + 1}
"""


class ModelCallBudget:
    def __init__(self, runtime, *, per_owner_minute: int = 5, global_day: int = 100):
        if type(per_owner_minute) is not int or not 1 <= per_owner_minute <= 120:
            raise ValueError('Model owner-minute limit must be an integer from 1 to 120.')
        if type(global_day) is not int or not 1 <= global_day <= 10_000:
            raise ValueError('Model rolling-day limit must be an integer from 1 to 10000.')
        namespace = getattr(runtime, 'namespace', None)
        if not isinstance(namespace, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{1,40}', namespace) or getattr(runtime, 'client', None) is None:
            raise ValueError('Model admission requires a configured shared runtime and namespace.')
        self.runtime = runtime
        self.per_owner_minute = per_owner_minute
        self.global_day = global_day
        self.prefix = namespace + ':model-budget:'

    async def reserve(self, owner: str) -> None:
        if not isinstance(owner, str) or not re.fullmatch(r'[a-f0-9]{64}', owner):
            raise ModelBudgetUnavailableError('Paid model admission requires opaque ownership.')
        try:
            values = await self.runtime.client.eval(
                MODEL_BUDGET_LUA, 2, self.prefix + 'owner:' + owner, self.prefix + 'global',
                self.per_owner_minute, self.global_day, secrets.token_hex(16),
            )
            if not isinstance(values, (list, tuple)) or len(values) != 3:
                raise ValueError('Invalid shared admission reply.')
            allowed, owner_count, global_count = (int(value) for value in values)
            if allowed not in {0, 1} or owner_count < 0 or global_count < 0:
                raise ValueError('Invalid shared admission counts.')
            if allowed and (not 1 <= owner_count <= self.per_owner_minute or not 1 <= global_count <= self.global_day):
                raise ValueError('Shared admission exceeded its configured allowance.')
        except Exception:
            # Driver messages may contain runtime details. Never expose them or
            # silently replace a distributed allowance with process-local state.
            raise ModelBudgetUnavailableError('Shared paid model admission is unavailable.') from None
        if not allowed:
            raise ModelCallLimitError('Paid model allowance exhausted; built-in processing remains available.')
