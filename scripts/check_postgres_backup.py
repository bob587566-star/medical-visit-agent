"""Real fixture backup/restore exercise using one disposable fictional visitor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.postgres_backup import backup_fixture, restore_fixture, verify_fixture_archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8272")
    parser.add_argument("--config", type=Path, default=Path("data/runtime-check.env"))
    parser.add_argument("--data-key-file", type=Path, default=Path("data/runtime-profile/data.keys.json"))
    parser.add_argument("--task-key-file", type=Path, default=Path("data/runtime-profile/tasks.key"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/postgres-backup-drill-20261013.json"))
    args = parser.parse_args()
    if args.base_url != "http://127.0.0.1:8272":
        raise SystemExit("This exercise only accepts the dedicated local fixture API.")
    private = Path("data") / ("pg-backup-drill-" + secrets.token_hex(6))
    private.mkdir()
    password = secrets.token_urlsafe(48).encode()
    password_file = private / "backup-password.private"
    password_file.write_bytes(password)
    password_file.chmod(0o600)
    headers, visit_id = {}, None
    with httpx.Client(base_url=args.base_url, timeout=20, trust_env=False, follow_redirects=False) as client:
        try:
            bootstrap = client.get("/api/bootstrap").raise_for_status().json()
            headers = {"X-CSRF-Token": bootstrap["csrf_token"], "Origin": args.base_url}
            client.post("/api/privacy/consent", headers=headers, json={"notice_version": bootstrap["privacy"]["notice_version"],
                        "local_storage": True, "external_ai": False, "retention_days": 1}).raise_for_status()
            visit = client.post("/api/visits", headers=headers, json={"title": "虚构 PostgreSQL 备份演练"}).raise_for_status().json()
            visit_id = visit["id"]
            client.patch(f"/api/visits/{visit_id}/intake", headers=headers,
                         json={"chief_complaint": "虚构备份演练：轻微咳嗽两天"}).raise_for_status()
            task = client.post(f"/api/visits/{visit_id}/tasks", headers=headers,
                               json={"goal": "prepare_visit", "use_model": False}).raise_for_status().json()["task"]
            finished = None
            for _ in range(80):
                finished = client.get(f"/api/visits/{visit_id}/tasks/{task['id']}").raise_for_status().json()
                if finished["status"] not in {"queued", "running"}:
                    break
                time.sleep(.1)
            if not finished or finished["status"] not in {"completed", "needs_input"}:
                raise AssertionError("Fictional task was not completed by the independent worker")
            archive = private / "snapshot.pg-zhihe"
            manifest = backup_fixture(args.config, args.data_key_file.parent, archive, password,
                                      data_key_file=args.data_key_file, task_key_file=args.task_key_file)
            verified, _ = verify_fixture_archive(archive, password)
            assert verified == manifest
            restored = restore_fixture(args.config, archive, private / "restored-keys", password)
            report = {"scope": "isolated fixture PG/API/worker; one fictional visitor; external AI disabled",
                      "source_counts": manifest["counts"], "source_schema_version": manifest["schema_version"],
                      "authenticated_archive": True, "restoration": restored,
                      "source_fictional_visit_deleted": False}
            removed = client.delete(f"/api/visits/{visit_id}", headers=headers)
            assert removed.status_code == 204
            report["source_fictional_visit_deleted"] = True
            visit_id = None
            client.delete("/api/privacy/consent", headers=headers).raise_for_status()
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"archive_authenticated": True, "restored_counts_match": restored["counts_match"],
                              "source_fictional_visit_deleted": True, "report": str(args.out)}))
            return 0
        except Exception as error:
            print("Fixture backup exercise failed: " + type(error).__name__ + "; no credentials, key, or clinical payload printed.")
            return 1
        finally:
            if visit_id:
                try:
                    client.delete(f"/api/visits/{visit_id}", headers=headers).raise_for_status()
                    client.delete("/api/privacy/consent", headers=headers).raise_for_status()
                except Exception:
                    print("Own fictional visitor cleanup needs checking; no other visitor was modified.")


if __name__ == "__main__":
    raise SystemExit(main())
