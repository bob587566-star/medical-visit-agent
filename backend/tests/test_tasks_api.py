import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.model import ModelService
from backend.task_storage import TaskIntegrityError


def prepare(client):
    headers = {'X-CSRF-Token': client.get('/api/bootstrap').json()['csrf_token']}
    visit = client.post('/api/visits', json={}, headers=headers).json()
    base = f"/api/visits/{visit['id']}"
    client.patch(base + '/intake', json={'chief_complaint': '虚构：咳嗽两天'}, headers=headers)
    return headers, base


def finish(client, base, task_id):
    for _ in range(100):
        response = client.get(base + '/tasks/' + task_id)
        assert response.status_code == 200
        task = response.json()
        if task['status'] not in {'queued', 'running'}:
            return task
        time.sleep(.01)
    pytest.fail('local task did not reach a terminal state')


def test_real_task_jobs_drafts_apply_once_and_audit(tmp_path):
    app = create_app(tmp_path / 'db.sqlite3', ModelService())
    with TestClient(app) as client:
        headers, base = prepare(client)
        queued = client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers)
        assert queued.status_code == 202
        task_id = queued.json()['task']['id']
        task = finish(client, base, task_id)
        assert task['status'] == 'needs_input' and task['result']['summary']
        before = client.get(base).json()
        assert before['summary'] is None
        applied = client.post(base + f'/tasks/{task_id}/apply', json={}, headers=headers)
        assert applied.status_code == 200 and applied.json()['task']['applied_at']
        assert applied.json()['visit']['summary']
        assert applied.json()['visit']['intake'] == before['intake']
        assert client.post(base + f'/tasks/{task_id}/apply', json={}, headers=headers).status_code == 409
        audit = client.get('/api/tasks/audit').json()
        assert audit['verified'] and audit['event_count'] == 4
        assert all('咳嗽' not in str(event) for event in audit['items'])


def test_stale_result_cannot_overwrite_new_intake(tmp_path):
    with TestClient(create_app(tmp_path / 'db.sqlite3', ModelService())) as client:
        headers, base = prepare(client)
        task_id = client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers).json()['task']['id']
        finish(client, base, task_id)
        client.patch(base + '/intake', json={'chief_complaint': '虚构：后来记录头痛'}, headers=headers)
        assert client.post(base + f'/tasks/{task_id}/apply', json={}, headers=headers).status_code == 409
        assert client.get(base).json()['intake']['chief_complaint'] == '虚构：后来记录头痛'
        task = client.get(base + '/tasks/' + task_id).json()
        assert task['status'] == 'superseded' and not task['can_apply']
        assert task['result']['status'] == 'needs_input'


def test_new_emergency_invalidates_display_and_apply_without_rewriting_execution(tmp_path):
    with TestClient(create_app(tmp_path / 'db.sqlite3', ModelService())) as client:
        headers, base = prepare(client)
        task_id = client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers).json()['task']['id']
        original = finish(client, base, task_id)
        assert original['can_apply']
        reply = client.post(base + '/messages', json={'content': '现在胸口疼，冒冷汗', 'use_model': False}, headers=headers)
        assert reply.status_code == 200
        current = client.get(base + '/tasks/' + task_id).json()
        assert current['status'] == 'emergency'
        assert current['current_risk']['level'] == 'emergency'
        assert not current['can_apply'] and current['invalidated_reason']
        assert current['result'] == original['result']
        listed = client.get(base + '/tasks').json()['items'][0]
        assert listed['status'] == 'emergency' and not listed['can_apply']
        blocked = client.post(base + f'/tasks/{task_id}/apply', json={}, headers=headers)
        assert blocked.status_code == 409 and '安全' in blocked.json()['detail']
        assert client.get(base).json()['summary'] is None


