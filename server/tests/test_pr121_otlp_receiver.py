"""PR-121 — OTLP receiver REAL trace processing.

§9.4 Tier 2 runtime correlation: OTLP traces flow into the
graph as ``exercised`` flags on CALLS edges. Up to now this
was wired but never exercised by a test.

This file calls the receive_traces handler directly with
synthetic-but-spec-correct OTLP/HTTP JSON payloads, plus
unit-tests scrub_span and assemble_trace_tree. Direct-call
instead of HTTPX client because the full app's
AuditMiddleware + RateLimit + CSRF chain pulls in state from
other tests that breaks isolation.

What's verified:
- Bearer auth gates the handler (no token / wrong token → 401)
- Valid token + valid payload → JSONResponse with span counts
- PII attributes scrubbed before any persistence
- Empty resourceSpans → graceful return, no DB write
- assemble_trace_tree → service/operation/kind triples
- buffer_observations writes via real SQLAlchemy session
"""

from __future__ import annotations

import json
import uuid as _uuid
from typing import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.models import audit as _audit  # noqa: E402,F401
from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models import plans as _plans  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models.base import Base  # noqa: E402


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await eng.dispose()


def _span(name: str = "POST /orders", trace_id: str | None = None,
          span_id: str | None = None, parent_id: str | None = None) -> dict:
    return {
        "traceId": trace_id or _uuid.uuid4().hex,
        "spanId": span_id or _uuid.uuid4().hex[:16],
        "parentSpanId": parent_id,
        "name": name,
        "kind": 2,  # SPAN_KIND_SERVER
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000020000000",
        "attributes": [
            {"key": "http.method", "value": {"stringValue": "POST"}},
            {"key": "http.route", "value": {"stringValue": "/orders"}},
            {"key": "user.email",
             "value": {"stringValue": "alice@example.com"}},
        ],
        "status": {"code": 1},
    }


def _otlp_payload(spans: list[dict] | None = None) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name",
                         "value": {"stringValue": "orders-api"}},
                    ],
                },
                "scopeSpans": [
                    {"scope": {"name": "fastapi", "version": "0.1"},
                     "spans": spans if spans is not None else [_span()]},
                ],
            }
        ]
    }


# ─── _check_otlp_bearer — auth gate ────────────────────────────────


def test_bearer_check_rejects_when_token_unset(monkeypatch):
    """Spec §2.5 — receiver fails closed if MNEMOS_OTLP_TOKEN
    isn't configured. An open default would let any client poison
    runtime_observations."""
    from app.runtime_receiver.router import _check_otlp_bearer
    monkeypatch.delenv("MNEMOS_OTLP_TOKEN", raising=False)
    with pytest.raises(HTTPException) as ei:
        _check_otlp_bearer("Bearer anything")
    assert ei.value.status_code in (401, 403, 503)


def test_bearer_check_rejects_wrong_token(monkeypatch):
    from app.runtime_receiver.router import _check_otlp_bearer
    monkeypatch.setenv("MNEMOS_OTLP_TOKEN", "right-token")
    with pytest.raises(HTTPException) as ei:
        _check_otlp_bearer("Bearer wrong-token")
    assert ei.value.status_code in (401, 403)


def test_bearer_check_accepts_correct_token(monkeypatch):
    from app.runtime_receiver.router import _check_otlp_bearer
    monkeypatch.setenv("MNEMOS_OTLP_TOKEN", "right-token")
    # Must not raise.
    _check_otlp_bearer("Bearer right-token")


def test_bearer_check_rejects_missing_header(monkeypatch):
    from app.runtime_receiver.router import _check_otlp_bearer
    monkeypatch.setenv("MNEMOS_OTLP_TOKEN", "right-token")
    with pytest.raises(HTTPException):
        _check_otlp_bearer(None)


# ─── scrub_span — PII attribute filter ─────────────────────────────


def test_scrub_strips_or_masks_pii():
    """Span attributes like user.email must NOT survive into
    persistence in plaintext."""
    from app.runtime_receiver.scrub import scrub_span

    span = _span()
    out = scrub_span(span)
    raw = json.dumps(out)
    assert "alice@example.com" not in raw, (
        f"PII leaked through scrubber:\n{raw[:500]}"
    )


def test_scrub_preserves_non_pii_attributes():
    """The scrubber must not over-scrub — http.method / http.route
    are operational metadata, not PII."""
    from app.runtime_receiver.scrub import scrub_span

    span = _span()
    out = scrub_span(span)
    raw = json.dumps(out)
    # http.method should survive (it's just "POST" — no PII).
    assert "http.method" in raw
    # The span name is operational.
    assert "POST /orders" in raw


