"""Reject known private artifact paths and obvious literal credentials in Git.

This is a narrow CI check, not a replacement for a full secret scanner.
"""
from pathlib import Path
import re
import subprocess


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    names = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, stdout=subprocess.PIPE).stdout.decode().split("\0")
    private = []
    literal = []
    pattern = re.compile(rb"(?:(?<![a-zA-Z0-9_-])sk-[a-zA-Z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
    for name in names:
        if not name:
            continue
        path = Path(name)
        if (name == ".env" or (name.startswith('.env.') and name != '.env.example')
                or name.startswith(("data/", "logs/", ".venv/", 'private-backups/', '.pytest-tmp/', '.ops-audit-venv/', 'frontend/test-results/'))
                or name.endswith((".tasks.key", ".data.keys.json", ".sqlite3", ".sqlite", ".zhihe", ".password", '.key', '.private', '.keyring', '.encrypted-backup'))):
            private.append(name)
            continue
        disk = root / path
        if disk.is_file() and disk.stat().st_size <= 2_000_000 and pattern.search(disk.read_bytes()):
            literal.append(name)
    if private or literal:
        print("Private artifact or possible credential detected; contents were not printed.")
        for name in private + literal:
            print(name)
        return 1
    print("Known private paths and obvious credential patterns were not found in tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
