"""Copy original fixture secrets into private volumes for non-root consumers.

This one-shot, network-disabled container never generates or replaces keys.
The host inputs stay read-only and retain their existing ownership and mode.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


def _chown(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)


def prepare(source: Path, application: Path, metrics: Path) -> None:
    os.umask(0o077)
    specifications = (
        ("record_key", application, 1000, 1000),
        ("task_key", application, 1000, 1000),
        ("metrics_token", metrics, 65534, 65534),
    )
    copies = []
    # Validate every source and existing destination before changing anything.
    for name, directory, uid, gid in specifications:
        original, destination = source / name, directory / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("Prepared secret directory must be a real directory.")
        if not original.is_file() or not 0 < original.stat().st_size <= 65536:
            raise ValueError("An original secret is missing, empty, or too large.")
        value = original.read_bytes()
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ValueError("Prepared secret must be a regular file.")
        if destination.exists() and destination.read_bytes() != value:
            raise ValueError("Prepared secret differs from its original; nothing was replaced.")
        copies.append((destination, value, uid, gid))

    for destination, value, uid, gid in copies:
        directory = destination.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        _chown(directory, uid, gid)
        if not destination.exists():
            # Exclusive creation prevents a second initializer from replacing it.
            with destination.open("xb") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
        destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
        _chown(destination, uid, gid)


def main() -> int:
    try:
        prepare(Path("/run/secrets"), Path("/prepared/application"), Path("/prepared/metrics"))
    except (OSError, ValueError):
        print("Secret preparation failed; original inputs were retained and contents were not printed.", file=sys.stderr)
        return 1
    print("Original fixture secrets prepared for non-root consumers; contents were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
