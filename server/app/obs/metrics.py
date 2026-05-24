"""Prometheus metrics.

Exposes the core four that a single-org deployment needs to alert on:
- ``mnemos_http_requests_total{method,path,status}`` — traffic + 4xx/5xx rate
- ``mnemos_http_request_duration_seconds`` — latency histogram
- ``mnemos_analysis_runs_total{status}`` — worker throughput
- ``mnemos_rate_limited_total{scope}`` — abuse / saturation signal

Kept intentionally small — adding a metric has a cost (label cardinality,
scrape time, alert fatigue). Feature-specific metrics belong closer to
the feature code, not this module.
"""

from __future__ import annotations

import hmac
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request  # noqa: F401  used in metrics_endpoint signature
from starlette.responses import Response

http_requests_total = Counter(
    "mnemos_http_requests_total",
    "HTTP requests by method, route template, and status bucket",
    labelnames=("method", "path", "status"),
)

http_request_duration_seconds = Histogram(
    "mnemos_http_request_duration_seconds",
    "HTTP request latency (seconds)",
    labelnames=("method", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

analysis_runs_total = Counter(
    "mnemos_analysis_runs_total",
    "Analysis runs completed by terminal status",
    labelnames=("status",),
)

rate_limited_total = Counter(
    "mnemos_rate_limited_total",
    "Requests rejected by the rate limiter by scope",
    labelnames=("scope",),
)

# Webhook -> worker pickup lag. Backs the Grafana panel added in PR-10.
# The histogram buckets are sized for the 0-10 minute window where the
# operator-visible "is my push being analysed?" question lives.
ingest_lag_seconds = Histogram(
    "mnemos_ingest_lag_seconds",
    "Seconds between an ingest AnalysisRun being queued and the worker "
    "picking it up",
    labelnames=("source",),
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)

# How many ProjectDB bindings the daily probe sweep has currently flagged
# as RW (and therefore unsafe). Gauge rather than Counter so the panel
# tracks the *current* fleet size, not cumulative disable events.
project_db_disabled = Gauge(
    "mnemos_project_db_disabled_total",
    "Current count of project_db rows with disabled_at set",
)

# Tracks contention on the per-cron advisory lock. A non-zero rate is
# expected and *desired* under a multi-worker deployment — it means the
# leader-election story is keeping doubles out.
cron_lock_lost_total = Counter(
    "mnemos_cron_lock_lost_total",
    "Cron iterations that failed to acquire the advisory lock",
    labelnames=("job",),
)

# Break-glass workflow visibility. Three labelled actions:
#   - "issued"   on POST /break_glass_grant success
#   - "consumed" when the approve endpoint's atomic UPDATE returns a row
#   - "expired"  when the cron sweep marks an unused grant as consumed
break_glass_grants_total = Counter(
    "mnemos_break_glass_grants_total",
    "Break-glass grants by action (issued/consumed/expired)",
    labelnames=("action",),
)

# PR-94 — append-only audit (§14.4) failures. The previous logger
# swallowed write errors silently; this counter + the structured log
# entry give an operator a visible "audit went dark" signal.
audit_write_failures_total = Counter(
    "mnemos_audit_write_failures_total",
    "Audit-log inserts that failed (and were rolled back) per action.",
    labelnames=("action",),
)


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Records the HTTP counter and histogram for every handled request.

    Uses the FastAPI route template (``/projects/{project_id}/...``)
    rather than the raw URL so high-cardinality path labels don't blow
    up the metric store.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        started = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration = time.perf_counter() - started
            path_template = _route_template(request)
            status = response.status_code if response is not None else 500
            # Bucket status into 1xx/2xx/3xx/4xx/5xx to keep cardinality
            # low; operators still see the exact code in logs.
            status_bucket = f"{status // 100}xx"
            http_requests_total.labels(
                method=request.method, path=path_template, status=status_bucket
            ).inc()
            http_request_duration_seconds.labels(
                method=request.method, path=path_template
            ).observe(duration)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return request.url.path


async def metrics_endpoint(request: "Request") -> Response:
    """Serve the Prometheus scrape endpoint.

    Authentication is fail-closed: ``METRICS_BEARER_TOKEN`` MUST be set
    and the request must present ``Authorization: Bearer <token>``.
    Metrics labels carry project ids and actor handles — leaving the
    scrape open by default lets anyone with reachability directory-list
    every tenant (§14.3). A localhost-only loopback exception keeps
    same-host monitoring sidecars working without a token.

    Supports multiprocess (uvicorn --workers N) when the
    ``PROMETHEUS_MULTIPROC_DIR`` env var is set — otherwise falls back
    to the process-local default registry.
    """
    import os

    from app.config import get_settings

    expected = get_settings().metrics_bearer_token
    client = request.client
    is_loopback = bool(client) and client.host in ("127.0.0.1", "::1", "localhost")
    if not expected and not is_loopback:
        return Response(status_code=503, content=b"metrics_token_not_configured")
    if expected:
        provided = request.headers.get("authorization", "")
        if not hmac.compare_digest(provided, f"Bearer {expected}"):
            return Response(status_code=401, content=b"unauthorized")

    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        data = generate_latest(registry)
    else:
        data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
