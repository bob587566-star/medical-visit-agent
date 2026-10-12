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
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Executor, export_text
from .knowledge import Knowledge
from .model import ModelService, read_project_env
from .safety import check_conversation_urgent, check_urgent
from .schemas import BookmarkUpdate, ChecklistUpdate, CreateVisit, Intake, Message, MessageRequest, Observation, ObservationRequest, RenameVisit, SummaryRequest, Visit
from .storage import Store, now
from .task_agent import TaskEngine
from .task_storage import TaskStore
from .tasks_api import register_task_routes
from .safety_review import LocalSafetyReview

ROOT = Path(__file__).resolve().parents[1]
COOKIE = "zhihe_visitor"


def create_app(db_path: Path | None = None, model_service: ModelService | None = None) -> FastAPI:
    config = read_project_env(ROOT / ".env")
    config.update({key: os.environ[key] for key in (
        "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "MODEL_TIMEOUT_SECONDS", "DATA_RETENTION_DAYS",
        "VISIT_DATA_PATH", "COOKIE_SECURE", "LOCAL_SAFETY_ENABLED", "LOCAL_SAFETY_MODEL",
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
            application.state.retriever = None
            application.state.safety_review = LocalSafetyReview(client, config.get('LOCAL_SAFETY_ENABLED', 'false').lower() == 'true', config.get('LOCAL_SAFETY_MODEL', 'qwen2.5:7b'))
            warm_task = None
            if db_path is None:
                from .retrieval import HybridRetriever
                application.state.retriever = HybridRetriever(application.state.knowledge, client,
                                                               ROOT / "data" / "knowledge-vectors.json")
                warm_task = asyncio.create_task(application.state.retriever.initialize())
            application.state.executor = Executor(application.state.knowledge, application.state.model,
                                                  application.state.retriever, application.state.safety_review)
            application.state.task_engine = TaskEngine(application.state.executor, application.state.model)
            application.state.task_store = TaskStore(application.state.store)
            application.state.task_store.interrupt_unfinished()
            application.state.task_slots = asyncio.Semaphore(2)
            application.state.task_workers = {}
            cleanup()
            try:
                yield
            finally:
                workers = list(application.state.task_workers.values())
                for worker in workers:
                    worker.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
                if warm_task:
                    warm_task.cancel()
                    try:
                        await warm_task
                    except (asyncio.CancelledError, Exception):
                        pass
        application.state.store.close()

    app = FastAPI(title="知禾 · 健康沟通工作台", version="2.0.0", lifespan=lifespan,
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
        allowed = {str(request.base_url).rstrip("/"), "http://127.0.0.1:5174", "http://localhost:5174"}
        if origin and origin.rstrip("/") not in allowed:
            raise HTTPException(403, "不接受其他网站发起的修改请求。")
        return owner

    def owned(owner: str, visit_id: str) -> Visit:
        visit = app.state.store.get(owner, visit_id)
        if visit is None:
            raise HTTPException(404, "未找到这条就诊准备记录。")
        from .planning import collection_for
        visit.collection = collection_for(visit.intake, visit.risk)
        return visit

    def retrieval_status():
        if app.state.retriever:
            return app.state.retriever.status()
        return {"mode": "topic_bm25", "embedding_available": False, "model": "bge-m3:latest",
                "document_count": len(app.state.knowledge.all())}

    def refresh_collection(visit: Visit):
        from .planning import collection_for
        visit.collection = collection_for(visit.intake, visit.risk)
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
        visits = [refresh_collection(visit) for visit in visits]
        return {"csrf_token": token, "model_available": app.state.model.available,
                "privacy": {
                    "local_default": True, "retention_days": retention,
                    "notice": "默认在本机整理。记录以本机数据库保存，可删除；请使用虚构演示资料。",
                    "model_notice": "主动开启后，仅向 DeepSeek 发送有限的症状词、时间和程度片段，不发送身份、表单或完整对话。",
                }, "sources": app.state.knowledge.all(), "visits": visits,
                "current_visit": visits[0] if visits else None,
                "retrieval": retrieval_status(), "bookmark_ids": app.state.store.bookmarks(owner),
                "version": "2.0.0", "safety_review": app.state.safety_review.status()}

    @app.get("/api/knowledge")
    async def knowledge():
        return {"items": app.state.knowledge.all()}

    @app.get("/api/workspace")
    async def workspace(session=Depends(identity)):
        visits = [refresh_collection(visit) for visit in app.state.store.list_visits(session[0])]
        return {"visits_count": len(visits), "messages_count": sum(len(v.messages) for v in visits),
                "observations_count": sum(len(v.observations) for v in visits),
                "ready_count": sum(v.collection.stage == "ready" for v in visits),
                "knowledge_count": len(app.state.knowledge.all()),
                "bookmarks_count": len(app.state.store.bookmarks(session[0])),
                "recent_visits": visits[:5], "retrieval": retrieval_status(),
                "privacy": {"retention_days": retention, "local_default": True}}

    @app.get("/api/library")
    async def library(q: str = Query(default="", max_length=500), category: str = Query(default="", max_length=100),
                      bookmarked: bool = False, session=Depends(identity)):
        items = app.state.knowledge.all()
        categories = sorted({item.category for item in items})
        favorites = app.state.store.bookmarks(session[0])
        if q.strip():
            ids = {s.id for s in (await app.state.retriever.search(q, limit=50)
                                  if app.state.retriever else app.state.knowledge.search(q, limit=50))}
            # Catalog text lookup is explicit browsing, not a medical answer.
            items = [s for s in items if s.id in ids or q.strip().lower() in (s.title + s.content + s.publisher).lower()]
        if category:
            items = [s for s in items if s.category == category]
        if bookmarked:
            items = [s for s in items if s.id in favorites]
        return {"items": items, "categories": categories, "bookmark_ids": favorites,
                "total": len(items), "retrieval": retrieval_status()}

    @app.get("/api/library/evidence")
    async def evidence(q: str = Query(min_length=1, max_length=500), session=Depends(identity)):
        items = await app.state.retriever.search(q) if app.state.retriever else app.state.knowledge.search(q)
        return {"items": items, "retrieval": retrieval_status()}

    @app.get("/api/library/{source_id}")
    async def article(source_id: str, session=Depends(identity)):
        item = next((s for s in app.state.knowledge.all() if s.id == source_id), None)
        if not item:
            raise HTTPException(404, "未找到这条健康资料。")
        return item

    @app.put("/api/library/bookmarks/{source_id}")
    async def bookmark(source_id: str, body: BookmarkUpdate, owner=Depends(mutation)):
        if not any(s.id == source_id for s in app.state.knowledge.all()):
            raise HTTPException(404, "未找到这条健康资料。")
        return {"bookmark_ids": app.state.store.bookmark(owner, source_id, body.saved)}

    @app.post("/api/visits", response_model=Visit, status_code=201)
    async def create(body: CreateVisit, owner=Depends(mutation)):
        async with app.state.commit_lock:
            if len(app.state.store.list_visits(owner)) >= 100:
                raise HTTPException(409, "记录已达 100 条，请先删除不需要的记录。")
            stamp = now()
            visit = Visit(id=uuid4().hex, title=body.title, created_at=stamp, updated_at=stamp,
                          risk=check_urgent("", Intake()))
            refresh_collection(visit)
            app.state.store.put(owner, visit)
        return visit

    @app.get("/api/visits/{visit_id}", response_model=Visit)
    async def get_visit(visit_id: str, session=Depends(identity)):
        return owned(session[0], visit_id)

    @app.patch("/api/visits/{visit_id}/intake", response_model=Visit)
    async def update_intake(visit_id: str, body: Intake, owner=Depends(mutation)):
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            previous_risk = visit.risk
            visit.intake = body
            visit.risk = check_urgent("", body)
            if previous_risk.level == "emergency" and visit.risk.level != "emergency":
                visit.risk = previous_risk
            visit.summary = None
            visit.plan = None
            visit.intake_candidates = []
            refresh_collection(visit)
            visit.updated_at = now()
            app.state.store.put(owner, visit)
        return visit

    @app.post("/api/visits/{visit_id}/messages")
    async def send_message(visit_id: str, body: MessageRequest, owner=Depends(mutation)):
        visit = owned(owner, visit_id)
        if len(visit.messages) >= 200:
            raise HTTPException(409, "本条记录已达到对话上限，请新建记录。")
        # Emergency rules run inside Executor before any model I/O; do not queue that check.
        if check_conversation_urgent(body.content, visit).level != "emergency":
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
                            duration_ms=result.duration_ms, intake_candidates=result.intake_candidates,
                            followup_field=result.followup_field)
        async with app.state.commit_lock:
            latest = owned(owner, visit_id)  # A deleted visit is never recreated by a slow model response.
            if len(latest.messages) >= 200:
                raise HTTPException(409, "本条记录已达到对话上限，请新建记录。")
            changed = (latest.intake != visit.intake or latest.messages != visit.messages
                       or latest.observations != visit.observations or latest.checklist != visit.checklist)
            # Recompute against the latest form after asynchronous model work.
            # A pending answer cannot downgrade a newer warning or dismiss an
            # unresolved emergency reported elsewhere in this same record.
            risk_options = [check_conversation_urgent(body.content, latest)]
            if changed or latest.risk.level == "emergency":
                risk_options.append(latest.risk)
            if not changed or result.risk.level == "emergency":
                risk_options.append(result.risk)
            merged_risk = max(risk_options, key=lambda risk: {"unknown": 0, "prompt": 1, "emergency": 2}[risk.level])
            user.risk = assistant.risk = merged_risk
            if merged_risk.level == "emergency" and result.risk.level != "emergency":
                assistant.content = merged_risk.message + "\n\n" + assistant.content
                assistant.suggested_questions = []
                assistant.followup_field = None
                assistant.intake_candidates = []
                assistant.sources = app.state.knowledge.by_ids(merged_risk.source_ids)
            latest.messages.extend([user, assistant])
            latest.summary = None if changed else result.summary
            latest.plan = None
            # Only retain suggestions still absent in the latest saved form.
            proposed = {c.field: c for c in latest.intake_candidates}
            for candidate in ([] if merged_risk.level == "emergency" else result.intake_candidates):
                if getattr(latest.intake, candidate.field) == getattr(visit.intake, candidate.field):
                    proposed[candidate.field] = candidate
            latest.intake_candidates = list(proposed.values())[:8]
            latest.risk = merged_risk
            if not changed:
                latest.checklist = result.checklist
            latest.updated_at = now()
            refresh_collection(latest)
            app.state.store.put(owner, latest)
        return {"user_message": user, "assistant_message": assistant, "visit": latest}

    @app.post("/api/visits/{visit_id}/summary")
    async def summary(visit_id: str, body: SummaryRequest, owner=Depends(mutation)):
        visit = owned(owner, visit_id)
        rate_limit(owner)
        result = await app.state.executor.run(visit, use_model=body.use_model, purpose="summary")
        async with app.state.commit_lock:
            latest = owned(owner, visit_id)
            if (latest.intake != visit.intake or latest.messages != visit.messages
                    or latest.observations != visit.observations):
                raise HTTPException(409, "资料在整理期间有更新，请重新生成摘要。")
            latest.summary = result.summary
            checked = {item.id: item.done for item in latest.checklist}
            latest.checklist = result.checklist
            for item in latest.checklist:
                item.done = checked.get(item.id, item.done)
            if latest.risk.level != "emergency" or result.risk.level == "emergency":
                latest.risk = result.risk
            latest.updated_at = now()
            refresh_collection(latest)
            app.state.store.put(owner, latest)
        return {"summary": latest.summary, "checklist": latest.checklist, "visit": latest}

    @app.patch("/api/visits/{visit_id}/title", response_model=Visit)
    async def rename(visit_id: str, body: RenameVisit, owner=Depends(mutation)):
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            visit.title = body.title
            visit.updated_at = now()
            app.state.store.put(owner, visit)
        return visit

    @app.post("/api/visits/{visit_id}/candidates/{candidate_id}/confirm", response_model=Visit)
    async def confirm_candidate(visit_id: str, candidate_id: str, owner=Depends(mutation)):
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            candidate = next((c for c in visit.intake_candidates if c.id == candidate_id), None)
            if not candidate:
                raise HTTPException(404, "这条待确认信息已经失效，请重新整理。")
            # Never trust a value from the confirmation request body.
            fields = visit.intake.model_dump()
            fields[candidate.field] = candidate.value
            visit.intake = Intake.model_validate(fields)
            visit.intake_candidates = [c for c in visit.intake_candidates if c.id != candidate_id]
            visit.risk = check_urgent("", visit.intake)
            if visit.risk.level != "emergency":
                # A field confirmation cannot dismiss an unresolved chat warning.
                saved = app.state.store.get(owner, visit_id)
                if saved.risk.level == "emergency":
                    visit.risk = saved.risk
            visit.summary, visit.plan = None, None
            visit.updated_at = now()
            refresh_collection(visit)
            app.state.store.put(owner, visit)
        return visit

    @app.post("/api/visits/{visit_id}/observations", response_model=Visit)
    async def add_observation(visit_id: str, body: ObservationRequest, owner=Depends(mutation)):
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            if len(visit.observations) >= 500:
                raise HTTPException(409, "单条就诊记录的时间线已满，请新建记录。")
            item = Observation(id=uuid4().hex, created_at=now(), **body.model_dump())
            visit.observations.append(item)
            visit.observations.sort(key=lambda o: datetime.fromisoformat(o.recorded_at))
            if item.kind == "symptom":
                age = datetime.now(timezone.utc) - datetime.fromisoformat(item.recorded_at)
                context = "既往观察：" if age > timedelta(hours=24) else "当前观察："
                risk = check_urgent(context + item.value + "，" + item.notes, visit.intake)
                if visit.risk.level != "emergency" or risk.level == "emergency":
                    visit.risk = risk
            visit.summary, visit.plan = None, None
            visit.updated_at = now()
            refresh_collection(visit)
            app.state.store.put(owner, visit)
        return visit

    @app.delete("/api/visits/{visit_id}/observations/{observation_id}", response_model=Visit)
    async def delete_observation(visit_id: str, observation_id: str, owner=Depends(mutation)):
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            if not any(o.id == observation_id for o in visit.observations):
                raise HTTPException(404, "未找到这条时间线记录。")
            visit.observations = [o for o in visit.observations if o.id != observation_id]
            visit.summary, visit.plan = None, None
            visit.updated_at = now()
            app.state.store.put(owner, visit)
        return visit

    @app.post("/api/visits/{visit_id}/plan")
    async def plan(visit_id: str, owner=Depends(mutation)):
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            visit.plan = await app.state.executor.execute("build_preparation_plan", visit=visit)
            visit.updated_at = now()
            app.state.store.put(owner, visit)
        return {"plan": visit.plan, "visit": visit}

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

    register_task_routes(app, owned, identity, mutation, rate_limit, refresh_collection)

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
