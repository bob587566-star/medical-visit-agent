"""Offline authenticated-record key rotation with a verified encrypted backup.

Stops are deliberately operator-controlled. The acknowledgement is mandatory;
this tool never kills a process or changes a running service's configuration.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.storage import Store
from scripts.backup import create_backup, verify_backup


def rotate(database: Path, backup: Path, password: bytes, *, data_key_file: Path | None = None,
           task_key_file: Path | None = None, confirm_service_stopped: bool = False) -> dict:
    if not confirm_service_stopped:
        raise ValueError('Stop the application and all workers before an offline rotation.')
    if not database.is_file():
        raise ValueError('Database does not exist; rotation never creates a new database.')
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_crypto_meta'").fetchone():
            raise ValueError('Run the explicit legacy encryption migration before rotating a key.')
        version = conn.execute("SELECT value FROM storage_crypto_meta WHERE name='version'").fetchone()
        if not version or version[0] != '1':
            raise ValueError('Database encryption version is unsupported.')
    # Backup and its validation are complete before creating the candidate key.
    manifest = create_backup(database, backup, password, data_key_file=data_key_file,
                             task_key_file=task_key_file)
    checked = verify_backup(backup, password)
    store = Store(database, key_path=data_key_file)
    result = store.rotate_data_key()
    return {'rotation': result, 'backup': str(backup.resolve()), 'backup_verified': True,
            'notice': 'Old keys are retained for interrupted-switch recovery and existing backups. '
                      'This rotates record/CSRF/profile envelopes, not the separately keyed task/audit key.'}


def main() -> int:
    parser = argparse.ArgumentParser(description='Offline record-key rotation; requires a verified encrypted backup.')
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--password-file', type=Path, required=True)
    parser.add_argument('--data-key-file', type=Path)
    parser.add_argument('--task-key-file', type=Path)
    parser.add_argument('--confirm-service-stopped', action='store_true')
    args = parser.parse_args()
    try:
        password = args.password_file.read_bytes().strip()
        print(json.dumps(rotate(args.db, args.backup, password,
                                data_key_file=args.data_key_file, task_key_file=args.task_key_file,
                                confirm_service_stopped=args.confirm_service_stopped),
                         ensure_ascii=False))
        return 0
    except Exception as error:
        # Never include source rows, key contents, passwords or arbitrary errors.
        print(json.dumps({'error': type(error).__name__,
                          'notice': 'Rotation did not report success. Keep the service stopped; '
                                    'verify the encrypted backup and keyring before restarting.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
