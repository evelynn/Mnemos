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

import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
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


async def metrics_endpoint() -> Response:
    """Serve the Prometheus scrape endpoint.

    Supports multiprocess (uvicorn --workers N) when the
    ``PROMETHEUS_MULTIPROC_DIR`` env var is set — otherwise falls back
    to the process-local default registry.
    """
    import os

    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        data = generate_latest(registry)
    else:
        data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
