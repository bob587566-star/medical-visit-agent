"""Opaque synthetic owners and mocked Redis/provider; no live model calls."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from backend.egress import authorized_egress, check_egress, model_request_hook
from backend.model_budget import MODEL_BUDGET_LUA, ModelCallBudget, ModelCallLimitError, ModelBudgetUnavailableError
from backend.schemas import Risk


OWNER = 'a' * 64
OTHER = 'b' * 64
ENDPOINT = 'https://api.deepseek.com/chat/completions'


class RedisDouble:
    """Atomic two-window transport double, not proof that Lua executed in Redis."""
    def __init__(self):
        self.now_ms = 1_800_000_000_000
        self.zsets, self.expiry, self.calls = {}, {}, []
        self.lock = asyncio.Lock()
        self.failure = None

    async def eval(self, script, count, *args):
        assert script == MODEL_BUDGET_LUA and count == 2
        self.calls.append((count, args))
        if self.failure:
            raise self.failure
        async with self.lock:
            await asyncio.sleep(0)  # Exercise competing callers without sleeping.
            owner_key, global_key, owner_limit, global_limit, member = args
            for key, window in ((owner_key, 60_000), (global_key, 86_400_000)):
                entries = self.zsets.setdefault(key, {})
                self.zsets[key] = {item: stamp for item, stamp in entries.items() if stamp > self.now_ms - window}
            counts = len(self.zsets[owner_key]), len(self.zsets[global_key])
            if counts[0] >= owner_limit or counts[1] >= global_limit:
                return [0, *counts]
            self.zsets[owner_key][member] = self.now_ms
            self.zsets[global_key][member] = self.now_ms
            self.expiry[owner_key], self.expiry[global_key] = 60_000, 86_400_000
            return [1, counts[0] + 1, counts[1] + 1]


def make_budget(**kwargs):
    client = RedisDouble()
    return ModelCallBudget(SimpleNamespace(namespace='fictional-model', client=client), **kwargs), client


class PrivacyDouble:
    def __init__(self):
        self.allowed, self.exists = True, True
        self.risk = Risk(message='fictional non-urgent fixture')
        self.checked = []
        self.store = SimpleNamespace(get=self.get)

    def require(self, owner, scope):
        self.checked.append((owner, scope))
        if not self.allowed:
            raise HTTPException(403, 'fictional consent revoked')

    def get(self, owner, visit_id):
        return SimpleNamespace(risk=self.risk) if self.exists else None


def test_atomic_owner_rejection_does_not_consume_global_allowance():
    async def scenario():
        budget, client = make_budget(per_owner_minute=1, global_day=2)
        await budget.reserve(OWNER)
        with pytest.raises(ModelCallLimitError):
            await budget.reserve(OWNER)
        assert len(client.zsets[budget.prefix + 'global']) == 1
        await budget.reserve(OTHER)
        assert len(client.zsets[budget.prefix + 'global']) == 2
    asyncio.run(scenario())


def test_global_allowance_is_shared_across_rotated_owners_and_rejection_keeps_owner_empty():
    async def scenario():
        budget, client = make_budget(per_owner_minute=5, global_day=2)
        await budget.reserve(OWNER)
        await budget.reserve(OTHER)
        with pytest.raises(ModelCallLimitError):
            await budget.reserve('c' * 64)
        assert not client.zsets[budget.prefix + 'owner:' + 'c' * 64]
        assert len(client.zsets[budget.prefix + 'global']) == 2
    asyncio.run(scenario())


def test_concurrent_reservations_across_instances_cannot_exceed_shared_cap():
    async def scenario():
        budget, client = make_budget(per_owner_minute=5, global_day=7)
        other_instance = ModelCallBudget(budget.runtime, per_owner_minute=5, global_day=7)
        owners = [f'{index:064x}' for index in range(50)]
        results = await asyncio.gather(*( (budget if index % 2 else other_instance).reserve(owner)
                                         for index, owner in enumerate(owners)), return_exceptions=True)
        assert sum(result is None for result in results) == 7
        assert sum(isinstance(result, ModelCallLimitError) for result in results) == 43
        assert len(client.zsets[budget.prefix + 'global']) == 7
    asyncio.run(scenario())


def test_concurrent_owner_requests_cannot_exceed_minute_cap():
    async def scenario():
        budget, client = make_budget(per_owner_minute=5, global_day=100)
        results = await asyncio.gather(*(budget.reserve(OWNER) for _ in range(40)), return_exceptions=True)
        assert sum(result is None for result in results) == 5
        assert sum(isinstance(result, ModelCallLimitError) for result in results) == 35
        assert len(client.zsets[budget.prefix + 'owner:' + OWNER]) == 5
        assert len(client.zsets[budget.prefix + 'global']) == 5
    asyncio.run(scenario())


def test_minute_rolling_day_and_key_expiry_use_shared_server_time():
    async def scenario():
        budget, client = make_budget(per_owner_minute=1, global_day=2)
        await budget.reserve(OWNER)
        client.now_ms += 60_000
        await budget.reserve(OWNER)
        assert len(client.zsets[budget.prefix + 'owner:' + OWNER]) == 1
        assert len(client.zsets[budget.prefix + 'global']) == 2
        with pytest.raises(ModelCallLimitError):
            await budget.reserve(OTHER)
        client.now_ms += 86_400_000
        await budget.reserve(OTHER)
        assert len(client.zsets[budget.prefix + 'global']) == 1
        assert client.expiry[budget.prefix + 'global'] == 86_400_000
        assert client.expiry[budget.prefix + 'owner:' + OTHER] == 60_000
    asyncio.run(scenario())


@pytest.mark.parametrize('kwargs', [
    {'per_owner_minute': 0}, {'per_owner_minute': 121}, {'per_owner_minute': True},
    {'per_owner_minute': '5'}, {'global_day': 0}, {'global_day': 10001}, {'global_day': False},
])
def test_invalid_limits_fail_configuration_before_redis_calls(kwargs):
    with pytest.raises(ValueError):
        make_budget(**kwargs)


@pytest.mark.parametrize('namespace', [None, '', 'Bad', 'space key', 'clinical:text'])
def test_invalid_namespace_is_not_used_as_model_budget_scope(namespace):
    with pytest.raises(ValueError):
        ModelCallBudget(SimpleNamespace(namespace=namespace, client=RedisDouble()))


def test_budget_keys_values_are_only_namespace_opaque_owner_and_unique_dispatch_id():
    async def scenario():
        budget, client = make_budget()
        await budget.reserve(OWNER)
        count, args = client.calls[0]
        assert count == 2 and args[:4] == (budget.prefix + 'owner:' + OWNER, budget.prefix + 'global', 5, 100)
        assert len(args[4]) == 32 and all(character in '0123456789abcdef' for character in args[4])
        assert not any(word in str(args) for word in ('snapshot', 'password', 'Bearer', '嗓子疼'))
        with pytest.raises(ModelBudgetUnavailableError):
            await budget.reserve('fictional patient text')
        assert len(client.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('reply', [None, [], [1], [2, 1, 1], [1, -1, 1], [1, 0, 0], [1, 6, 1]])
def test_invalid_redis_reply_fails_closed(reply):
    async def scenario():
        async def eval(*args):
            return reply
        budget = ModelCallBudget(SimpleNamespace(namespace='fictional-model', client=SimpleNamespace(eval=eval)))
        with pytest.raises(ModelBudgetUnavailableError):
            await budget.reserve(OWNER)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['limit', 'runtime', 'missing_context'])
def test_rejected_hook_never_dispatches_to_provider(failure):
    async def scenario():
        budget, redis = make_budget(per_owner_minute=1, global_day=1)
        privacy, calls = PrivacyDouble(), []
        if failure == 'limit':
            await budget.reserve(OWNER)
        elif failure == 'runtime':
            redis.failure = RuntimeError('fictional driver-only information')
        async def hook(request):
            await model_request_hook(request, budget=budget)
        def provider(request):
            calls.append(request)
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider), event_hooks={'request': [hook]}) as client:
            error = ModelCallLimitError if failure == 'limit' else ModelBudgetUnavailableError
            if failure == 'missing_context':
                with pytest.raises(error):
                    await client.post(ENDPOINT, json={})
                assert not redis.calls
            else:
                with authorized_egress(privacy, OWNER, 'fictional-visit'):
                    with pytest.raises(error):
                        await client.post(ENDPOINT, json={})
        assert calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize('change,expected', [('revoke', 403), ('delete', 404), ('safety', 409)])
def test_authorization_is_checked_again_after_awaited_reservation(change, expected):
    async def scenario():
        budget, redis = make_budget()
        privacy, calls = PrivacyDouble(), []
        original = budget.reserve
        async def raced(owner):
            await original(owner)
            if change == 'revoke':
                privacy.allowed = False
            elif change == 'delete':
                privacy.exists = False
            else:
                privacy.risk = Risk(level='emergency', kind='medical', message='fictional danger')
        budget.reserve = raced
        async def hook(request):
            await model_request_hook(request, budget=budget)
        def provider(request):
            calls.append(request)
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider), event_hooks={'request': [hook]}) as client:
            with authorized_egress(privacy, OWNER, 'fictional-visit'):
                with pytest.raises(HTTPException) as failure:
                    await client.post(ENDPOINT, json={})
            assert failure.value.status_code == expected
        assert calls == [] and len(redis.zsets[budget.prefix + 'global']) == 1
    asyncio.run(scenario())


def test_preflight_checks_do_not_count_and_each_deepseek_dispatch_is_reserved_once():
    async def scenario():
        budget, redis = make_budget()
        privacy, sent = PrivacyDouble(), []
        async def hook(request):
            await model_request_hook(request, budget=budget)
        def provider(request):
            sent.append(request.url.host)
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider), event_hooks={'request': [hook]}) as client:
            with authorized_egress(privacy, OWNER, 'fictional-visit'):
                check_egress()
                await client.post(ENDPOINT, json={'fictional_kind': 'extraction'})
                check_egress()
                await client.post(ENDPOINT, json={'fictional_kind': 'planning'})
            await client.get('https://fictional.test/public')
        assert sent == ['api.deepseek.com', 'api.deepseek.com', 'fictional.test']
        assert len(redis.calls) == 2 and len(redis.zsets[budget.prefix + 'global']) == 2
        assert len(privacy.checked) == 6  # Preflight + before/after each reservation.
    asyncio.run(scenario())


def test_legacy_egress_context_and_budget_free_hook_behavior_are_preserved():
    async def scenario():
        await model_request_hook(httpx.Request('POST', ENDPOINT))
        privacy = PrivacyDouble()
        with authorized_egress(privacy, OWNER):
            check_egress()
            privacy.allowed = False
            with pytest.raises(HTTPException):
                await model_request_hook(httpx.Request('POST', ENDPOINT))
        check_egress()  # Context cleanup restores the existing unbound behavior.
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['limit', 'runtime'])
def test_extraction_and_planner_use_controlled_local_fallback_when_admission_fails(failure):
    from backend.agent import Executor
    from backend.model import ModelService
    from backend.task_agent import TaskEngine
    from backend.tests.test_task_agent import knowledge, record

    async def scenario():
        budget, redis = make_budget(per_owner_minute=1, global_day=1)
        if failure == 'limit':
            await budget.reserve(OWNER)
        else:
            redis.failure = RuntimeError('fictional shared runtime unavailable')
        calls = []
        async def hook(request):
            await model_request_hook(request, budget=budget)
        def provider(request):
            calls.append(request)
            pytest.fail('Admission failure must never dispatch the mocked provider.')
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider), event_hooks={'request': [hook]}) as client:
            model = ModelService('fictional-test-key-never-used-on-network', client=client)
            with authorized_egress(PrivacyDouble(), OWNER, 'fictional-visit'):
                assert await model.extract('咳嗽') is None
                engine = TaskEngine(Executor(knowledge(), model), model)
                result = await engine.run(record(), 'prepare_visit', use_model=True)
            assert result.plan.planner_mode == 'fallback' and result.planner_notice
            assert result.status == 'needs_input' and result.summary
        assert calls == []
    asyncio.run(scenario())
