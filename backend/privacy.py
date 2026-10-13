"""Separate storage/external-processing consent and owned data rights."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from uuid import uuid4

from fastapi import Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .storage import now

NOTICE_VERSION = '2026-10-13.v1'
NOTICE_TEXT = (
    '健康自述、既往情况、用药和聊天可能包含敏感个人信息。本工具用于就诊准备和健康资料学习，不提供诊断、处方或医师接诊。'
    '同意本机保存后，表单、聊天和任务将加密保存在当前服务的数据存储中，仅供本访客或已绑定账号使用。'
    '你可选择1、7或30天留存，并可导出、删除或撤回；撤回会停止处理并删除本服务中的记录与任务。'
    '撤回或删除不能自动删除你已下载的文件、运维备份或服务商既有数据。请优先使用虚构资料演示。'
    '外部AI处理须另行选择：仅向DeepSeek发送受限症状片段，或任务目标、已填字段名称及观察数量等元数据；'
    '不同请求的范围在页面说明，不发送身份、Cookie或完整表单。当前未作为真实患者系统部署，不能凭这些技术防护认定医疗合规。'
)


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    notice_version: str = Field(max_length=80)
    local_storage: bool
    external_ai: bool = False
    retention_days: int = Field(default=7, ge=1, le=30)


class Privacy:
    def __init__(self, store, required=True):
        self.store, self.required = store, bool(required)
        with store.lock, store.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS privacy_events (id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id), payload TEXT NOT NULL, created_at TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS privacy_current (owner TEXT PRIMARY KEY REFERENCES sessions(id), event_id TEXT NOT NULL REFERENCES privacy_events(id), payload TEXT NOT NULL)')

    def status(self, owner):
        with self.store.connect() as conn:
            row = conn.execute('SELECT event_id,payload FROM privacy_current WHERE owner=?', (owner,)).fetchone()
        state = self.store.open_value('privacy_current', owner, row[0], row[1]) if row else {}
        accepted = bool(state.get('local_storage') and state.get('notice_version') == NOTICE_VERSION)
        return {'consent_required': self.required, 'accepted': accepted,
                'notice_version': NOTICE_VERSION, 'notice_text': NOTICE_TEXT,
                'scopes': {'local_storage': accepted, 'external_ai': accepted and bool(state.get('external_ai'))},
                'retention_days': state.get('retention_days', 7), 'accepted_at': state.get('accepted_at'),
                'local_default': True, 'notice': '健康记录采用加密存储；请使用虚构演示资料。',
                'model_notice': '单独同意后还需主动打开模型开关。仅发送受限片段或规划元数据，不发送身份或完整对话。'}

    def require(self, owner, scope='local_storage'):
        if self.required and not self.status(owner)['scopes'].get(scope):
            raise HTTPException(403, '请先阅读隐私告知并单独同意本机留存。' if scope == 'local_storage' else '尚未单独同意外部AI处理，未发送资料。')

    def _write(self, owner, state, action):
        identifier, stamp = uuid4().hex, now()
        with self.store.lock, self.store.connect() as conn:
            event = {'action': action, 'notice_version': NOTICE_VERSION, 'at': stamp,
                     'local_storage': state['local_storage'], 'external_ai': state['external_ai'], 'retention_days': state['retention_days']}
            conn.execute('INSERT INTO privacy_events VALUES (?,?,?,?)', (identifier, owner, self.store.seal_value('privacy_event', owner, identifier, event), stamp))
            conn.execute('INSERT INTO privacy_current VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET event_id=excluded.event_id,payload=excluded.payload', (owner, identifier, self.store.seal_value('privacy_current', owner, identifier, state)))

    def grant(self, owner, body):
        if body.notice_version != NOTICE_VERSION:
            raise HTTPException(409, '隐私告知版本已变化，请重新阅读。')
        if not body.local_storage or body.retention_days not in {1, 7, 30}:
            raise HTTPException(422, '本机留存须明确同意，留存期请选择1、7或30天。')
        self._write(owner, {'notice_version': NOTICE_VERSION, 'local_storage': True, 'external_ai': body.external_ai,
                            'retention_days': body.retention_days, 'accepted_at': now()}, 'consent_granted')
        return self.status(owner)

    def withdraw(self, owner):
        # Clear health records but retain minimal encrypted consent history.
        # Both actions commit together; no accepted state survives a withdrawal.
        identifier, stamp = uuid4().hex, now()
        state = {'notice_version': NOTICE_VERSION, 'local_storage': False, 'external_ai': False, 'retention_days': 7, 'accepted_at': None}
        with self.store.lock, self.store.connect() as conn:
            conn.execute('DELETE FROM visits WHERE owner=?', (owner,))
            conn.execute('DELETE FROM bookmarks WHERE owner=?', (owner,))
            for table in ('task_audit', 'task_audit_heads'):
                conn.execute(f'DELETE FROM {table} WHERE owner=?', (owner,))
            event = {'action': 'consent_withdrawn_and_data_deleted', 'notice_version': NOTICE_VERSION, 'at': stamp, 'local_storage': False, 'external_ai': False}
            conn.execute('INSERT INTO privacy_events VALUES (?,?,?,?)', (identifier, owner, self.store.seal_value('privacy_event', owner, identifier, event), stamp))
            conn.execute('INSERT INTO privacy_current VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET event_id=excluded.event_id,payload=excluded.payload', (owner, identifier, self.store.seal_value('privacy_current', owner, identifier, state)))
        return self.status(owner)

    def history(self, owner):
        with self.store.connect() as conn:
            rows = conn.execute('SELECT id,payload FROM privacy_events WHERE owner=? ORDER BY created_at,id', (owner,)).fetchall()
        return [self.store.open_value('privacy_event', owner, row[0], row[1]) for row in rows]

    def expire(self):
        with self.store.connect() as conn:
            rows = conn.execute('SELECT owner,event_id,payload FROM privacy_current').fetchall()
        for owner, identifier, payload in rows:
            state = self.store.open_value('privacy_current', owner, identifier, payload)
            days = state.get('retention_days', 7)
            if type(days) is not int or not 1 <= days <= 30:
                raise ValueError('隐私留存设置无效。')
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')
            with self.store.lock, self.store.connect() as conn:
                conn.execute('DELETE FROM visits WHERE owner=? AND updated_at<?', (owner, cutoff))


def public_compliance(config):
    def value(key):
        text = config.get(key, '').strip()
        return text[:250] if text else None
    def url(key):
        text = value(key)
        return text if text and re.fullmatch(r'https://[^\s@]+', text) else None
    records = []
    for kind, label, key in (('provider_model', '模型供应商备案信息', 'PROVIDER_MODEL_FILING'), ('application', '本应用登记信息', 'APPLICATION_REGISTRATION'), ('algorithm', '适用算法备案信息', 'ALGORITHM_FILING')):
        number = value(key + '_NUMBER')
        model_name = config.get('DEEPSEEK_MODEL', 'deepseek-chat') if kind == 'provider_model' else None
        if model_name is not None and not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', model_name):
            model_name = 'deepseek-chat'
        records.append({'type': kind, 'label': label, 'model_name': model_name, 'number': number,
                        'url': url(key + '_URL'), 'status': 'configured' if number else 'unconfigured'})
    return {'operator': {'name': value('PUBLIC_OPERATOR'), 'contact_url': url('PUBLIC_CONTACT_URL'), 'privacy_url': url('PUBLIC_PRIVACY_URL')},
            'product_name': '知禾', 'positioning': '就诊准备与健康资料', 'public_service_status': 'unconfigured',
            'records': records, 'notice': '配置展示信息不代表备案、等保、医疗资质或临床认证已完成。'}


def register_privacy_routes(app, identity, mutation):
    @app.get('/api/privacy')
    async def privacy(session=Depends(identity)):
        return app.state.privacy.status(session[0])

    @app.post('/api/privacy/consent')
    async def consent(body: ConsentRequest, owner=Depends(mutation)):
        return app.state.privacy.grant(owner, body)

    @app.delete('/api/privacy/consent')
    async def withdraw(owner=Depends(mutation)):
        async with app.state.commit_lock:
            return app.state.privacy.withdraw(owner)

    @app.get('/api/privacy/export')
    async def export(session=Depends(identity)):
        owner = session[0]
        visits = app.state.store.list_visits(owner)
        tasks = []
        for visit in visits:
            tasks.extend(app.state.task_store.list(owner, visit.id))
        result = {'format_version': 1, 'exported_at': now(), 'notice': '含本人自述及AI辅助整理结果，不构成诊断或处方。',
                  'ai_content_metadata': {'contains_ai_assisted_content': True, 'provider': '知禾', 'content_id': uuid4().hex},
                  'account': app.state.accounts.public(owner), 'visits': [visit.model_dump(mode='json') for visit in visits],
                  'tasks': tasks, 'bookmark_ids': app.state.store.bookmarks(owner), 'privacy': app.state.privacy.status(owner),
                  'consent_history': app.state.privacy.history(owner)}
        return Response(json.dumps(result, ensure_ascii=False), media_type='application/json; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="zhihe-personal-data.json"'})
