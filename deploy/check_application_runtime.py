"""Check the isolated fictional Compose profile without printing credentials.

Only fixed loopback endpoints are contacted. No records or external AI calls
are created; invoke after application-profile services have started.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, Request


def check(project: str) -> int:
    compose = ["docker", "compose", "-p", project, "-f", "compose.production.yaml", "--profile", "application"]
    opener = build_opener(ProxyHandler({}))

    def command(arguments: list[str]) -> str:
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            raise RuntimeError("An isolated container inspection failed.")
        return result.stdout.strip()

    def request(url: str, token: str | None = None) -> tuple[int, bytes]:
        headers = {"Authorization": "Bearer " + token} if token else {}
        try:
            with opener.open(Request(url, headers=headers), timeout=3) as response:
                return response.status, response.read(1_000_000)
        except HTTPError as error:
            return error.code, b""

    paths = [Path("data/runtime-profile/data.keys.json"), Path("data/runtime-profile/tasks.key"),
             Path("data/runtime-metrics-token.private"), Path("data/runtime-redis.conf")]
    if os.name == "posix" and any(stat.S_IMODE(path.stat().st_mode) != 0o600 for path in paths):
        raise RuntimeError("Original fixture files must remain private.")
    token = paths[2].read_text(encoding="ascii").strip()
    if not 32 <= len(token) <= 256:
        raise RuntimeError("The isolated metrics token is invalid.")

    inspection = ('{"readonly":{{json .HostConfig.ReadonlyRootfs}},'
                  '"caps":{{json .HostConfig.CapDrop}},"security":{{json .HostConfig.SecurityOpt}}}')
    for service in ("api", "worker", "prometheus"):
        container = command(compose + ["ps", "-q", service])
        if not container or "\n" in container:
            raise RuntimeError("Expected one isolated container per service.")
        settings = json.loads(command(["docker", "inspect", "--format", inspection, container]))
        if not settings["readonly"] or "ALL" not in settings["caps"] or not any(
                option.startswith("no-new-privileges") for option in settings["security"]):
            raise RuntimeError("Application container isolation is incomplete.")

    python_check = (
        "import os,stat; from pathlib import Path; "
        "assert os.geteuid()==os.getegid()==1000; "
        "paths=[Path('/run/secrets')/name for name in ('record_key','task_key')]; "
        "assert all(p.stat().st_uid==p.stat().st_gid==1000 and "
        "stat.S_IMODE(p.stat().st_mode)==0o600 and os.access(p,os.R_OK) and "
        "not os.access(p,os.W_OK) for p in paths)"
    )
    for service in ("api", "worker"):
        command(compose + ["exec", "-T", service, "python", "-c", python_check])
    command(compose + ["exec", "-T", "prometheus", "/bin/sh", "-c",
                       'test "$(id -u)" = 65534 && test "$(stat -c "%u:%g:%a" /run/secrets/metrics_token)" = 65534:65534:600 && test -r /run/secrets/metrics_token && test ! -w /run/secrets/metrics_token'])

    status, body = request("http://127.0.0.1:8273/readyz")
    if status != 200 or json.loads(body).get("status") != "ready":
        raise RuntimeError("The isolated API is not ready.")
    if request("http://127.0.0.1:8273/metrics")[0] != 404:
        raise RuntimeError("Unauthenticated metrics must be refused.")
    status, body = request("http://127.0.0.1:8273/metrics", token)
    if status != 200 or b"zhihe_http_requests_total" not in body:
        raise RuntimeError("Authenticated metrics are unavailable.")

    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            status, body = request('http://127.0.0.1:19090/api/v1/query?query=up%7Bjob%3D%22zhihe-fixture-api%22%7D')
            response = json.loads(body) if status == 200 else {}
            results = response.get("data", {}).get("result", [])
            if response.get("status") == "success" and len(results) == 1 and results[0]["value"][1] == "1":
                print("Application readiness, non-root private secrets, protected metrics, and Prometheus scrape passed.")
                return 0
        except (URLError, ValueError, KeyError):
            pass
        time.sleep(1)
    raise RuntimeError("Prometheus did not complete the isolated API scrape.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="zhihe-ci-application")
    arguments = parser.parse_args()
    try:
        return check(arguments.project)
    except (OSError, ValueError, RuntimeError, KeyError):
        print("Isolated application check failed; credentials and response bodies were not printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
