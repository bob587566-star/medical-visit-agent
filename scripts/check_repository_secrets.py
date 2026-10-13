"""Reject known private artifact paths and obvious literal credentials in Git.

This is a narrow CI check, not a replacement for a full secret scanner.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess


PRIVATE_PREFIXES = (
    "data/", "logs/", ".venv/", "private-backups/", ".pytest-tmp/",
    ".ops-audit-venv/", "frontend/test-results/",
)
PRIVATE_SUFFIXES = (
    ".tasks.key", ".data.keys.json", ".sqlite3", ".sqlite", ".zhihe",
    ".password", ".key", ".private", ".keyring", ".encrypted-backup",
)
CREDENTIAL_PATTERN = re.compile(
    rb"(?:(?<![a-zA-Z0-9_-])sk-[a-zA-Z0-9_-]{24,}"
    rb"|(?<![a-zA-Z0-9_-])(?:gh[pousr]_|github_pat_)[a-zA-Z0-9_]{30,}"
    rb"|-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----)"
)


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-replace-objects", *args], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def _private_path(name: str) -> bool:
    return (name == ".env" or (name.startswith(".env.") and name != ".env.example")
            or name.startswith(PRIVATE_PREFIXES) or name.endswith(PRIVATE_SUFFIXES))


def _working_tree_findings(root: Path) -> tuple[set[str], set[str]]:
    names = _git(root, "ls-files", "-z").decode("utf-8", "surrogateescape").split("\0")
    private, literal = set(), set()
    for name in names:
        if not name:
            continue
        if _private_path(name):
            private.add(name)
            continue
        disk = root / name
        if disk.is_file() and disk.stat().st_size <= 2_000_000 and CREDENTIAL_PATTERN.search(disk.read_bytes()):
            literal.add(name)
    return private, literal


def _blob_has_credential(stream, size: int) -> bool:
    remaining, overlap, found = size, b"", False
    while remaining:
        chunk = stream.read(min(65_536, remaining))
        if not chunk:
            raise RuntimeError("Incomplete Git blob")
        if not found:
            window = overlap + chunk
            # 128 bytes exceed every pattern's minimum match length. A match at
            # the start of an overlap was already checked with its real context.
            found = any(match.start() > 0 or remaining == size
                        for match in CREDENTIAL_PATTERN.finditer(window))
            overlap = window[-128:]
        remaining -= len(chunk)
    return found


def _history_findings(root: Path) -> tuple[set[str], set[str]]:
    private, literal = set(), set()
    blobs: dict[bytes, set[str]] = {}
    # Enumerate every reachable commit tree, rather than only one path per
    # object: a harmless blob can also have appeared under a private filename.
    for commit in _git(root, "rev-list", "--all").splitlines():
        entries = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit.decode("ascii"))
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, raw_name = entry.split(b"\t", 1)
            _, kind, oid = metadata.split()
            name = raw_name.decode("utf-8", "surrogateescape")
            if _private_path(name):
                private.add(name)
            elif kind == b"blob":
                blobs.setdefault(oid, set()).add(name)
    if not blobs:
        return private, literal
    # Binary blobs and blobs larger than the working-tree limit are scanned in
    # bounded chunks. Private artifact contents are unnecessary and not read.
    with subprocess.Popen(
        ["git", "--no-replace-objects", "cat-file", "--batch"], cwd=root,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as batch:
        for oid, names in blobs.items():
            batch.stdin.write(oid + b"\n")
            batch.stdin.flush()
            header = batch.stdout.readline().split()
            if len(header) != 3 or header[0] != oid or header[1] != b"blob":
                raise RuntimeError("Unexpected Git blob response")
            found = _blob_has_credential(batch.stdout, int(header[2]))
            if batch.stdout.read(1) != b"\n":
                raise RuntimeError("Incomplete Git blob response")
            if found:
                literal.update(names)
        batch.stdin.close()
        if batch.wait() != 0:
            raise RuntimeError("Git blob reader failed")
    return private, literal


def _reported_path(name: str) -> str:
    # Escape control characters and redact tokens even if used in a filename.
    safe = CREDENTIAL_PATTERN.sub(b"<credential-redacted>", name.encode("utf-8", "surrogateescape"))
    return json.dumps(safe.decode("utf-8", "surrogateescape"), ensure_ascii=True)


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="Also scan all reachable Git commit trees and blobs.")
    args = parser.parse_args(argv)
    root = root or Path(__file__).resolve().parents[1]
    try:
        private, literal = _working_tree_findings(root)
        if args.history:
            old_private, old_literal = _history_findings(root)
            private.update(old_private)
            literal.update(old_literal)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print("Repository credential check could not complete; contents were not printed.")
        return 1
    if private or literal:
        print("Private artifact or possible credential detected; contents were not printed.")
        for name in sorted(private | literal):
            print(_reported_path(name))
        return 1
    scope = "tracked files and reachable Git history" if args.history else "tracked files"
    print(f"Known private paths and obvious credential patterns were not found in {scope}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
