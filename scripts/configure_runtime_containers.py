"""Prepare private container config from the existing isolated fixture profile."""
from pathlib import Path
import secrets


def main():
    root = Path(__file__).resolve().parents[1]
    directory = root / "data"
    config = directory / "runtime-container.env"
    if config.exists():
        current = config.read_text(encoding='utf-8')
        if not any(line.startswith('APP_ALLOWED_HOSTS=') for line in current.splitlines()):
            with config.open('a', encoding='utf-8', newline='\n') as file:
                file.write('\nAPP_ALLOWED_HOSTS=127.0.0.1,localhost,api\n')
        print("Existing private runtime container configuration retained.")
        return
    if not all((directory / "runtime-profile" / name).is_file() for name in ("data.keys.json", "tasks.key")):
        raise SystemExit("Prepare the isolated runtime profile and its original keys first. No replacement keys were generated.")
    values = dict(line.split("=", 1) for line in (directory / "runtime-check.env").read_text(encoding="utf-8").splitlines() if "=" in line)
    database = values["DATABASE_URL"].replace("@127.0.0.1:18432/", "@postgres:5432/")
    redis = values["REDIS_URL"].replace("@127.0.0.1:18379/", "@redis:6379/")
    if "@postgres:5432/medical_fixture" not in database or "@redis:6379/" not in redis:
        raise SystemExit("Unexpected fixture connection configuration.")
    token = secrets.token_hex(32)
    token_file = directory / "runtime-metrics-token.private"
    with token_file.open("x", encoding="ascii") as file:
        file.write(token)
    token_file.chmod(0o600)
    with config.open("x", encoding="utf-8", newline="\n") as file:
        file.write(f"DATABASE_URL={database}\nREDIS_URL={redis}\nMEDICAL_DATA_KEY_FILE=/run/secrets/record_key\nMEDICAL_TASK_KEY_FILE=/run/secrets/task_key\nMETRICS_TOKEN={token}\nDEEPSEEK_API_KEY=\nLOCAL_SAFETY_ENABLED=false\nCOOKIE_SECURE=false\nAPP_ALLOWED_HOSTS=127.0.0.1,localhost,api\n")
    config.chmod(0o600)
    print("Private container connection/token configuration prepared; credentials were not printed.")


if __name__ == "__main__":
    main()
