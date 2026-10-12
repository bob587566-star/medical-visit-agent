"""Asynchronous, owned task jobs. Tool results are drafts until confirmed."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Literal
from uuid import uuid4

from fastapi import Depends, HTTPException
from pydantic import Field

from .schemas import Risk, StrictModel, Visit
from .storage import now
from .task_agent import TaskGoal, TaskRun
from .task_storage import TaskIntegrityError
from .knowledge_versions import knowledge_version
from .risk_policy import requires_human_support, support_status


class TaskRequest(StrictModel):
    goal: TaskGoal
    use_model: bool = False


class TaskJob(StrictModel):
    id: str
    visit_id: str
    goal: TaskGoal
    status: Literal['queued', 'running', 'completed', 'needs_input', 'emergency', 'support_required', 'failed', 'interrupted', 'superseded'] = 'queued'
    created_at: str
    updated_at: str
    source_revision: str
    applied_at: str | None = None
    result: TaskRun | None = None
    error: str | None = None
    current_risk: Risk | None = None
    invalidated_reason: str | None = None
    can_apply: bool = False


def revision(visit: Visit) -> str:
    fields = visit.model_dump(mode='json', include={'intake', 'messages', 'observations', 'risk', 'intake_candidates', 'checklist'})
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def register_task_routes(app, owned, identity, mutation, rate_limit, refresh_collection):
    def assess_job(visit: Visit, job: TaskJob) -> TaskJob:
        # Keep the encrypted execution result historical, but expose current
        # eligibility separately; old successful output cannot overrule safety.
        job.current_risk = visit.risk
        if requires_human_support(visit.risk):
            job.status = support_status(visit.risk)
            job.invalidated_reason = '当前记录需要立即关注安全，已暂停使用原任务结果。'
            job.can_apply = False
        elif revision(visit) != job.source_revision and not job.applied_at:
            job.status = 'superseded'
            job.invalidated_reason = '原始资料已更新，这份任务结果已过期，请重新运行。'
            job.can_apply = False
        else:
            job.can_apply = job.status in {'completed', 'needs_input'} and bool(job.result and job.result.summary) and not job.applied_at
        return job

    def read_job(owner: str, visit_id: str, task_id: str) -> TaskJob:
        visit = owned(owner, visit_id)
        raw = app.state.task_store.get(owner, visit_id, task_id)
        if not raw:
            raise HTTPException(404, '未找到这项任务。')
        return assess_job(visit, TaskJob.model_validate(raw))

    async def worker(owner: str, job: TaskJob, snapshot: Visit, use_model: bool):
        try:
            async with app.state.task_slots:
                async with app.state.commit_lock:
                    if app.state.store.get(owner, snapshot.id) is None:
                        return
                    job.status, job.updated_at = 'running', now()
                    app.state.task_store.put(owner, job.model_dump(mode='json'), 'task_started')
                result = await asyncio.wait_for(app.state.task_engine.run(snapshot, job.goal, use_model=use_model), timeout=85)
                result.id = job.id
                async with app.state.commit_lock:
                    latest = app.state.store.get(owner, snapshot.id)
                    if latest is None:
                        return
                    # Fresh emergency information takes priority over a result
                    # drafted from an earlier snapshot, without model I/O.
                    if requires_human_support(latest.risk) and result.status not in {'emergency', 'support_required'}:
                        result = await app.state.task_engine.run(latest, job.goal, use_model=False)
                        result.id = job.id
                    job.result, job.status = result, result.status
                    if revision(latest) != job.source_revision and result.status not in {'emergency', 'support_required'}:
                        job.error = '本次任务基于启动时的资料；记录已更新，请重新运行后保存。'
                    job.updated_at = now()
                    app.state.task_store.put(owner, job.model_dump(mode='json'), 'task_finished')
        except asyncio.CancelledError:
            job.status, job.error, job.updated_at = 'interrupted', '服务停止，上一次任务未完成。请重新运行。', now()
            app.state.task_store.put(owner, job.model_dump(mode='json'), 'task_interrupted')
            raise
        except Exception:
            job.status, job.error, job.updated_at = 'failed', '任务未能完成，原始资料没有修改。请重试。', now()
            app.state.task_store.put(owner, job.model_dump(mode='json'), 'task_failed')

    @app.exception_handler(TaskIntegrityError)
    async def integrity_error(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': '任务或审计记录完整性校验失败，未使用损坏内容。'}, status_code=503)

    @app.post('/api/visits/{visit_id}/tasks', status_code=202)
    async def start_task(visit_id: str, body: TaskRequest, owner=Depends(mutation)):
        rate_limit(owner)
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            existing = app.state.task_store.list(owner, visit_id)
            if any(task['status'] in {'queued', 'running'} for task in existing):
                raise HTTPException(409, '这条记录已有任务正在运行，请等待结果。')
            if len(existing) >= 50:
                raise HTTPException(409, '这条记录的任务已达上限，请新建记录。')
            if len(app.state.task_workers) >= 8:
                raise HTTPException(429, '当前准备任务较多，请稍后重试。')
            stamp = now()
            job = TaskJob(id=uuid4().hex, visit_id=visit_id, goal=body.goal, created_at=stamp, updated_at=stamp, source_revision=revision(visit))
            app.state.task_store.put(owner, job.model_dump(mode='json'), 'task_queued')
            task = asyncio.create_task(worker(owner, job.model_copy(deep=True), visit.model_copy(deep=True), body.use_model))
            app.state.task_workers[job.id] = task
            task.add_done_callback(lambda finished, identifier=job.id: app.state.task_workers.pop(identifier, None))
        return {'task': job}

    @app.get('/api/visits/{visit_id}/tasks')
    async def list_tasks(visit_id: str, session=Depends(identity)):
        visit = owned(session[0], visit_id)
        return {'items': [assess_job(visit, TaskJob.model_validate(raw)) for raw in app.state.task_store.list(session[0], visit_id)]}

    @app.get('/api/visits/{visit_id}/tasks/{task_id}', response_model=TaskJob)
    async def task_detail(visit_id: str, task_id: str, session=Depends(identity)):
        return read_job(session[0], visit_id, task_id)

    @app.post('/api/visits/{visit_id}/tasks/{task_id}/apply')
    async def apply_task(visit_id: str, task_id: str, owner=Depends(mutation)):
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            job = read_job(owner, visit_id, task_id)
            if requires_human_support(visit.risk):
                raise HTTPException(409, '当前记录需要立即关注安全，已暂停保存准备任务结果。')
            if job.applied_at:
                raise HTTPException(409, '这份结果已经保存，请查看当前记录。')
            if revision(visit) != job.source_revision:
                raise HTTPException(409, '资料在任务生成后已更新，请重新运行任务，避免覆盖新内容。')
            if job.status not in {'completed', 'needs_input'} or not job.result or not job.result.summary:
                raise HTTPException(409, '当前任务没有可保存的完整准备草稿。')
            if job.result.knowledge_version != knowledge_version(app.state.knowledge):
                raise HTTPException(409, '健康资料版本已更新，请重新运行任务后保存。')
            visit.summary = job.result.summary
            visit.checklist = job.result.checklist or visit.checklist
            visit.plan = job.result.preparation_plan
            visit.updated_at = now()
            refresh_collection(visit)
            job.applied_at = job.updated_at = now()
            if not app.state.task_store.apply(owner, visit, job.model_dump(mode='json')):
                raise HTTPException(404, '记录已删除，未保存任务结果。')
        return {'task': job, 'visit': visit}

    @app.get('/api/tasks/audit')
    async def audit(session=Depends(identity)):
        return app.state.task_store.audit(session[0])
