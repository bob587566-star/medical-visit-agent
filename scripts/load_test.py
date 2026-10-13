"""Bounded fictional HTTP probe; no third-party model calls or real accounts.

This measures the selected local scenario, not clinical safety, peak production
capacity, model throughput, or a service-level objective.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import statistics
import time
from urllib.parse import urlsplit

import httpx


def percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))] if values else 0


async def run(base_url: str, *, users: int = 5, turns: int = 3, concurrency: int = 5, scenario: str = "chat") -> dict:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password:
        raise ValueError("Only an explicit loopback HTTP target is allowed.")
    if not 1 <= users <= 12 or not 1 <= turns <= 5 or not 1 <= concurrency <= 8:
        raise ValueError("This finite probe is capped at 12 visitors, five turns, eight simultaneous visitors.")
    if scenario not in {"health", "chat"}:
        raise ValueError("Unknown probe scenario.")
    semaphore = asyncio.Semaphore(concurrency)
    timings, statuses, failures = [], [], []
    cleaned = 0
    started = time.perf_counter()

    async def request(client, method, path, **kwargs):
        stamp = time.perf_counter()
        response = await client.request(method, path, **kwargs)
        timings.append((time.perf_counter() - stamp) * 1000)
        statuses.append(response.status_code)
        response.raise_for_status()
        return response

    async def visitor(index):
        nonlocal cleaned
        async with semaphore, httpx.AsyncClient(base_url=base_url, timeout=20, trust_env=False, follow_redirects=False) as client:
            headers = {}
            visit_id = None
            try:
                if scenario == "health":
                    for turn in range(turns):
                        await request(client, "GET", "/readyz" if turn % 2 else "/healthz")
                    return
                bootstrap = (await request(client, "GET", "/api/bootstrap")).json()
                headers = {"X-CSRF-Token": bootstrap["csrf_token"], "Origin": base_url.rstrip("/")}
                privacy = bootstrap.get("privacy")
                if privacy and privacy.get("consent_required"):
                    await request(client, "POST", "/api/privacy/consent", headers=headers,
                                  json={"notice_version": privacy["notice_version"], "local_storage": True,
                                        "external_ai": False, "retention_days": 1})
                visit = (await request(client, "POST", "/api/visits", headers=headers,
                                       json={"title": "虚构有限负载测试"})).json()
                visit_id = visit["id"]
                for turn in range(turns):
                    question = ("你好", "这个助手可以做什么？", "谢谢")[turn % 3]
                    response = (await request(client, "POST", f"/api/visits/{visit_id}/messages", headers=headers,
                                              json={"content": question, "use_model": False})).json()
                    if not (response.get("assistant_message") or {}).get("content"):
                        raise AssertionError("Local response was empty")
            except Exception as error:
                # Do not include URL credentials, health text, or response body.
                failures.append({"virtual_visitor": index, "error_type": type(error).__name__})
            finally:
                if visit_id:
                    try:
                        response = await client.delete(f"/api/visits/{visit_id}", headers=headers)
                        if response.status_code != 204:
                            raise AssertionError("Fictional visit cleanup failed")
                        cleaned += 1
                        if privacy and privacy.get("consent_required"):
                            withdrawal = await client.delete("/api/privacy/consent", headers=headers)
                            if withdrawal.status_code != 200:
                                raise AssertionError("Fictional consent cleanup failed")
                    except Exception as error:
                        failures.append({"virtual_visitor": index, "error_type": "cleanup_" + type(error).__name__})

    await asyncio.gather(*(visitor(index) for index in range(users)))
    elapsed = time.perf_counter() - started
    return {"scenario": scenario, "scope": "finite loopback probe; fictional visitors; use_model=false",
            "virtual_visitors": users, "turns_per_visitor": turns, "visitor_concurrency": concurrency,
            "requests_measured": len(timings), "status_counts": {str(status): statuses.count(status) for status in sorted(set(statuses))},
            "elapsed_seconds": round(elapsed, 3), "requests_per_second": round(len(timings) / elapsed, 2),
            "latency_ms": {"p50": round(percentile(timings, .5), 2), "p95": round(percentile(timings, .95), 2),
                           "max": round(max(timings, default=0), 2)},
            "failures": failures, "fictional_visits_deleted": cleaned,
            "notice": "Only this small scenario was measured. These numbers do not establish maximum capacity, external model throughput, a clinical guarantee, or high availability."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--users", type=int, default=5)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--scenario", choices=("health", "chat"), default="chat")
    parser.add_argument("--out", type=Path, default=Path("artifacts/capacity-check-20261013.json"))
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.base_url, users=args.users, turns=args.turns, concurrency=args.concurrency, scenario=args.scenario))
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"requests_measured": report["requests_measured"], "failures": len(report["failures"]), "latency_ms": report["latency_ms"]}))
        return 1 if report["failures"] else 0
    except Exception as error:
        print("Finite probe failed: " + type(error).__name__ + "; no credentials/clinical payloads printed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
