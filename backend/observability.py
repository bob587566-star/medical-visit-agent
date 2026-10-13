"""Bounded route metrics and structured request metadata, never clinical data."""
from __future__ import annotations

from collections import defaultdict
import hmac
import ipaddress
import json
import logging
import re
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

BUCKETS = (.01, .05, .1, .3, 1.0, 3.0, 10.0, 30.0)
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class RequestMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts = defaultdict(int)
        self.duration_sum = defaultdict(float)
        self.duration_count = defaultdict(int)
        self.duration_buckets = defaultdict(lambda: [0] * len(BUCKETS))

    def observe(self, method: str, route: str, status: int, duration: float) -> None:
        # Route MUST come from an installed route template, not request.url.
        method = method if method in METHODS else "OTHER"
        status = status if 100 <= status <= 599 else 500
        duration = max(0, duration)
        key = (method, route)
        with self.lock:
            self.counts[(method, route, status)] += 1
            self.duration_sum[key] += duration
            self.duration_count[key] += 1
            for index, bucket in enumerate(BUCKETS):
                if duration <= bucket:
                    self.duration_buckets[key][index] += 1

    def render(self) -> str:
        lines = ["# HELP zhihe_http_requests_total HTTP responses by method, installed route template, and status.",
                 "# TYPE zhihe_http_requests_total counter",
                 "# HELP zhihe_http_request_duration_seconds HTTP processing duration in seconds.",
                 "# TYPE zhihe_http_request_duration_seconds histogram"]
        with self.lock:
            for (method, route, status), count in sorted(self.counts.items()):
                labels = f'method="{_escape(method)}",route="{_escape(route)}",status="{status}"'
                lines.append(f"zhihe_http_requests_total{{{labels}}} {count}")
            for (method, route), count in sorted(self.duration_count.items()):
                labels = f'method="{_escape(method)}",route="{_escape(route)}"'
                for index, bucket in enumerate(BUCKETS):
                    lines.append(f'zhihe_http_request_duration_seconds_bucket{{{labels},le="{bucket:g}"}} {self.duration_buckets[(method, route)][index]}')
                lines.append(f'zhihe_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count}')
                lines.append(f"zhihe_http_request_duration_seconds_sum{{{labels}}} {self.duration_sum[(method, route)]:.9f}")
                lines.append(f"zhihe_http_request_duration_seconds_count{{{labels}}} {count}")
        return "\n".join(lines) + "\n"


def attach_observability(app: FastAPI, *, metrics_token: str | None = None,
                         logger: logging.Logger | None = None) -> RequestMetrics:
    """Call once after routes are installed, before the application starts."""
    if metrics_token and len(metrics_token) < 32:
        raise ValueError("Metrics token must be at least 32 characters.")
    if metrics_token and (not metrics_token.isascii() or len(metrics_token) > 256):
        raise ValueError('Metrics token must be bounded ASCII.')
    if getattr(app.state, "request_metrics", None) is not None:
        raise ValueError("Request observability is already attached.")
    metrics = RequestMetrics()
    app.state.request_metrics = metrics
    metadata_logger = logger or logging.getLogger("zhihe.request_metadata")
    if logger is None:
        # Uvicorn does not enable an arbitrary application's INFO logger.
        # Emit pure JSON once to stdout/stderr so platform log rotation can
        # bound storage; do not add an unbounded local file handler.
        if not any(getattr(handler, "_zhihe_metadata", False) for handler in metadata_logger.handlers):
            handler = logging.StreamHandler()
            handler._zhihe_metadata = True
            handler.setFormatter(logging.Formatter("%(message)s"))
            metadata_logger.addHandler(handler)
        metadata_logger.setLevel(logging.INFO)
        metadata_logger.propagate = False

    @app.middleware("http")
    async def metadata(request: Request, call_next):
        started = time.perf_counter()
        status, request_id = 500, None
        try:
            response = await call_next(request)
            status = response.status_code
            candidate = response.headers.get("X-Request-ID", "")
            if re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", candidate):
                request_id = candidate
            return response
        finally:
            # Scope route is the framework's registered template. Unmatched
            # attacker-controlled paths all use ONE bounded label.
            route_object = request.scope.get("route")
            route = getattr(route_object, "path", "unmatched")
            if not isinstance(route, str) or len(route) > 200:
                route = "unmatched"
            method = request.method if request.method in METHODS else "OTHER"
            duration = time.perf_counter() - started
            metrics.observe(method, route, status, duration)
            event = {"event": "http_request", "method": method, "route": route,
                     "status": status, "duration_ms": round(duration * 1000, 2)}
            if request_id:
                event["request_id"] = request_id
            metadata_logger.info(json.dumps(event, ensure_ascii=True, separators=(",", ":")))

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request):
        if metrics_token:
            authorization = request.headers.get("Authorization", "")
            authorized = authorization.isascii() and len(authorization) <= 300 and hmac.compare_digest(authorization, "Bearer " + metrics_token)
        else:
            try:
                authorized = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
            except ValueError:
                authorized = False
        if not authorized:
            return PlainTextResponse("Not found", status_code=404, headers={"Cache-Control": "no-store"})
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4", headers={"Cache-Control": "no-store"})

    return metrics
