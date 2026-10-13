"""Asynchronous, owned task jobs. Tool results are drafts until confirmed."""
from __future__ import annotations

import asyncio
import hashlib
import json
import inspect
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from typing import Literal
from uuid import uuid4

from fastapi import Depends, HTTPException
from pydantic import Field, ValidationError

from .schemas import Risk, StrictModel, Visit
from .storage import now
from .task_agent import TaskGoal, TaskRun
from .task_storage import TaskIntegrityError
from .knowledge_versions import knowledge_version
from .risk_policy import requires_human_support, support_status
from .production_runtime import RuntimeUnavailable
from .data_crypto import DataIntegrityError


class PrivateTaskInput(StrictModel):
    snapshot: Visit
    use_model: bool = False


class PersistentTaskRepository:
    """Encrypted task input and transactional outbox; Redis holds an opaque ID.

    A fencing token guards the final write even after an expired Redis lease.
    Both foreign keys cascade when a visit/task is deleted or consent withdrawn.
    The database adapter serializes short transactions across configured workers.
    """
    def __init__(self, store, tasks):
        self.store, self.tasks = store, tasks
        with store.lock, store.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS persistent_task_inputs (task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, owner TEXT NOT NULL REFERENCES sessions(id), visit_id TEXT NOT NULL REFERENCES visits(id) ON DELETE CASCADE, payload TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN (\'queued\',\'running\')), claim_token TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, dispatched_at TEXT)')
            conn.execute('CREATE INDEX IF NOT EXISTS persistent_task_active ON persistent_task_inputs(owner,visit_id,state)')

    @staticmethod
    def _scope(conn, store, owner: str, scope: str, required: bool) -> bool:
        if not required:
            return True
        from .privacy import NOTICE_VERSION
        row = conn.execute('SELECT event_id,payload FROM privacy_current WHERE owner=?', (owner,)).fetchone()
        state = store.open_value('privacy_current', owner, row[0], row[1]) if row else {}
        return bool(state.get('notice_version') == NOTICE_VERSION and state.get('local_storage') and (scope == 'local_storage' or state.get('external_ai')))

    def _job(self, conn, owner, visit_id, task_id):
        row = conn.execute('SELECT payload FROM tasks WHERE id=? AND owner=? AND visit_id=?', (task_id, owner, visit_id)).fetchone()
        return TaskJob.model_validate(self.tasks._open(owner, task_id, visit_id, row[0])) if row else None

    def _save(self, conn, owner: str, job, action: str) -> None:
        conn.execute('UPDATE tasks SET payload=?,updated_at=? WHERE id=? AND owner=? AND visit_id=?', (self.tasks._seal(owner, job.model_dump(mode='json')), job.updated_at, job.id, owner, job.visit_id))
        self.tasks._audit(conn, owner, job.model_dump(mode='json'), action)

    def admit(self, owner: str, job, snapshot: Visit, use_model: bool, *, consent_required=False) -> None:
        with self.store.lock, self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT payload FROM visits WHERE id=? AND owner=?', (snapshot.id, owner)).fetchone()
            if not row:
                raise HTTPException(404, '记录已删除，未创建任务。')
            latest = self.store.decode_visit(owner, snapshot.id, row[0])
            if revision(latest) != job.source_revision:
                raise HTTPException(409, '资料在排队前已变化，请重新运行任务。')
            if not self._scope(conn, self.store, owner, 'local_storage', consent_required):
                raise HTTPException(403, '保存同意已撤回，未创建任务。')
            if use_model and not requires_human_support(latest.risk) and not self._scope(conn, self.store, owner, 'external_ai', consent_required):
                raise HTTPException(403, '尚未单独同意外部AI处理，未发送资料。')
            if conn.execute('SELECT 1 FROM persistent_task_inputs WHERE owner=? AND visit_id=?', (owner, snapshot.id)).fetchone():
                raise HTTPException(409, '这条记录已有任务正在运行，请等待结果。')
            if conn.execute('SELECT COUNT(*) FROM persistent_task_inputs').fetchone()[0] >= 8:
                raise HTTPException(429, '当前准备任务较多，请稍后重试。')
            if conn.execute('SELECT COUNT(*) FROM tasks WHERE owner=? AND visit_id=?', (owner, snapshot.id)).fetchone()[0] >= 50:
                raise HTTPException(409, '这条记录的任务已达上限，请新建记录。')
            data = PrivateTaskInput(snapshot=snapshot, use_model=use_model).model_dump(mode='json')
            payload = self.store.seal_value('persistent_task_input', owner, job.id, data)
            conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', (job.id, owner, snapshot.id, self.tasks._seal(owner, job.model_dump(mode='json')), job.updated_at))
            self.tasks._audit(conn, owner, job.model_dump(mode='json'), 'task_queued')
            conn.execute('INSERT INTO persistent_task_inputs(task_id,owner,visit_id,payload,state,updated_at) VALUES(?,?,?,?,?,?)', (job.id, owner, snapshot.id, payload, 'queued', now()))

    def dispatch_due(self) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(timespec='seconds')
        with self.store.connect() as conn:
            rows = conn.execute('SELECT task_id FROM persistent_task_inputs WHERE (state=? AND (dispatched_at IS NULL OR dispatched_at<?)) OR (state=? AND updated_at<?) ORDER BY updated_at LIMIT 8', ('queued', cutoff, 'running', cutoff)).fetchall()
        return [row[0] for row in rows]

    def dispatched(self, task_id):
        with self.store.lock, self.store.connect() as conn:
            conn.execute('UPDATE persistent_task_inputs SET dispatched_at=? WHERE task_id=?', (now(), task_id))

    def claim(self, task_id: str, token: str, *, consent_required=False):
        with self.store.lock, self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT owner,visit_id,payload FROM persistent_task_inputs WHERE task_id=?', (task_id,)).fetchone()
            if not row:
                return None
            owner, visit_id, payload = row
            job = self._job(conn, owner, visit_id, task_id)
            current = conn.execute('SELECT payload FROM visits WHERE id=? AND owner=?', (visit_id, owner)).fetchone()
            if not job or not current or job.status not in {'queued', 'running'}:
                conn.execute('DELETE FROM persistent_task_inputs WHERE task_id=?', (task_id,))
                return None
            latest = self.store.decode_visit(owner, visit_id, current[0])
            if not self._scope(conn, self.store, owner, 'local_storage', consent_required):
                return self._reject(conn, owner, job, '保存同意已变化，任务未执行。')
            if not requires_human_support(latest.risk) and revision(latest) != job.source_revision:
                return self._reject(conn, owner, job, '原始资料已更新，请重新运行任务。', status='superseded')
            try:
                private = PrivateTaskInput.model_validate(self.store.open_value('persistent_task_input', owner, task_id, payload))
                if private.snapshot.id != visit_id or revision(private.snapshot) != job.source_revision:
                    raise TaskIntegrityError('任务快照和启动修订不一致。')
            except (DataIntegrityError, TaskIntegrityError, ValidationError):
                return self._reject(conn, owner, job, '任务输入完整性校验失败，未执行损坏快照。')
            use_model = private.use_model and not requires_human_support(latest.risk)
            if use_model and not self._scope(conn, self.store, owner, 'external_ai', consent_required):
                return self._reject(conn, owner, job, '外部AI同意已撤回，任务未发送资料。')
            snapshot = latest if requires_human_support(latest.risk) else private.snapshot
            conn.execute('UPDATE persistent_task_inputs SET state=?,claim_token=?,attempts=attempts+1,updated_at=? WHERE task_id=?', ('running', token, now(), task_id))
            job.status, job.error, job.updated_at = 'running', None, now()
            self._save(conn, owner, job, 'task_started')
            return owner, job, snapshot, use_model

    def _reject(self, conn, owner, job, message, status='failed'):
        job.status, job.error, job.updated_at, job.result = status, message, now(), None
        self._save(conn, owner, job, 'task_rejected')
        conn.execute('DELETE FROM persistent_task_inputs WHERE task_id=?', (job.id,))
        return None

    def finish(self, owner, job, token, result, *, use_model=False, consent_required=False):
        with self.store.lock, self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT claim_token FROM persistent_task_inputs WHERE task_id=? AND owner=? AND visit_id=?', (job.id, owner, job.visit_id)).fetchone()
            if not row:
                return 'gone', None
            if row[0] != token:
                return 'fenced', None
            current = conn.execute('SELECT payload FROM visits WHERE id=? AND owner=?', (job.visit_id, owner)).fetchone()
            if not current:
                return 'gone', None
            latest = self.store.decode_visit(owner, job.visit_id, current[0])
            if not self._scope(conn, self.store, owner, 'local_storage', consent_required):
                self._reject(conn, owner, job, '保存同意已变化，任务结果未保存。')
                return 'done', None
            if requires_human_support(latest.risk) and (not result or result.status not in {'emergency', 'support_required'}):
                return 'safety', latest
            if use_model and not self._scope(conn, self.store, owner, 'external_ai', consent_required):
                self._reject(conn, owner, job, '外部AI同意已撤回，任务结果未保存。')
                return 'done', None
            job.updated_at = now()
            if result:
                job.result, job.status = result, result.status
                if revision(latest) != job.source_revision and result.status not in {'emergency', 'support_required'}:
                    job.status, job.error = 'superseded', '原始资料已更新，请重新运行后保存。'
            else:
                job.status, job.error, job.result = 'failed', '任务未能完成，原始资料没有修改。请重试。', None
            self._save(conn, owner, job, 'task_finished' if result else 'task_failed')
            conn.execute('DELETE FROM persistent_task_inputs WHERE task_id=? AND claim_token=?', (job.id, token))
            return 'done', None

    def requeue(self, task_id, token):
        with self.store.lock, self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT owner,visit_id FROM persistent_task_inputs WHERE task_id=? AND claim_token=?', (task_id, token)).fetchone()
            if not row:
                return
            conn.execute('UPDATE persistent_task_inputs SET state=?,claim_token=NULL,dispatched_at=NULL,updated_at=? WHERE task_id=? AND claim_token=?', ('queued', now(), task_id, token))
            job = self._job(conn, row[0], row[1], task_id)
            if job:
                job.status, job.updated_at = 'queued', now()
                self._save(conn, row[0], job, 'task_requeued')


def persistent_repository(app):
    if not getattr(app.state, 'persistent_tasks', None):
        app.state.persistent_tasks = PersistentTaskRepository(app.state.store, app.state.task_store)
    return app.state.persistent_tasks


def authorized_run(app, owner, visit_id):
    privacy = getattr(app.state, 'privacy', None)
    if privacy is None:
        return nullcontext()
    from .egress import authorized_egress
    return authorized_egress(privacy, owner, visit_id)


async def reconcile_outbox(app) -> int:
    repository, runtime = persistent_repository(app), app.state.queue_runtime
    dispatched = 0
    for task_id in repository.dispatch_due():
        await runtime.enqueue_task(task_id)
        repository.dispatched(task_id)
        dispatched += 1
    return dispatched


async def process_delivery(app, delivery) -> bool:
    """At-least-once delivery with an encrypted snapshot and fenced DB commit."""
    runtime, repository = app.state.queue_runtime, persistent_repository(app)
    token = await runtime.claim(delivery.task_id, lease_seconds=120)
    if not token:
        return False
    required = getattr(getattr(app.state, 'privacy', None), 'required', False)
    try:
        claimed = repository.claim(delivery.task_id, token, consent_required=required)
        if not claimed:
            await runtime.acknowledge(delivery.message_id)
            return True
        owner, job, snapshot, use_model = claimed
        try:
            with authorized_run(app, owner, snapshot.id):
                result = await asyncio.wait_for(app.state.task_engine.run(snapshot, job.goal, use_model=use_model), timeout=85)
            result.id = job.id
        except asyncio.CancelledError:
            repository.requeue(job.id, token)
            raise
        except Exception:
            result = None
        for _ in range(3):
            outcome, latest = repository.finish(owner, job, token, result, use_model=use_model, consent_required=required)
            if outcome != 'safety':
                break
            result = await app.state.task_engine.run(latest, job.goal, use_model=False)
            result.id = job.id
            use_model = False
        else:
            raise RuntimeUnavailable('Safety state changed repeatedly; task was not acknowledged.')
        if outcome == 'fenced':
            return False
        await runtime.acknowledge(delivery.message_id)
        return True
    finally:
        await runtime.release(delivery.task_id, token)


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
    async def limit(owner):
        # The production gate is async; old injected local test gates remain safe.
        decision = rate_limit(owner)
        if inspect.isawaitable(decision):
            await decision

    def consent(owner, visit, use_model=False):
        privacy = getattr(app.state, 'privacy', None)
        if privacy:
            privacy.require(owner)
            if use_model and not requires_human_support(visit.risk):
                privacy.require(owner, 'external_ai')
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
                    latest = app.state.store.get(owner, snapshot.id)
                    if latest is None:
                        return
                    consent(owner, latest, use_model)
                    if requires_human_support(latest.risk):
                        snapshot, use_model = latest, False
                    job.status, job.updated_at = 'running', now()
                    app.state.task_store.put(owner, job.model_dump(mode='json'), 'task_started')
                with authorized_run(app, owner, snapshot.id):
                    result = await asyncio.wait_for(app.state.task_engine.run(snapshot, job.goal, use_model=use_model), timeout=85)
                result.id = job.id
                async with app.state.commit_lock:
                    latest = app.state.store.get(owner, snapshot.id)
                    if latest is None:
                        return
                    consent(owner, latest, use_model)
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
        await limit(owner)
        async with app.state.commit_lock:
            visit = owned(owner, visit_id)
            consent(owner, visit, body.use_model)
            runtime = getattr(app.state, 'queue_runtime', None)
            if runtime:
                stamp = now()
                job = TaskJob(id=uuid4().hex, visit_id=visit_id, goal=body.goal, created_at=stamp, updated_at=stamp, source_revision=revision(visit))
                repository = persistent_repository(app)
                required = getattr(getattr(app.state, 'privacy', None), 'required', False)
                repository.admit(owner, job, visit.model_copy(deep=True), body.use_model, consent_required=required)
                try:
                    await runtime.enqueue_task(job.id)
                    repository.dispatched(job.id)
                except RuntimeUnavailable:
                    raise HTTPException(503, '任务已保存，但持久队列暂不可用。请查看任务列表，服务恢复后继续执行；不要重复提交。') from None
                return {'task': job}
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
            consent(owner, visit)
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
