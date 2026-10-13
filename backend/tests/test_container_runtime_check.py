"""Runtime diagnostics remain useful without exposing private failure text."""
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def checker():
    path = Path(__file__).resolve().parents[2] / "deploy" / "check_application_runtime.py"
    spec = importlib.util.spec_from_file_location("fixture_runtime_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("caps", [["ALL"], ["all"], ["CAP_ALL"]])
def test_equivalent_drop_all_capability_names_accepted(checker, caps):
    assert checker.isolated({"readonly": True, "caps": caps, "security": ["no-new-privileges:true"]})


@pytest.mark.parametrize("change", [{"readonly": False}, {"caps": ["CHOWN"]}, {"caps": None}, {"security": []}])
def test_missing_isolation_still_refused(checker, change):
    settings = {"readonly": True, "caps": ["ALL"], "security": ["no-new-privileges:true"]}
    assert not checker.isolated({**settings, **change})


def test_failure_names_case_and_hides_raw_error(checker, monkeypatch, capsys):
    marker = "private-error-content-must-not-appear"

    def fail(project):
        checker.case("api-secret-ownership-and-readonly")
        raise RuntimeError(marker)

    monkeypatch.setattr(checker, "check", fail)
    monkeypatch.setattr(sys, "argv", ["check_application_runtime.py"])
    assert checker.main() == 1
    output = capsys.readouterr()
    assert "failed api-secret-ownership-and-readonly (RuntimeError)" in output.err
    assert marker not in output.out + output.err
