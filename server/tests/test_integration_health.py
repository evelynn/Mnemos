"""Integration smoke tests — health endpoints.

Marked ``integration`` so they run in CI (where Postgres/Redis service
containers are live) but can be skipped locally via
``MNEMOS_SKIP_INTEGRATION=1 pytest``.
"""

import pytest

pytestmark = pytest.mark.integration


async def test_health_liveness(http_client):
    r = await http_client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readiness_returns_structured_checks(http_client):
    r = await http_client.get("/api/v1/health/ready")
    # 200 when everything is up, 503 when a dep is down — either way the
    # response body shape must be stable so LBs can parse it.
    assert r.status_code in (200, 503)
    body = r.json()
    # PR-99 added "crypto" and "analyzers"; the LB contract guarantees
    # the original three keys remain present, not that the set is closed.
    assert {"database", "redis", "worker", "llm_paid_dispatch"} <= set(
        body["checks"].keys()
    )
    for part in body["checks"].values():
        assert {"ok", "message"} <= part.keys()


async def test_request_id_header_echoed(http_client):
    r = await http_client.get("/api/v1/health")
    rid = r.headers.get("x-request-id")
    assert rid and len(rid) >= 8


async def test_request_id_honoured_when_supplied(http_client):
    supplied = "abc12345xyz"
    r = await http_client.get(
        "/api/v1/health", headers={"x-request-id": supplied}
    )
    assert r.headers.get("x-request-id") == supplied
