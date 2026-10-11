from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Executor, export_text
from .knowledge import Knowledge
from .model import ModelService, read_project_env
from .safety import check_urgent
from .schemas import ChecklistUpdate, CreateVisit, Intake, Message, MessageRequest, SummaryRequest, Visit
from .storage import Store, now

ROOT = Path(__file__).resolve().parents[1]
COOKIE = "zhihe_visitor"


def create_app(db_path: Path | None = None, model_service: ModelService | None = None) -> FastAPI:
    config = read_project_env(ROOT / ".env")
    config.update({key: os.environ[key] for key in (
        "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "MODEL_TIMEOUT_SECONDS", "DATA_RETENTION_DAYS",
        "VISIT_DATA_PATH", "COOKIE_SECURE",
    ) if key in os.environ})
    retention = max(1, min(30, int(config.get("DATA_RETENTION_DAYS", "7"))))
    timeout = max(1, min(30, float(config.get("MODEL_TIMEOUT_SECONDS", "12"))))
    database = db_path or Path(config.get("VISIT_DATA_PATH", str(ROOT / "data" / "visits.sqlite3")))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.store = Store(database)
        application.state.knowledge = Knowledge(ROOT / "knowledge" / "articles.json")
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), trust_env=False) as client:
            application.state.model = model_service or ModelService(
                config.get("DEEPSEEK_API_KEY", ""), config.get("DEEPSEEK_MODEL", "deepseek-chat"), client
            )
            application.state.executor = Executor(application.state.knowledge, application.state.model)
            cleanup()
            yield
        application.state.store.close()

    app = FastAPI(title="知禾 · 就诊准备 Agent", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]"])
    app.state.commit_lock = asyncio.Lock()
    buckets: dict[str, deque] = defaultdict(deque)
    last_cleanup = 0.0

    def cleanup():
        nonlocal last_cleanup
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).isoformat(timespec="seconds")
        app.state.store.expire(cutoff)
        last_cleanup = time.monotonic()

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        # Buffer at most 64 KiB so missing or forged Content-Length cannot evade limits.
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            chunks, length = [], 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > 65536:
                    return JSONResponse({"detail": "提交内容过大，请缩短后重试。"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        if hasattr(app.state, "store") and time.monotonic() - last_cleanup > 60:
            cleanup()
            stale = [key for key, values in buckets.items() if not values or values[-1] < time.monotonic() - 60]
            for key in stale:
                del buckets[key]
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = uuid4().hex
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's default includes submitted clinical text; do not echo it.
        fields = [".".join(str(p) for p in error["loc"] if p != "body") for error in exc.errors()]
        return JSONResponse({"detail": "输入格式或长度不正确，请检查后重试。", "fields": fields}, status_code=422)

    def identity(request: Request) -> tuple[str, str]:
        session = app.state.store.session(request.cookies.get(COOKIE))
        if not session:
            raise HTTPException(401, "访客会话已失效，请刷新页面重新进入。")
        return session

    def mutation(request: Request, session=Depends(identity)) -> str:
        owner, token = session
        if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), token):
            raise HTTPException(403, "请求校验失败，请刷新页面后重试。")
        origin = request.headers.get("Origin")
        allowed = {str(request.base_url).rstrip("/"), "http://127.0.0.1:5173", "http://localhost:5173"}
        if origin and origin.rstrip("/") not in allowed:
            raise HTTPException(403, "不接受其他网站发起的修改请求。")
        return owner

    def owned(owner: str, visit_id: str) -> Visit:
        visit = app.state.store.get(owner, visit_id)
        if visit is None:
            raise HTTPException(404, "未找到这条就诊准备记录。")
        return visit

    def rate_limit(owner: str):
        values = buckets[owner]
        moment = time.monotonic()
        while values and values[0] < moment - 60:
            values.popleft()
        if len(values) >= 120:
            raise HTTPException(429, "请求过于频繁，请稍等一分钟再试。", headers={"Retry-After": "60"})
        values.append(moment)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "service": "zhihe-medical-visit-agent"}

    @app.get("/readyz")
    async def ready():
        items = app.state.knowledge.all()
        if app.state.knowledge.load_error or not items:
            return JSONResponse({"status": "not_ready", "detail": "知识资料未通过加载校验。"}, status_code=503)
        try:
            with app.state.store.connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:
            return JSONResponse({"status": "not_ready", "detail": "本机记录存储暂不可用。"}, status_code=503)
        return {"status": "ready", "knowledge_count": len(items)}

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request, response: Response):
        session = app.state.store.session(request.cookies.get(COOKIE))
        if not session:
            cookie, owner, token = app.state.store.new_session()
            response.set_cookie(COOKIE, cookie, max_age=retention * 86400, httponly=True,
                                samesite="strict", secure=config.get("COOKIE_SECURE", "false").lower() == "true")
        else:
            owner, token = session
        visits = app.state.store.list_visits(owner)
        return {"csrf_token": token, "model_available": app.state.model.available,
                "privacy": {
                    "local_default": True, "retention_days": retention,
                    "notice": "默认在本机整理。记录以本机数据库保存，可删除；请使用虚构演示资料。",
                    "model_notice": "主动开启后，仅向 DeepSeek 发送有限的症状词、时间和程度片段，不发送身份、表单或完整对话。",
                }, "sources": app.state.knowledge.all(), "visits": visits,
                "current_visit": visits[0] if visits else None}

    @app.get("/api/knowledge")
    async def knowledge():
        return {"items": app.state.knowledge.all()}

    @app.post("/api/visits", response_model=Visit, status_code=201)
    async def create(body: CreateVisit, owner=Depends(mutation)):
        async with app.state.commit_lock:
            if len(app.state.store.list_visits(owner)) >= 100:
                raise HTTPException(409, "记录已达 100 条，请先删除不需要的记录。")
            stamp = now()
            visit = Visit(id=uuid4().hex, title=body.title, created_at=stamp, updated_at=stamp,
                          risk=check_urgent("", Intake()))
            app.state.store.put(owner, visit)
        return visit

    @app.get("/api/visits/{visit_id}", response_model=Visit)
    async def get_visit(visit_id: str, session=Depends(identity)):
        return owned(session[0], visit_id)

    @app.patch("/api/visits/{visit_id}/intake", response_model=Visit)
    async def update_intake(visit_id: str, body: Intake, owner=Depends(mutation)):
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            visit.intake = body
            visit.risk = check_urgent("", body)
            visit.summary = None
            visit.updated_at = now()
            app.state.store.put(owner, visit)
        return visit

    @app.post("/api/visits/{visit_id}/messages")
    async def send_message(visit_id: str, body: MessageRequest, owner=Depends(mutation)):
        visit = owned(owner, visit_id)
        if len(visit.messages) >= 200:
            raise HTTPException(409, "本条记录已达到对话上限，请新建记录。")
        # Emergency rules run inside Executor before any model I/O; do not queue that check.
        if check_urgent(body.content, visit.intake).level != "emergency":
            rate_limit(owner)
        result = await app.state.executor.run(visit, body.content, body.use_model)
        if not result.content.strip():
            raise HTTPException(503, "整理结果为空，请重试或联系专业医务人员。")
        stamp = now()
        user = Message(id=uuid4().hex, role="user", content=body.content, created_at=stamp,
                       risk=result.risk)
        assistant = Message(id=uuid4().hex, role="assistant", content=result.content, created_at=stamp,
                            risk=result.risk, sources=result.sources, trace=result.trace,
                            suggested_questions=result.suggested_questions, mode=result.mode,
                            duration_ms=result.duration_ms)
        async with app.state.commit_lock:
            latest = owned(owner, visit_id)  # A deleted visit is never recreated by a slow model response.
            if len(latest.messages) >= 200:
                raise HTTPException(409, "本条记录已达到对话上限，请新建记录。")
            changed = latest.intake != visit.intake or latest.messages != visit.messages
            latest.messages.extend([user, assistant])
            latest.summary = None if changed else result.summary
            if latest.risk.level != "emergency" or result.risk.level == "emergency":
                latest.risk = result.risk
            if not changed:
                latest.checklist = result.checklist
            latest.updated_at = now()
            app.state.store.put(owner, latest)
        return {"user_message": user, "assistant_message": assistant, "visit": latest}

    @app.post("/api/visits/{visit_id}/summary")
    async def summary(visit_id: str, body: SummaryRequest, owner=Depends(mutation)):
        visit = owned(owner, visit_id)
        rate_limit(owner)
        result = await app.state.executor.run(visit, use_model=body.use_model, purpose="summary")
        async with app.state.commit_lock:
            latest = owned(owner, visit_id)
            if latest.intake != visit.intake or latest.messages != visit.messages:
                raise HTTPException(409, "资料在整理期间有更新，请重新生成摘要。")
            latest.summary = result.summary
            latest.checklist = result.checklist
            if latest.risk.level != "emergency" or result.risk.level == "emergency":
                latest.risk = result.risk
            latest.updated_at = now()
            app.state.store.put(owner, latest)
        return {"summary": latest.summary, "checklist": latest.checklist, "visit": latest}

    @app.patch("/api/visits/{visit_id}/checklist", response_model=Visit)
    async def checklist(visit_id: str, body: ChecklistUpdate, owner=Depends(mutation)):
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            item = next((item for item in visit.checklist if item.id == body.item_id), None)
            if item is None:
                raise HTTPException(404, "未找到这项准备事项。")
            item.done = body.done
            visit.updated_at = now()
            app.state.store.put(owner, visit)
        return visit

    @app.get("/api/visits/{visit_id}/export")
    async def export(visit_id: str, session=Depends(identity)):
        visit = owned(session[0], visit_id)
        filename = quote("就诊准备摘要.txt")
        return Response(export_text(visit), media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=visit-summary.txt; filename*=UTF-8''{filename}"})

    @app.delete("/api/visits/{visit_id}", status_code=204)
    async def delete_visit(visit_id: str, owner=Depends(mutation)):
        async with app.state.commit_lock:
            if not app.state.store.delete(owner, visit_id):
                raise HTTPException(404, "未找到这条就诊准备记录。")
        return Response(status_code=204)

    @app.delete("/api/data", status_code=204)
    async def clear(owner=Depends(mutation)):
        async with app.state.commit_lock:
            app.state.store.clear(owner)
        return Response(status_code=204)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    async def unknown_api(path: str):
        raise HTTPException(404, "接口不存在。")

    dist = ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @app.get("/")
        async def unbuilt():
            return JSONResponse({"detail": "页面尚未构建，请在 frontend 运行 npm run build 后重启。"}, status_code=503)
    return app


app = create_app()
