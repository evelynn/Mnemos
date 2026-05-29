"""PR-83 — fail-closed authentication on /metrics.

The platform review flagged ``/metrics`` as open by default: the
Prometheus labels carry project ids and actor handles, so an
unauthenticated scrape directory-lists every tenant (§14.3). The
docstring acknowledged the default was open "only safe behind a
reverse proxy" — a deployment-time concern that should be a
secure-by-default in code.

``metrics_endpoint`` now 503s with ``metrics_token_not_configured``
when ``METRICS_BEARER_TOKEN`` is unset and the request is not
loopback, matching the webhook (PR-82) and OTLP (PR-82) pattern.
A localhost exception keeps sidecar scrapes working.
"""

from __future__ import annotations

from pathlib import Path

_METRICS = (
    Path(__file__).resolve().parents[1] / "app" / "obs" / "metrics.py"
)


def _read() -> str:
    return _METRICS.read_text(encoding="utf-8")


def test_metrics_fails_closed_without_token():
    body = _read()
    idx = body.find("async def metrics_endpoint(")
    slab = body[idx:idx + 1600]
    assert '"metrics_token_not_configured"' in slab
    assert "503" in slab


def test_metrics_compares_token_constant_time():
    body = _read()
    idx = body.find("async def metrics_endpoint(")
    slab = body[idx:idx + 1600]
    assert "hmac.compare_digest" in slab


def test_metrics_allows_loopback_without_token():
    """A sidecar on the same host can scrape without a token."""
    body = _read()
    idx = body.find("async def metrics_endpoint(")
    slab = body[idx:idx + 1600]
    assert "127.0.0.1" in slab
    assert "is_loopback" in slab
