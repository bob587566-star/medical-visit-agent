"""Start/stop the isolated local PG/Redis API and worker; never migrate user data.

Credentials stay in ignored runtime-check.env, not CLI arguments or output.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.model import read_project_env


PRIVATE = ROOT / 'data' / 'runtime-profile'


def profile_env():
    config = read_project_env(ROOT / 'data' / 'runtime-check.env')
    if not config.get('DATABASE_URL') or not config.get('REDIS_URL'):
        raise SystemExit('Prepare the isolated infrastructure configuration first.')
    if not all((PRIVATE / name).exists() for name in ('data.keys.json', 'tasks.key')):
        raise SystemExit('Existing profile key files are required; no replacement was generated.')
    return {**os.environ, **config, 'DEEPSEEK_API_KEY': '', 'LOCAL_SAFETY_ENABLED': 'false',
            'MEDICAL_DATA_KEY_FILE': str(PRIVATE / 'data.keys.json'),
            'MEDICAL_TASK_KEY_FILE': str(PRIVATE / 'tasks.key')}


def process_identity(name: str, *, terminate=False) -> bool:
    """Verify launcher AND direct interpreter child before any process stop."""
    if name not in {'api', 'worker'} or os.name != 'nt':
        raise SystemExit('Use a supported Windows profile process operation.')
    path = PRIVATE / 'processes.json'
    command = r'''$ErrorActionPreference='Stop'
$state=Get-Content -LiteralPath $env:ZHIHE_PROFILE_STATE -Raw | ConvertFrom-Json
$name=$env:ZHIHE_PROFILE_PROCESS
$id=[int]$state.$name.pid
if($id -lt 1) { Write-Output 'absent'; exit 0 }
$info=Get-CimInstance Win32_Process -Filter "ProcessId=$id"
if(-not $info) { Write-Output 'absent'; exit 0 }
$expected=Join-Path $env:ZHIHE_PROFILE_ROOT '.venv\Scripts\python.exe'
$pattern=if($name -eq 'worker') {'(?:^|\s)-m\s+backend\.worker(?:\s|$)'} else {'(?:^|\s)-m\s+uvicorn\s+backend\.main:app(?:\s|$)'}
if($info.ExecutablePath -ne $expected -or $info.CommandLine -notmatch $pattern) { throw 'Profile process identity mismatch.' }
if($name -eq 'api' -and $info.CommandLine -notmatch '(?:^|\s)--port\s+8272(?:\s|$)') { throw 'Profile API port mismatch.' }
$children=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$id")
$interpreters=@()
foreach($child in $children) {
  if($child.Name -eq 'conhost.exe' -and $child.ExecutablePath -eq (Join-Path $env:WINDIR 'system32\conhost.exe')) { continue }
  if($child.ExecutablePath -ne $env:ZHIHE_PROFILE_BASE_PYTHON -or $child.CommandLine -notmatch $pattern) { throw 'Profile interpreter child identity mismatch.' }
  if($name -eq 'api' -and $child.CommandLine -notmatch '(?:^|\s)--port\s+8272(?:\s|$)') { throw 'Profile API child port mismatch.' }
  $interpreters += $child
}
if($env:ZHIHE_PROFILE_TERMINATE -eq 'true') {
  foreach($child in $interpreters) { Stop-Process -Id $child.ProcessId }
  $remaining=Get-CimInstance Win32_Process -Filter "ProcessId=$id"
  if($remaining) {
    if($remaining.ExecutablePath -ne $expected -or $remaining.CommandLine -notmatch $pattern) { throw 'Profile launcher identity changed.' }
    Stop-Process -Id $id
  }
}
Write-Output 'present'
'''
    result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
        env={**os.environ, 'ZHIHE_PROFILE_STATE': str(path), 'ZHIHE_PROFILE_ROOT': str(ROOT),
             'ZHIHE_PROFILE_PROCESS': name, 'ZHIHE_PROFILE_BASE_PYTHON': sys._base_executable,
             'ZHIHE_PROFILE_TERMINATE': 'true' if terminate else 'false'}, capture_output=True)
    if result.returncode:
        raise SystemExit('Profile process identity could not be verified; private process data was not printed.')
    return result.stdout.decode(errors='replace').strip() == 'present'


def save_state(state):
    path = PRIVATE / 'processes.json'
    temporary = PRIVATE / 'processes.json.tmp'
    temporary.write_text(json.dumps(state), encoding='utf-8')
    temporary.replace(path)


def pause_worker():
    path = PRIVATE / 'processes.json'
    if not path.exists():
        raise SystemExit('No isolated profile is running.')
    process_identity('worker', terminate=True)
    state = json.loads(path.read_text(encoding='utf-8'))
    state['worker']['pid'] = None
    state['worker_paused'] = True
    save_state(state)
    print('Verified isolated worker stopped; API, PostgreSQL and Redis retained.')


def resume_worker():
    path = PRIVATE / 'processes.json'
    if not path.exists() or not process_identity('api'):
        raise SystemExit('The verified isolated API must be running before resuming its worker.')
    if process_identity('worker'):
        print('Verified isolated worker is already running.')
        return
    state = json.loads(path.read_text(encoding='utf-8'))
    python = str(ROOT / '.venv' / 'Scripts' / 'python.exe')
    arguments = [python, '-m', 'backend.worker']
    with (PRIVATE / 'worker.log').open('ab') as log:
        process = subprocess.Popen(arguments, cwd=ROOT, env=profile_env(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(.6)
    if process.poll() is not None:
        raise SystemExit('Worker resume failed; inspect private logs without sharing credentials.')
    state['worker'] = {'pid': process.pid, 'arguments': arguments}
    state['worker_paused'] = False
    save_state(state)
    if not process_identity('worker'):
        raise SystemExit('Resumed worker exited before identity verification.')
    print('Verified isolated worker resumed against the existing PostgreSQL and Redis.')


def start():
    private = ROOT / 'data' / 'runtime-profile'
    private.mkdir(exist_ok=True)
    state_file = private / 'processes.json'
    if state_file.exists():
        raise SystemExit('A profile process file exists. Stop this profile before starting it again.')
    config = read_project_env(ROOT / 'data' / 'runtime-check.env')
    if not config.get('DATABASE_URL') or not config.get('REDIS_URL'):
        raise SystemExit('Prepare the isolated infrastructure configuration first.')
    if not all((private / name).is_file() for name in ('data.keys.json', 'tasks.key')):
        raise SystemExit('Original profile keys are missing. Restore the matching keys; no replacement was generated.')
    env = {**os.environ, **config, 'DEEPSEEK_API_KEY': '', 'LOCAL_SAFETY_ENABLED': 'false',
           'MEDICAL_DATA_KEY_FILE': str(private / 'data.keys.json'),
           'MEDICAL_TASK_KEY_FILE': str(private / 'tasks.key')}
    python = str(ROOT / '.venv' / 'Scripts' / 'python.exe') if os.name == 'nt' else sys.executable
    commands = {
        'api': [python, '-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', '8272', '--no-access-log'],
        'worker': [python, '-m', 'backend.worker'],
    }
    children, output = [], {}
    try:
        for name, arguments in commands.items():
            log = (private / (name + '.log')).open('ab')
            process = subprocess.Popen(arguments, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            log.close()
            children.append(process)
            output[name] = {'pid': process.pid, 'arguments': arguments}
        for _ in range(40):
            if any(process.poll() is not None for process in children):
                raise RuntimeError('A profile process exited before readiness.')
            try:
                with urllib.request.urlopen('http://127.0.0.1:8272/readyz', timeout=2) as response:
                    if json.load(response).get('status') == 'ready':
                        state_file.write_text(json.dumps(output), encoding='utf-8')
                        print('Isolated PostgreSQL/Redis profile ready: http://127.0.0.1:8272/ ; independent worker started.')
                        return
            except Exception:
                pass
            time.sleep(.25)
        raise RuntimeError('Profile readiness timed out.')
    except Exception:
        for process in children:
            process.terminate()
            process.wait(timeout=10)
        raise SystemExit('Profile startup failed safely. Inspect its private logs; credentials were not printed.') from None


def stop():
    path = ROOT / 'data' / 'runtime-profile' / 'processes.json'
    if not path.exists():
        print('No isolated profile process record.')
        return
    if os.name != 'nt':
        raise SystemExit('Use the process supervisor to stop this profile on non-Windows systems.')
    # Verify both first, so an unrelated PID prevents the whole operation.
    process_identity('api')
    process_identity('worker')
    process_identity('worker', terminate=True)
    process_identity('api', terminate=True)
    path.unlink()
    print('Isolated API and worker stopped; PostgreSQL/Redis fixture volumes retained.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'pause-worker', 'resume-worker'))
    arguments = parser.parse_args()
    {'start': start, 'stop': stop, 'pause-worker': pause_worker, 'resume-worker': resume_worker}[arguments.action]()
