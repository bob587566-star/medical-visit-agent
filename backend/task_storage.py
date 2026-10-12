"""Visitor-owned encrypted task results and a locally verifiable audit chain."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .storage import Store, now


class TaskIntegrityError(ValueError):
    pass


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class TaskStore:
    def __init__(self, store: Store):
        self.store = store
        key_path = Path(store.path).with_suffix('.tasks.key')
        with store.lock, store.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES sessions(id), visit_id TEXT NOT NULL REFERENCES visits(id) ON DELETE CASCADE, payload TEXT NOT NULL, updated_at TEXT NOT NULL)')
            conn.execute('CREATE INDEX IF NOT EXISTS tasks_owner_visit ON tasks(owner,visit_id)')
            conn.execute('CREATE TABLE IF NOT EXISTS task_audit (owner TEXT NOT NULL REFERENCES sessions(id), seq INTEGER NOT NULL, event TEXT NOT NULL, mac TEXT NOT NULL, PRIMARY KEY(owner,seq))')
            conn.execute('CREATE TABLE IF NOT EXISTS task_audit_heads (owner TEXT PRIMARY KEY REFERENCES sessions(id), seq INTEGER NOT NULL, mac TEXT NOT NULL)')
            has_records = bool(conn.execute('SELECT 1 FROM tasks LIMIT 1').fetchone() or conn.execute('SELECT 1 FROM task_audit_heads LIMIT 1').fetchone())
        if not key_path.exists():
            if has_records:
                raise TaskIntegrityError('任务解密密钥缺失，未覆盖已有任务。请恢复原密钥。')
            try:
                with key_path.open('xb') as file:
                    file.write(Fernet.generate_key())
                key_path.chmod(0o600)
            except FileExistsError:
                pass
        key = key_path.read_bytes()
        self.cipher = Fernet(key)
        self.audit_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b'zhihe-task-audit-v1').derive(base64.urlsafe_b64decode(key))

    def _seal(self, owner: str, task: dict) -> str:
        return self.cipher.encrypt(canonical({'owner': owner, 'task': task}).encode()).decode()

    def _open(self, owner: str, task_id: str, visit_id: str, payload: str) -> dict:
        try:
            value = json.loads(self.cipher.decrypt(payload.encode()))
            task = value['task']
            if value['owner'] != owner or task['id'] != task_id or task['visit_id'] != visit_id:
                raise TaskIntegrityError('任务归属或标识校验失败。')
            return task
        except (InvalidToken, ValueError, TypeError, KeyError) as error:
            raise TaskIntegrityError('任务内容完整性校验失败，未使用损坏结果。') from error

    def _audit(self, conn, owner: str, task: dict, action: str):
        head = conn.execute('SELECT seq,mac FROM task_audit_heads WHERE owner=?', (owner,)).fetchone()
        seq, previous = (head[0] + 1, head[1]) if head else (1, '')
        event = canonical({'owner': owner, 'at': now(), 'task_id': task['id'], 'visit_id': task['visit_id'], 'action': action, 'status': task['status'], 'result_hash': hashlib.sha256(canonical(task).encode()).hexdigest(), 'previous': previous})
        mac = hmac.new(self.audit_key, (previous + '\n' + event).encode(), hashlib.sha256).hexdigest()
        conn.execute('INSERT INTO task_audit VALUES (?,?,?,?)', (owner, seq, event, mac))
        conn.execute('INSERT INTO task_audit_heads VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET seq=excluded.seq,mac=excluded.mac', (owner, seq, mac))

    def put(self, owner: str, task: dict, action: str = 'task_updated') -> bool:
        with self.store.lock, self.store.connect() as conn:
            if not conn.execute('SELECT 1 FROM visits WHERE id=? AND owner=?', (task['visit_id'], owner)).fetchone():
                return False  # Deleted records are never recreated by a worker.
            changed = conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at WHERE tasks.owner=excluded.owner AND tasks.visit_id=excluded.visit_id', (task['id'], owner, task['visit_id'], self._seal(owner, task), task['updated_at'])).rowcount
            if changed:
                self._audit(conn, owner, task, action)
            return bool(changed)

    def get(self, owner: str, visit_id: str, task_id: str) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute('SELECT payload FROM tasks WHERE id=? AND visit_id=? AND owner=?', (task_id, visit_id, owner)).fetchone()
        return self._open(owner, task_id, visit_id, row[0]) if row else None

    def apply(self, owner: str, visit, task: dict) -> bool:
        """Commit the confirmed draft, task marker and audit event together."""
        with self.store.lock, self.store.connect() as conn:
            if not conn.execute('SELECT 1 FROM tasks WHERE id=? AND owner=? AND visit_id=?', (task['id'], owner, visit.id)).fetchone():
                return False
            changed = conn.execute('UPDATE visits SET payload=?,updated_at=? WHERE id=? AND owner=?', (visit.model_dump_json(), visit.updated_at, visit.id, owner)).rowcount
            if not changed:
                return False
            conn.execute('UPDATE tasks SET payload=?,updated_at=? WHERE id=? AND owner=?', (self._seal(owner, task), task['updated_at'], task['id'], owner))
            self._audit(conn, owner, task, 'user_confirmed_apply')
            return True

    def list(self, owner: str, visit_id: str) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute('SELECT id,payload FROM tasks WHERE owner=? AND visit_id=? ORDER BY updated_at DESC,id DESC', (owner, visit_id)).fetchall()
        return [self._open(owner, row[0], visit_id, row[1]) for row in rows]

    def interrupt_unfinished(self):
        with self.store.connect() as conn:
            rows = conn.execute('SELECT owner,visit_id,id,payload FROM tasks').fetchall()
        for owner, visit_id, task_id, payload in rows:
            task = self._open(owner, task_id, visit_id, payload)
            if task['status'] in {'queued', 'running'}:
                task.update(status='interrupted', updated_at=now(), error='服务已重启，上一次任务未完成。请重新运行。')
                self.put(owner, task, 'service_restart_interrupted')

    def audit(self, owner: str) -> dict:
        with self.store.connect() as conn:
            rows = conn.execute('SELECT seq,event,mac FROM task_audit WHERE owner=? ORDER BY seq', (owner,)).fetchall()
            head = conn.execute('SELECT seq,mac FROM task_audit_heads WHERE owner=?', (owner,)).fetchone()
            has_tasks = bool(conn.execute('SELECT 1 FROM tasks WHERE owner=? LIMIT 1', (owner,)).fetchone())
        previous, items = '', []
        for expected, (seq, event, mac) in enumerate(rows, 1):
            computed = hmac.new(self.audit_key, (previous + '\n' + event).encode(), hashlib.sha256).hexdigest()
            try:
                value = json.loads(event)
            except (ValueError, TypeError) as error:
                raise TaskIntegrityError('任务审计记录格式校验失败。') from error
            if not isinstance(value, dict) or value.get('owner') != owner or seq != expected or value.get('previous') != previous or not hmac.compare_digest(computed, mac):
                raise TaskIntegrityError('任务审计链校验失败。')
            items.append(value)
            previous = mac
        if (head and (head[0] != len(rows) or head[1] != previous)) or (rows and not head) or (has_tasks and not rows):
            raise TaskIntegrityError('任务审计链缺少记录或尾部被删除。')
        return {'verified': True, 'event_count': len(items), 'items': items, 'notice': '本机 HMAC 链可检测已保存事件的修改和截断；不等同外部不可删除审计或医疗合规认证。'}
