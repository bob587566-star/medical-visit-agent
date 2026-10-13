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


CURRENT_CASE = "initialization"


def case(name: str) -> None:
    global CURRENT_CASE
    CURRENT_CASE = name
    print("[application-runtime] checking " + name, flush=True)


def isolated(settings: dict) -> bool:
    # Docker engines may normalize capability names differently.
    caps = {str(cap).upper().removeprefix("CAP_") for cap in settings.get("caps") or []}
    return (settings.get("readonly") is True and "ALL" in caps and any(
        option.startswith("no-new-privileges") for option in settings.get("security") or []))


def check(project: str) -> int:
    compose = ["docker", "compose", "-p", project, "-f", "compose.production.yaml", "--profile", "application"]
    opener = build_opener(ProxyHandler({}))

    def command(arguments: list[str]) -> str:
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            print("[application-runtime] container command exit code=" + str(result.returncode), flush=True)
            raise RuntimeError("An isolated container inspection failed.")
        return result.stdout.strip()

    def request(url: str, token: str | None = None) -> tuple[int, bytes]:
        headers = {"Authorization": "Bearer " + token} if token else {}
        try:
            with opener.open(Request(url, headers=headers), timeout=3) as response:
                return response.status, response.read(1_000_000)
        except HTTPError as error:
            return error.code, b""

    case("original-file-permissions")
    paths = [Path("data/runtime-profile/data.keys.json"), Path("data/runtime-profile/tasks.key"),
             Path("data/runtime-metrics-token.private"), Path("data/runtime-redis.conf")]
    if os.name == "posix" and any(stat.S_IMODE(path.stat().st_mode) != 0o600 for path in paths):
        raise RuntimeError("Original fixture files must remain private.")
    case("metrics-token-format")
    token = paths[2].read_text(encoding="ascii").strip()
    if not 32 <= len(token) <= 256:
        raise RuntimeError("The isolated metrics token is invalid.")

    inspection = ('{"readonly":{{json .HostConfig.ReadonlyRootfs}},'
                  '"caps":{{json .HostConfig.CapDrop}},"security":{{json .HostConfig.SecurityOpt}}}')
    for service in ("api", "worker", "prometheus"):
        case(service + "-container-isolation")
        container = command(compose + ["ps", "-q", service])
        if not container or "\n" in container:
            raise RuntimeError("Expected one isolated container per service.")
        settings = json.loads(command(["docker", "inspect", "--format", inspection, container]))
        caps = {str(cap).upper().removeprefix("CAP_") for cap in settings.get("caps") or []}
        print("[application-runtime] " + service + " readonly=" + str(settings.get("readonly") is True) +
              " drop-all=" + str("ALL" in caps) + " no-new-privileges=" + str(any(
                  option.startswith("no-new-privileges") for option in settings.get("security") or [])), flush=True)
        if not isolated(settings):
            raise RuntimeError("Application container isolation is incomplete.")

    python_check = (
        "import os,stat,json; from pathlib import Path; "
        "paths=[Path('/run/secrets')/name for name in ('record_key','task_key')]; "
        "print(json.dumps({'uid':os.geteuid(),'gid':os.getegid(),'files':"
        "[{'uid':p.stat().st_uid,'gid':p.stat().st_gid,'mode':stat.S_IMODE(p.stat().st_mode),"
        "'readable':os.access(p,os.R_OK),'writable':os.access(p,os.W_OK)} for p in paths]}))"
    )
    for service in ("api", "worker"):
        case(service + "-secret-ownership-and-readonly")
        metadata = json.loads(command(compose + ["exec", "-T", service, "python", "-c", python_check]))
        # Only numeric ownership/mode and access flags are emitted; never file contents.
        print("[application-runtime] " + service + " uid=" + str(int(metadata["uid"])) +
              " gid=" + str(int(metadata["gid"])), flush=True)
        for index, file in enumerate(metadata["files"]):
            print("[application-runtime] " + service + " secret-index=" + str(index) +
                  " uid=" + str(int(file["uid"])) + " gid=" + str(int(file["gid"])) +
                  " mode=" + oct(int(file["mode"])) + " readable=" + str(file["readable"] is True) +
                  " writable=" + str(file["writable"] is True), flush=True)
        if metadata["uid"] != 1000 or metadata["gid"] != 1000 or len(metadata["files"]) != 2 or not all(
                file["uid"] == file["gid"] == 1000 and file["mode"] == 0o600 and
                file["readable"] is True and file["writable"] is False for file in metadata["files"]):
            raise RuntimeError("Non-root application secret access is incomplete.")
    case("prometheus-secret-ownership-and-readonly")
    prometheus_check = (
        'file=/run/secrets/metrics_token; readable=false; writable=false; '
        'if test -r "$file"; then readable=true; fi; '
        # BusyBox test -w checks mode bits and may ignore a read-only mount.
        # Opening for append without writing bytes probes actual filesystem
        # access; it preserves the token contents even if the mount is wrong.
        'if ( : >> "$file" ) 2>/dev/null; then writable=true; fi; '
        'printf "uid=%s gid=%s owner=%s readable=%s writable=%s\\n" '
        '"$(id -u)" "$(id -g)" "$(stat -c "%u:%g:%a" "$file")" "$readable" "$writable"; '
        'test "$(id -u)" = 65534 && test "$(id -g)" = 65534 && '
        'test "$(stat -c "%u:%g:%a" "$file")" = 65534:65534:600 && '
        'test "$readable" = true && test "$writable" = false'
    )
    command(compose + ["exec", "-T", "prometheus", "/bin/sh", "-c", prometheus_check])

    case("api-readiness")
    status, body = request("http://127.0.0.1:8273/readyz")
    print("[application-runtime] readiness HTTP status=" + str(status), flush=True)
    if status != 200 or json.loads(body).get("status") != "ready":
        raise RuntimeError("The isolated API is not ready.")
    case("unauthenticated-metrics-refused")
    if request("http://127.0.0.1:8273/metrics")[0] != 404:
        raise RuntimeError("Unauthenticated metrics must be refused.")
    case("authenticated-metrics")
    status, body = request("http://127.0.0.1:8273/metrics", token)
    print("[application-runtime] authenticated metrics HTTP status=" + str(status), flush=True)
    if status != 200 or b"zhihe_http_requests_total" not in body:
        raise RuntimeError("Authenticated metrics are unavailable.")

    case("prometheus-live-scrape")
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
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print("[application-runtime] failed " + CURRENT_CASE + " (" + type(error).__name__ +
              "); credentials, subprocess errors and response bodies were not printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
