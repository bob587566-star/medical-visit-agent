"""Inspect/apply the versioned SQLite baseline without printing record data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.migrations import MigrationError, apply_migrations, migration_status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "apply"))
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    database = args.db.resolve(strict=True)
    mode = "ro" if args.action == "status" else "rw"
    try:
        with sqlite3.connect(database.as_uri() + "?mode=" + mode, uri=True) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            result = migration_status(conn) if args.action == "status" else apply_migrations(conn)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MigrationError, sqlite3.Error):
        print("Migration inspection/upgrade failed. No clinical data is included in this error.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
