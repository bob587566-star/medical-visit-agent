"""Request-local, fresh authorization immediately before external model I/O."""
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException

from .risk_policy import requires_human_support

_authorization = ContextVar('medical_external_authorization', default=None)


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
    token = _authorization.set(verify)
    try:
        yield
    finally:
        _authorization.reset(token)


def check_egress():
    checker = _authorization.get()
    if checker is not None:
        checker()


async def model_request_hook(request):
    if request.url.host == 'api.deepseek.com':
        check_egress()
