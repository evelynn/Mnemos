"""OTLP HTTP receiver.

Accepts OTLP/HTTP JSON trace payloads at ``/otlp/v1/traces`` (per the OTLP
spec), scrubs sensitive attributes, and buffers a compact
``(service, operation, kind)`` triple per span in
``runtime_observations`` for the merge stage to reconcile against the
static graph (spec §7.6 exercised-edge upserts — Tier 2 work shipped
in PR-25).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.db import get_session
from app.merge.runtime import assemble_trace_tree, buffer_observations
from app.models.organization import Organization
from app.runtime_receiver.scrub import scrub_span

router = APIRouter(prefix="/otlp/v1", tags=["otlp"])


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _configured_otlp_identities() -> dict[uuid.UUID, bytes]:
    """Load an unambiguous organization → token-digest configuration."""

    raw_map = (os.environ.get("MNEMOS_OTLP_ORG_TOKENS") or "").strip()
    legacy_token = os.environ.get("MNEMOS_OTLP_TOKEN") or ""
    legacy_org = (os.environ.get("MNEMOS_OTLP_ORGANIZATION_ID") or "").strip()
    if raw_map and (legacy_token or legacy_org):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="otlp_token_configuration_invalid",
        )
    entries: dict[uuid.UUID, str]
    if raw_map:
        try:
            parsed = json.loads(raw_map, object_pairs_hook=_strict_object)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="otlp_token_configuration_invalid",
            ) from exc
        if not isinstance(parsed, dict) or not parsed:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="otlp_token_configuration_invalid",
            )
        entries = {}
        for raw_org, token in parsed.items():
            if not isinstance(raw_org, str) or not isinstance(token, str):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="otlp_token_configuration_invalid",
                )
            try:
                org_id = uuid.UUID(raw_org)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="otlp_token_configuration_invalid",
                ) from exc
            if org_id in entries:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="otlp_token_configuration_invalid",
                )
            entries[org_id] = token
    else:
        if bool(legacy_token) != bool(legacy_org):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="otlp_token_configuration_invalid",
            )
        if not legacy_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="otlp_token_not_configured",
            )
        try:
            org_id = uuid.UUID(legacy_org)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="otlp_token_configuration_invalid",
            ) from exc
        entries = {org_id: legacy_token}

    digests: dict[uuid.UUID, bytes] = {}
    seen_tokens: set[str] = set()
    for org_id, token in entries.items():
        if len(token) < 32 or token != token.strip() or len(token) > 4096:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="otlp_token_configuration_invalid",
            )
        if token in seen_tokens:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="otlp_token_configuration_ambiguous",
            )
        seen_tokens.add(token)
        digests[org_id] = hashlib.sha256(token.encode("utf-8")).digest()
    return digests


def _check_otlp_bearer(
    authorization: str | None,
    organization_id_hdr: str | None = None,
) -> uuid.UUID:
    """Authenticate the bearer and derive its tenant; headers cannot choose it."""

    expected = _configured_otlp_identities()
    prefix = "Bearer "
    presented = (
        authorization[len(prefix):]
        if authorization and authorization.startswith(prefix)
        else ""
    )
    if not presented or len(presented) > 4096:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_otlp_token",
        )
    presented_digest = hashlib.sha256(presented.encode("utf-8")).digest()
    matches = [
        org_id
        for org_id, digest in expected.items()
        if hmac.compare_digest(presented_digest, digest)
    ]
    if len(matches) != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_otlp_token",
        )
    bound_org = matches[0]
    if organization_id_hdr is not None:
        try:
            header_org = uuid.UUID(organization_id_hdr)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="otlp_organization_mismatch",
            ) from exc
        if header_org != bound_org:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="otlp_organization_mismatch",
            )
    return bound_org


async def _require_live_organization(
    db: AsyncSession,
    organization_id: uuid.UUID,
) -> None:
    found = (
        await db.execute(
            select(Organization.id).where(Organization.id == organization_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="otlp_organization_unavailable",
        )


@router.post("/traces")
async def receive_traces(
    request: Request,
    db: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_mnemos_organization_id: str | None = Header(default=None),
) -> JSONResponse:
    org_id = _check_otlp_bearer(authorization, x_mnemos_organization_id)
    await _require_live_organization(db, org_id)
    body = await request.json()
    spans_seen = 0
    for resource_span in body.get("resourceSpans", []) or []:
        for scope in resource_span.get("scopeSpans", []) or []:
            for span in scope.get("spans", []) or []:
                _ = scrub_span(span)
                spans_seen += 1

    # Tier 2: assemble the trace tree and buffer the
    # (service, operation, kind) triples for the next merge run.
    buffered = 0
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
        details={
            "organization_id": str(org_id),
            "spans": spans_seen,
            "buffered": buffered,
        },
    )
    return JSONResponse({"accepted": spans_seen, "buffered": buffered})
