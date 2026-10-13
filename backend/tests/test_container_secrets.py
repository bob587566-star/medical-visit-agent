"""Private fixture secret delivery without changing or replacing original keys."""
import importlib.util
import os
from pathlib import Path
import stat

import pytest


@pytest.fixture
def secret_preparation(monkeypatch):
    script = Path(__file__).resolve().parents[2] / "deploy" / "prepare_secrets.py"
    spec = importlib.util.spec_from_file_location("fixture_secret_preparation", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    owners = []
    monkeypatch.setattr(module, "_chown", lambda path, uid, gid: owners.append((path, uid, gid)))
    # Keep the test runner's umask unchanged.
    monkeypatch.setattr(module.os, "umask", lambda mode: 0)
    return module, owners


def sources(tmp_path):
    source = tmp_path / "originals"
    source.mkdir()
    for name in ("record_key", "task_key", "metrics_token"):
        path = source / name
        path.write_bytes(("fictional-original-" + name).encode())
        path.chmod(0o600)
    return source


def test_original_bytes_and_host_modes_preserved_for_each_consumer(tmp_path, secret_preparation):
    module, owners = secret_preparation
    source = sources(tmp_path)
    application, metrics = tmp_path / "app", tmp_path / "metrics"
    original = {path.name: (path.read_bytes(), path.stat().st_mode) for path in source.iterdir()}
    module.prepare(source, application, metrics)
    module.prepare(source, application, metrics)
    for name, directory, uid in (("record_key", application, 1000), ("task_key", application, 1000), ("metrics_token", metrics, 65534)):
        destination = directory / name
        assert destination.read_bytes() == original[name][0]
        assert (destination, uid, uid) in owners
        assert (source / name).stat().st_mode == original[name][1]
        if os.name == "posix":
            assert stat.S_IMODE(destination.stat().st_mode) == 0o600
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert set(path.name for path in application.iterdir()) == {"record_key", "task_key"}
    assert set(path.name for path in metrics.iterdir()) == {"metrics_token"}


def test_changed_original_refused_without_replacing_prepared_keys(tmp_path, secret_preparation):
    module, owners = secret_preparation
    source = sources(tmp_path)
    application, metrics = tmp_path / "app", tmp_path / "metrics"
    module.prepare(source, application, metrics)
    old = (application / "record_key").read_bytes()
    (source / "record_key").write_bytes(b"different-fictional-key")
    owners.clear()
    with pytest.raises(ValueError, match="nothing was replaced"):
        module.prepare(source, application, metrics)
    assert (application / "record_key").read_bytes() == old
    assert owners == []


@pytest.mark.parametrize("problem", ["missing", "empty", "oversized", "directory"])
def test_invalid_original_refused_before_creating_any_destination(tmp_path, secret_preparation, problem):
    module, owners = secret_preparation
    source = sources(tmp_path)
    target = source / "task_key"
    target.unlink()
    if problem == "empty":
        target.touch()
    elif problem == "oversized":
        target.write_bytes(b"x" * 65537)
    elif problem == "directory":
        target.mkdir()
    application, metrics = tmp_path / "app", tmp_path / "metrics"
    with pytest.raises(ValueError):
        module.prepare(source, application, metrics)
    assert not application.exists() and not metrics.exists()
    assert owners == []
