"""OTLP HTTP receiver.

Accepts OTLP/HTTP JSON trace payloads at ``/otlp/v1/traces`` (per the OTLP
spec), scrubs sensitive attributes, and buffers a compact
``(service, operation, kind)`` triple per span in
``runtime_observations`` for the merge stage to reconcile against the
static graph (spec §7.6 exercised-edge upserts — Tier 2 work shipped
in PR-25).
"""

from __future__ import annotations

import hmac
import os
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.db import get_session
from app.merge.runtime import assemble_trace_tree, buffer_observations
from app.runtime_receiver.scrub import scrub_span

router = APIRouter(prefix="/otlp/v1", tags=["otlp"])


async def _resolve_org_and_project(
    db: AsyncSession, organization_id_hdr: str | None
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Resolve the tenant from the ``X-Mnemos-Organization-Id`` header.

    OTLP doesn't natively carry tenant context, so we accept it via a
    custom header for now. Project id is intentionally not derived
    here — a single org can run many projects against the same OTel
    collector, and the merge stage figures out which project a given
    (service, operation) pair belongs to at reconcile time.

    Returns ``(None, None)`` when the header is absent or malformed
    so the caller can fall back to the audit-only path. Pre-runtime-
    receiver deployments still work; the buffer is just empty.
    """
    if not organization_id_hdr:
        return None, None
    try:
        return uuid.UUID(organization_id_hdr), None
    except (TypeError, ValueError):
        return None, None


def _check_otlp_bearer(authorization: str | None) -> None:
    """Reject any OTLP trace that doesn't carry the deployment's shared
    bearer. Fail-closed: if ``MNEMOS_OTLP_TOKEN`` isn't set in the
    environment, no trace is accepted — the prior open default let any
    process on the OTLP port poison ``runtime_observations`` and lift
    edges to ``exercised=true`` for another tenant (§14.3)."""
    expected = os.environ.get("MNEMOS_OTLP_TOKEN") or ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="otlp_token_not_configured",
        )
    prefix = "Bearer "
    presented = (
        authorization[len(prefix):]
        if authorization and authorization.startswith(prefix)
        else ""
    )
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_otlp_token",
        )


@router.post("/traces")
async def receive_traces(
    request: Request,
    db: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_mnemos_organization_id: str | None = Header(default=None),
) -> JSONResponse:
    _check_otlp_bearer(authorization)
    body = await request.json()
    spans_seen = 0
    for resource_span in body.get("resourceSpans", []) or []:
        for scope in resource_span.get("scopeSpans", []) or []:
            for span in scope.get("spans", []) or []:
                _ = scrub_span(span)
                spans_seen += 1

    # Tier 2: assemble the trace tree and buffer the
    # (service, operation, kind) triples for the next merge run.
    # Tenant resolution is required — anonymous spans aren't routable
    # to a project, so we audit them but don't write to the table.
    org_id, _ = await _resolve_org_and_project(db, x_mnemos_organization_id)
    buffered = 0
    if org_id is not None:
        records = assemble_trace_tree(body.get("resourceSpans", []) or [])
        try:
            buffered = await buffer_observations(db, org_id, records)
            await db.commit()
        except Exception:
            # Spans aren't fail-stop traffic — never let a buffer
            # error break the receiver. The merge stage replays
            # whatever is in the table next time around.
            await db.rollback()

    await audit_record(
        actor="otel_receiver",
        action="otlp.traces",
        details={"spans": spans_seen, "buffered": buffered},
    )
    return JSONResponse({"accepted": spans_seen, "buffered": buffered})
