"""Request-local, fresh authorization immediately before external model I/O."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException

from .risk_policy import requires_human_support
from .model_budget import ModelBudgetUnavailableError

_authorization = ContextVar('medical_external_authorization', default=None)


@dataclass(frozen=True)
class _AuthorizedOwner:
    owner: str
    verify: Callable[[], None]


@contextmanager
def authorized_egress(privacy, owner: str, visit_id: str | None = None):
    def verify():
        privacy.require(owner, 'external_ai')
        if visit_id is not None:
            current = privacy.store.get(owner, visit_id)
            if current is None:
                raise HTTPException(404, '记录已删除，未发送资料。')
            if requires_human_support(current.risk):
                raise HTTPException(409, '当前记录需先获得人类支持，已暂停外部模型处理。')
    token = _authorization.set(_AuthorizedOwner(owner, verify))
    try:
        yield
    finally:
        _authorization.reset(token)


def check_egress():
    authorization = _authorization.get()
    if authorization is not None:
        authorization.verify()


async def model_request_hook(request, *, budget=None):
    if request.url.host == 'api.deepseek.com':
        check_egress()
        if budget is not None:
            authorization = _authorization.get()
            if authorization is None:
                raise ModelBudgetUnavailableError('Paid model requests require an authorized owner context.')
            await budget.reserve(authorization.owner)
            # Reservation itself awaits Redis: consent, deletion or safety may
            # change in that gap. Recheck immediately before HTTPX dispatch.
            check_egress()
