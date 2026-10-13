import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.check_repository_secrets import main


def git(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    env = os.environ | {
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Scanner Test", "GIT_AUTHOR_EMAIL": "scanner@example.invalid",
        "GIT_COMMITTER_NAME": "Scanner Test", "GIT_COMMITTER_EMAIL": "scanner@example.invalid",
    }
    return subprocess.run(
        ["git", "-c", "commit.gpgSign=false", "-c", f"core.hooksPath={repo / 'no-hooks'}", *args],
        cwd=repo, env=env, input=input, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "--initial-branch=main")
    return tmp_path


def write(repo, name, content=b"harmless synthetic fixture\n"):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def commit(repo):
    git(repo, "add", "--all")
    git(repo, "commit", "-q", "-m", "Synthetic scanner fixture")


def reported_paths(output):
    return {json.loads(line) for line in output.splitlines()[1:]}


def test_deleted_private_paths_are_found_even_with_shared_blob(repo, capsys):
    names = {
        ".env", ".env.production", "data/records.txt", "logs/request.txt", ".venv/runtime.txt",
        "private-backups/export.txt", ".pytest-tmp/cache.txt", ".ops-audit-venv/runtime.txt",
        "frontend/test-results/report.txt", "local.tasks.key", "local.data.keys.json",
        "records.sqlite3", "records.sqlite", "backup.zhihe", "backup.password", "server.key",
        "metrics.private", "vault.keyring", "archive.encrypted-backup",
    }
    # All paths deliberately share one blob; rev-list --objects alone can lose
    # the private aliases because it only supplies one name for each object.
    for name in names | {"fixture.txt"}:
        write(repo, name)
    commit(repo)
    git(repo, "rm", "-r", "--", *sorted(names))
    commit(repo)
    assert main([], root=repo) == 0
    capsys.readouterr()
    assert main(["--history"], root=repo) == 1
    assert reported_paths(capsys.readouterr().out) == names


@pytest.mark.parametrize("prefix", [b"sk-", b"sk-proj-", b"sk-ant-api03-"]
                         + [b"gh" + kind + b"_" for kind in (b"p", b"o", b"u", b"s", b"r")]
                         + [b"github_" + b"pat_"])
def test_removed_provider_token_is_found_without_printing_it(repo, capsys, prefix):
    token = prefix + b"A" * 32
    write(repo, "config.txt", b"provider=" + token + b"\n")
    commit(repo)
    write(repo, "config.txt")
    commit(repo)
    assert main([], root=repo) == 0
    capsys.readouterr()
    assert main(["--history"], root=repo) == 1
    output = capsys.readouterr().out
    assert reported_paths(output) == {"config.txt"}
    assert token.decode() not in output
    assert "provider=" not in output


@pytest.mark.parametrize("kind", [b"", b"RSA ", b"DSA ", b"EC ", b"OPENSSH ", b"ENCRYPTED "])
def test_removed_private_key_marker_is_found_in_binary_blob(repo, capsys, kind):
    marker = b"-----BEGIN " + kind + b"PRIVATE KEY-----"
    write(repo, "fixture.bin", b"\x00\xff" + marker + b"\x00fictional-key-body")
    commit(repo)
    git(repo, "rm", "--", "fixture.bin")
    commit(repo)
    assert main(["--history"], root=repo) == 1
    output = capsys.readouterr().out
    assert reported_paths(output) == {"fixture.bin"}
    assert marker.decode() not in output
    assert "fictional-key-body" not in output


def test_large_binary_blob_matches_across_chunk_boundary(repo, capsys):
    token = b"sk-" + b"B" * 32
    write(repo, "large.bin", b"\x00" * (65_536 - 10) + token + b"\xff" * 2_100_000)
    commit(repo)
    write(repo, "large.bin")
    commit(repo)
    assert main(["--history"], root=repo) == 1
    assert reported_paths(capsys.readouterr().out) == {"large.bin"}


def test_harmless_fixtures_and_env_example_pass(repo, capsys):
    write(repo, ".env.example", b"PROVIDER_API_KEY=your-key-here\n")
    write(repo, "fixture.bin", b"\x00\xffsk-short\x00public data\n")
    # A preceding token character excludes an incidental string, including at
    # the overlap boundary where the preceding byte must not be forgotten.
    write(repo, "boundary.bin", b"\x00" * (65_536 - 129) + b"ask-" + b"C" * 32 + b"\x00" * 65_536)
    commit(repo)
    assert main([], root=repo) == 0
    assert main(["--history"], root=repo) == 0
    assert "could not complete" not in capsys.readouterr().out


def test_unicode_paths_and_filename_tokens_are_reported_safely(repo, capsys):
    token = "sk-" + "D" * 32
    names = {"历史/诊疗说明.txt", f"历史/{token}.txt"}
    for name in names:
        write(repo, name, b"sk-" + b"E" * 32)
    commit(repo)
    git(repo, "rm", "-r", "--", "历史")
    commit(repo)
    assert main(["--history"], root=repo) == 1
    output = capsys.readouterr().out
    assert reported_paths(output) == {"历史/诊疗说明.txt", "历史/<credential-redacted>.txt"}
    assert token not in output


def test_branch_and_tag_history_are_reachable(repo, capsys):
    write(repo, "fixture.txt")
    commit(repo)
    git(repo, "checkout", "-q", "-b", "other")
    write(repo, "other.txt", b"sk-" + b"F" * 32)
    commit(repo)
    git(repo, "checkout", "-q", "main")
    assert main(["--history"], root=repo) == 1
    assert reported_paths(capsys.readouterr().out) == {"other.txt"}
    git(repo, "tag", "retained", "other")
    git(repo, "branch", "-D", "other")
    assert main(["--history"], root=repo) == 1
    assert reported_paths(capsys.readouterr().out) == {"other.txt"}


def test_unreachable_commit_and_loose_blob_are_not_scanned(repo, capsys):
    write(repo, "fixture.txt")
    commit(repo)
    git(repo, "checkout", "-q", "-b", "discarded")
    write(repo, ".env", b"fictional private fixture")
    write(repo, "removed.txt", b"sk-" + b"G" * 32)
    commit(repo)
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-D", "discarded")
    git(repo, "hash-object", "-w", "--stdin", input=b"sk-" + b"H" * 32)
    assert main(["--history"], root=repo) == 0
    assert "reachable Git history" in capsys.readouterr().out


def test_detached_head_history_is_reachable(repo, capsys):
    write(repo, "fixture.txt")
    commit(repo)
    git(repo, "checkout", "-q", "--detach", "main")
    write(repo, "detached.txt", b"sk-" + b"J" * 32)
    commit(repo)
    git(repo, "rm", "--", "detached.txt")
    commit(repo)
    assert main(["--history"], root=repo) == 1
    assert reported_paths(capsys.readouterr().out) == {"detached.txt"}


def test_git_failure_fails_closed_without_printing_details(tmp_path, capsys, monkeypatch):
    # The pytest temp root may itself be inside the real repository; disable
    # parent discovery by explicitly pointing Git at a nonexistent directory.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing.git"))
    assert main(["--history"], root=tmp_path) == 1
    output = capsys.readouterr()
    assert output.out == "Repository credential check could not complete; contents were not printed.\n"
    assert output.err == ""


def test_history_also_checks_tracked_uncommitted_changes(repo, capsys):
    write(repo, "fixture.txt")
    commit(repo)
    write(repo, "fixture.txt", b"sk-" + b"I" * 32)
    assert main([], root=repo) == 1
    capsys.readouterr()
    assert main(["--history"], root=repo) == 1
    assert reported_paths(capsys.readouterr().out) == {"fixture.txt"}