def test_tasks_csrf_owner_and_clear(tmp_path):
    with TestClient(create_app(tmp_path / 'db.sqlite3', ModelService())) as client:
        headers, base = prepare(client)
        assert client.post(base + '/tasks', json={'goal': 'prepare_visit'}).status_code == 403
        task_id = client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers).json()['task']['id']
        finish(client, base, task_id)
        cookie = dict(client.cookies)
        client.cookies.clear()
        stranger = {'X-CSRF-Token': client.get('/api/bootstrap').json()['csrf_token']}
        assert client.get(base + '/tasks').status_code == 404
        assert client.get(base + '/tasks/' + task_id).status_code == 404
        assert client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=stranger).status_code == 404
        client.cookies.clear()
        client.cookies.update(cookie)
        assert client.delete('/api/data', headers=headers).status_code == 204
        assert client.get('/api/tasks/audit').json()['event_count'] == 0


def test_encrypted_task_payload_and_tampering_fail_closed(tmp_path):
    app = create_app(tmp_path / 'db.sqlite3', ModelService())
    with TestClient(app) as client:
        headers, base = prepare(client)
        task_id = client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers).json()['task']['id']
        finish(client, base, task_id)
        with app.state.store.connect() as conn:
            payload = conn.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
            assert '咳嗽' not in payload and 'summary' not in payload
            conn.execute('UPDATE tasks SET payload=? WHERE id=?', (payload[:-4] + 'xxxx', task_id))
        response = client.get(base + '/tasks/' + task_id)
        assert response.status_code == 503
        assert '咳嗽' not in response.text


def test_audit_event_edit_and_truncation_detected(tmp_path):
    app = create_app(tmp_path / 'db.sqlite3', ModelService())
    with TestClient(app) as client:
        headers, base = prepare(client)
        task_id = client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers).json()['task']['id']
        finish(client, base, task_id)
        with app.state.store.connect() as conn:
            conn.execute('DELETE FROM task_audit WHERE seq=(SELECT MAX(seq) FROM task_audit)')
        assert client.get('/api/tasks/audit').status_code == 503


def test_restart_marks_unfinished_task_interrupted(tmp_path):
    app = create_app(tmp_path / 'db.sqlite3', ModelService())
    with TestClient(app) as client:
        headers, base = prepare(client)
        owner = app.state.store.session(client.cookies.get('zhihe_visitor'))[0]
        from backend.tasks_api import TaskJob, revision
        from backend.storage import now
        visit = client.get(base).json()
        from backend.schemas import Visit
        task = TaskJob(id='interrupted-fixture', visit_id=visit['id'], goal='prepare_visit', created_at=now(), updated_at=now(), source_revision=revision(Visit.model_validate(visit)))
        app.state.task_store.put(owner, task.model_dump(mode='json'))
        app.state.task_store.interrupt_unfinished()
        assert client.get(base + '/tasks/interrupted-fixture').json()['status'] == 'interrupted'


def test_invalid_audit_json_returns_explicit_integrity_error(tmp_path):
    app = create_app(tmp_path / 'db.sqlite3', ModelService())
    with TestClient(app) as client:
        headers, base = prepare(client)
        task_id = client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers).json()['task']['id']
        finish(client, base, task_id)
        with app.state.store.connect() as conn:
            conn.execute('UPDATE task_audit SET event=? WHERE seq=1', ('invalid JSON',))
        assert client.get('/api/tasks/audit').status_code == 503


def test_expired_owner_removes_tasks_and_audit_before_session(tmp_path):
    app = create_app(tmp_path / 'db.sqlite3', ModelService())
    with TestClient(app) as client:
        headers, base = prepare(client)
        task_id = client.post(base + '/tasks', json={'goal': 'prepare_visit'}, headers=headers).json()['task']['id']
        finish(client, base, task_id)
        owner = app.state.store.session(client.cookies.get('zhihe_visitor'))[0]
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with app.state.store.connect() as conn:
            conn.execute('UPDATE sessions SET created_at=? WHERE id=?', (old, owner))
        app.state.store.expire((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
        with app.state.store.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
            assert conn.execute('SELECT COUNT(*) FROM task_audit').fetchone()[0] == 0
