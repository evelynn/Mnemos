"""OTLP HTTP receiver.

Accepts OTLP/HTTP JSON trace payloads at ``/otlp/v1/traces`` (per the OTLP
spec), scrubs sensitive attributes, and publishes a compact summary the merge
engine consumes later. Storing raw spans is intentionally out-of-scope for
Phase 1; we only keep the aggregated edge-exercise state in the graph.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.audit.logger import record as audit_record
from app.runtime_receiver.scrub import scrub_span

router = APIRouter(prefix="/otlp/v1", tags=["otlp"])


@router.post("/traces")
async def receive_traces(request: Request) -> JSONResponse:
    body = await request.json()
    spans_seen = 0
    for resource_span in body.get("resourceSpans", []) or []:
        for scope in resource_span.get("scopeSpans", []) or []:
            for span in scope.get("spans", []) or []:
                _ = scrub_span(span)
                spans_seen += 1
    await audit_record(
        actor="otel_receiver",
        action="otlp.traces",
        details={"spans": spans_seen},
    )
    # Full correlation + exercised-edge upserts land in Week-6 merge v2.
    return JSONResponse({"accepted": spans_seen})
