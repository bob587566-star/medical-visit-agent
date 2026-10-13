"""Create private credentials for the isolated local PG/Redis engineering stack.

No credential is printed, reused from the application, or sent to a model.
Existing files are never silently replaced.
"""
from pathlib import Path
import secrets


def main():
    root = Path(__file__).resolve().parents[1]
    directory = root / "data"
    directory.mkdir(exist_ok=True)
    files = [directory / name for name in ("runtime-check.env", "runtime-postgres.password", "runtime-redis.conf")]
    if any(path.exists() for path in files):
        if all(path.exists() for path in files):
            print("Isolated runtime credential files already exist; retained unchanged.")
            return
        raise SystemExit("Partial runtime configuration exists. Inspect the private files before proceeding; nothing was overwritten.")
    key_directory = directory / 'runtime-profile'
    key_files = [key_directory / name for name in ('data.keys.json', 'tasks.key')]
    if any(path.exists() for path in key_files):
        raise SystemExit('Existing fixture keys found without matching infrastructure config. Nothing was overwritten.')
    pg_password, redis_password = secrets.token_hex(32), secrets.token_hex(32)
    values = {
        files[0]: f"DATABASE_URL=postgresql+psycopg://zhihe_fixture:{pg_password}@127.0.0.1:18432/medical_fixture\nREDIS_URL=redis://:{redis_password}@127.0.0.1:18379/0\n",
        files[1]: pg_password + "\n",
        files[2]: f"bind 0.0.0.0\nprotected-mode yes\nrequirepass {redis_password}\nappendonly yes\nappendfsync everysec\ndir /data\nsave 60 1\nloglevel warning\nmaxmemory 128mb\nmaxmemory-policy noeviction\n",
    }
    for path, value in values.items():
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(value)
        path.chmod(0o600)
    # Fresh fictional profile only. Existing deployment keys are never replaced.
    from cryptography.fernet import Fernet
    key_directory = directory / 'runtime-profile'
    key_directory.mkdir(exist_ok=True)
    for name in ('data.keys.json', 'tasks.key'):
        key_file = key_directory / name
        if key_file.exists():
            raise SystemExit('Existing fixture key found during new configuration; verify matching credentials before proceeding.')
        with key_file.open('xb') as file:
            file.write(Fernet.generate_key())
        key_file.chmod(0o600)
    print("Created private isolated runtime configuration; passwords/connection URLs were not printed.")


if __name__ == "__main__":
    main()