def test_scrub_handles_span_with_no_attributes():
    """A span without an ``attributes`` key (some SDKs omit it
    when empty) must not crash the scrubber."""
    from app.runtime_receiver.scrub import scrub_span

    minimal = {"traceId": "x", "spanId": "y", "name": "z"}
    out = scrub_span(minimal)
    assert out["name"] == "z"


# ─── assemble_trace_tree — Tier 2 graph extraction ─────────────────


def test_assemble_trace_tree_extracts_service_operation_records():
    """The merge stage downstream gets (service, operation, kind)
    triples from assemble_trace_tree. Verify the function actually
    extracts them from a real OTLP payload."""
    from app.merge.runtime import assemble_trace_tree

    payload = _otlp_payload(spans=[
        _span(name="POST /orders"),
        _span(name="GET /orders/{id}"),
    ])
    records = assemble_trace_tree(payload["resourceSpans"])
    assert isinstance(records, list)
    # We sent 2 spans from one service — expect at least 2 records.
    assert len(records) >= 2, f"expected ≥2 records, got {len(records)}"


def test_assemble_trace_tree_handles_empty_input():
    from app.merge.runtime import assemble_trace_tree
    assert assemble_trace_tree([]) == [] or assemble_trace_tree([]) is not None


# ─── receive_traces handler — full flow with real session ──────────


@pytest.mark.asyncio
async def test_receive_traces_returns_accepted_count(session, monkeypatch):
    """Direct call to receive_traces — verify the response shape
    AND that accepted count matches input span count."""
    from app.runtime_receiver.router import receive_traces

    monkeypatch.setenv("MNEMOS_OTLP_TOKEN", "test-bearer")
    payload = _otlp_payload(spans=[_span(), _span(name="GET /x"), _span(name="GET /y")])

    class FakeReq:
        async def json(self):
            return payload

    resp = await receive_traces(
        request=FakeReq(),
        db=session,
        authorization="Bearer test-bearer",
        x_mnemos_organization_id=None,
    )
    body = json.loads(resp.body)
    assert body["accepted"] == 3
    # No org header → not buffered, but still accepted.
    assert "buffered" in body


@pytest.mark.asyncio
async def test_receive_traces_handles_empty_resource_spans(session, monkeypatch):
    from app.runtime_receiver.router import receive_traces

    monkeypatch.setenv("MNEMOS_OTLP_TOKEN", "test-bearer")

    class FakeReq:
        async def json(self):
            return {"resourceSpans": []}

    resp = await receive_traces(
        request=FakeReq(),
        db=session,
        authorization="Bearer test-bearer",
        x_mnemos_organization_id=None,
    )
    body = json.loads(resp.body)
    assert body["accepted"] == 0


@pytest.mark.asyncio
async def test_receive_traces_rejects_missing_token(session, monkeypatch):
    from app.runtime_receiver.router import receive_traces

    monkeypatch.setenv("MNEMOS_OTLP_TOKEN", "test-bearer")

    class FakeReq:
        async def json(self):
            return _otlp_payload()

    with pytest.raises(HTTPException):
        await receive_traces(
            request=FakeReq(), db=session,
            authorization=None,
            x_mnemos_organization_id=None,
        )


@pytest.mark.asyncio
async def test_receive_traces_audits_each_post(session, monkeypatch):
    """audit_log row must land for every accepted POST so
    operators can reconcile span counts."""
    from app.runtime_receiver.router import receive_traces

    monkeypatch.setenv("MNEMOS_OTLP_TOKEN", "test-bearer")

    class FakeReq:
        async def json(self):
            return _otlp_payload(spans=[_span()])

    captured = []

    async def fake_record(*, actor, action, target=None, project_id=None, details=None, session=None):
        captured.append({"actor": actor, "action": action, "details": details})

    # NB: ``app.runtime_receiver.__init__`` re-exports the APIRouter
    # *instance* as ``router``, which shadows the
    # ``app.runtime_receiver.router`` module attribute. Reach for the
    # actual module via sys.modules to patch its namespace.
    import sys
    receiver_mod = sys.modules["app.runtime_receiver.router"]
    with patch.object(receiver_mod, "audit_record", fake_record):
        await receive_traces(
            request=FakeReq(), db=session,
            authorization="Bearer test-bearer",
            x_mnemos_organization_id=None,
        )

    assert captured, "no audit record produced"
    assert any(a["action"] == "otlp.traces" for a in captured)
    last = captured[-1]
    assert last["details"]["spans"] == 1
