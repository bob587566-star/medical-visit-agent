"""Process-level acceptance of the isolated API and independent CLI worker.

Only 127.0.0.1:8272 and PIDs verified by runtime_profile are touched. PostgreSQL
and Redis remain running. All visits are fictional, all model flags are false,
and cleanup uses the same newly created visitor identity as each scenario.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import runtime_profile
from acceptance import Client, require
from acceptance_tasks import FULL_INTAKE

BASE_URL = 'http://127.0.0.1:8272'
REPORT = ROOT / 'artifacts' / 'persistent-cli-check-20261013.json'


def prepare():
    client = Client(BASE_URL)
    boot = client.bootstrap()['json']
    require(boot.get('model_available') is False, 'isolated profile must disable external model credentials')
    visit = client.create('虚构：独立worker进程验收')
    base = '/api/visits/' + visit['id']
    require(client.request('PATCH', base + '/intake', FULL_INTAKE.copy())['status'] == 200, 'fictional input save failed')
    return client, base


def start_job(client, base):
    response = client.request('POST', base + '/tasks', {'goal': 'prepare_visit', 'use_model': False})
    require(response['status'] == 202, 'persistent task admission did not return 202')
    job = response['json']['task']
    require(job['status'] == 'queued' and job['result'] is None, 'admission did not expose queued state')
    return job['id']


def wait_job(client, base, task_id, timeout=30):
    start = time.monotonic()
    states = []
    while time.monotonic() - start < timeout:
        response = client.request('GET', base + '/tasks/' + task_id)
        require(response['status'] == 200, 'task detail became unavailable')
        job = response['json']
        require(job['id'] == task_id, 'task identity changed during processing')
        if not states or states[-1] != job['status']:
            states.append(job['status'])
        if job['status'] not in {'queued', 'running'}:
            require(job['status'] == 'completed' and job['result']['id'] == task_id, 'local CLI task failed to complete')
            require(job['result']['plan']['planner_mode'] == 'local', 'fixture unexpectedly used an external planner')
            return job, {'states': states, 'elapsed_ms': round((time.monotonic() - start) * 1000)}
        time.sleep(.2)
    raise AssertionError('independent worker did not complete within the bounded wait')


def audit_actions(client, task_id):
    response = client.request('GET', '/api/tasks/audit')
    require(response['status'] == 200 and response['json']['verified'], 'persistent audit did not verify')
    return [item['action'] for item in response['json']['items'] if item['task_id'] == task_id]


def worker_pid():
    return json.loads((ROOT / 'data' / 'runtime-profile' / 'processes.json').read_text(encoding='utf-8'))['worker']['pid']


def run():
    clients = []
    checks = []
    failure = None
    cleanup = []
    resumed = True
    require(runtime_profile.process_identity('api'), 'isolated API process identity is not verified')
    require(runtime_profile.process_identity('worker'), 'isolated worker process identity is not verified')
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        client, base = prepare(); clients.append(client)
        first = start_job(client, base)
        _, normal = wait_job(client, base, first)
        require(audit_actions(client, first) == ['task_queued', 'task_started', 'task_finished'], 'normal task has duplicate or missing audit transitions')
        checks.append({'name': 'independent_cli_worker_normal_completion', 'passed': True, 'task_id': first, 'audit_actions': audit_actions(client, first), **normal})

        previous_pid = worker_pid()
        runtime_profile.pause_worker(); resumed = False
        # Use a separate visit so previous confirmed draft state cannot affect it.
        waiting_visit = client.create('虚构：worker暂停时排队')
        waiting_base = '/api/visits/' + waiting_visit['id']
        require(client.request('PATCH', waiting_base + '/intake', FULL_INTAKE.copy())['status'] == 200, 'queued fixture input save failed')
        waiting = start_job(client, waiting_base)
        observed = []
        held_at = time.monotonic()
        for _ in range(5):
            item = client.request('GET', waiting_base + '/tasks/' + waiting)
            require(item['status'] == 200 and item['json']['status'] == 'queued' and item['json']['result'] is None, 'API executed a task while its independent worker was stopped')
            observed.append(item['json']['status'])
            time.sleep(.5)
        require(audit_actions(client, waiting) == ['task_queued'], 'paused task started in-process')
        require(client.request('GET', '/readyz')['status'] == 200, 'worker pause unexpectedly stopped the API')
        checks.append({'name': 'paused_worker_no_inprocess_execution', 'passed': True, 'task_id': waiting, 'observed_states': observed, 'held_ms': round((time.monotonic() - held_at) * 1000), 'audit_actions': ['task_queued'], 'api_remained_ready': True})

        runtime_profile.resume_worker(); resumed = True
        require(worker_pid() != previous_pid, 'worker process did not change on resume')
        completed, recovery = wait_job(client, waiting_base, waiting)
        actions = audit_actions(client, waiting)
        require(actions == ['task_queued', 'task_started', 'task_finished'], 'restart duplicated task execution/audit')
        checks.append({'name': 'new_cli_process_finishes_same_persisted_job', 'passed': True, 'task_id': waiting, 'worker_pid_changed': True, 'audit_actions': actions, **recovery})
        applied = client.request('POST', waiting_base + '/tasks/' + waiting + '/apply', {})
        require(applied['status'] == 200 and applied['json']['task']['applied_at'], 'confirmed draft apply did not persist')
        retry = client.request('POST', waiting_base + '/tasks/' + waiting + '/apply', {})
        require(retry['status'] == 409, 'duplicate task apply was not rejected')
        require(audit_actions(client, waiting) == ['task_queued', 'task_started', 'task_finished', 'user_confirmed_apply'], 'duplicate apply changed audit')
        checks.append({'name': 'confirmed_apply_once_after_worker_restart', 'passed': True, 'apply_http': 200, 'duplicate_http': 409, 'draft_was_not_automatically_applied': completed['applied_at'] is None})

        withdrawn, withdrawn_base = prepare(); clients.append(withdrawn)
        runtime_profile.pause_worker(); resumed = False
        pending = start_job(withdrawn, withdrawn_base)
        require(withdrawn.request('GET', withdrawn_base + '/tasks/' + pending)['json']['status'] == 'queued', 'withdrawal fixture not queued')
        removed = withdrawn.request('DELETE', '/api/privacy/consent')
        require(removed['status'] == 200 and removed['json']['accepted'] is False, 'withdrawal did not remove storage consent')
        require(withdrawn.request('GET', withdrawn_base)['status'] == 404, 'withdrawal left the visit readable')
        require(withdrawn.request('GET', withdrawn_base + '/tasks/' + pending)['status'] == 404, 'withdrawal left task payload readable')
        runtime_profile.resume_worker(); resumed = True
        # A resumed worker gets the orphan ID from Redis, then acknowledges it;
        # no snapshot/visit survives the database cascade to recreate any data.
        time.sleep(2)
        require(withdrawn.request('GET', withdrawn_base)['status'] == 404, 'resumed worker resurrected withdrawn visit')
        require(withdrawn.request('GET', withdrawn_base + '/tasks/' + pending)['status'] == 404, 'resumed worker resurrected withdrawn task')
        require(withdrawn.request('GET', '/api/tasks/audit')['json']['event_count'] == 0, 'withdrawn task left task audit payload')
        checks.append({'name': 'withdraw_pending_then_restart_no_resurrection', 'passed': True, 'task_id': pending, 'visit_http_after_restart': 404, 'task_http_after_restart': 404, 'task_audit_events_after_restart': 0})
    except Exception as error:
        failure = type(error).__name__ + ': ' + str(error)
    finally:
        if not resumed:
            try:
                runtime_profile.resume_worker(); resumed = True
            except Exception:
                failure = failure or 'Worker resume failed during cleanup; inspect private profile state.'
        for client in clients:
            try:
                response = client.request('DELETE', '/api/privacy/consent')
                cleanup.append({'storage_withdrawal_http': response['status'], 'health_records_cleared': response['status'] == 200 and response['json']['accepted'] is False})
            except Exception:
                cleanup.append({'health_records_cleared': False})
    report = {'tested_at': started_at, 'base_url': BASE_URL, 'scope': 'isolated fictional PostgreSQL/Redis profile and independent Python CLI worker', 'external_model_requested': False, 'checks': checks, 'passed': len(checks) == 5 and failure is None and all(item['health_records_cleared'] for item in cleanup), 'failure': failure, 'cleanup': cleanup, 'worker_running_at_end': resumed, 'boundaries': ['Finite process-level regression, not availability or load-capacity certification.', 'Worker was stopped before admission; pending-stream hard-crash reclaim has separate real Redis regression.', 'User SQLite service on 8270 and its records were not modified.']}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'checks': len(checks), 'worker_resumed': resumed, 'cleanup_passed': all(item['health_records_cleared'] for item in cleanup)}, ensure_ascii=True))
    if not report['passed']:
        raise SystemExit('Isolated CLI worker acceptance failed; read the sanitized artifact report.')


if __name__ == '__main__':
    run()
