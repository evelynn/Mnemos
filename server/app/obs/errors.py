"""Uniform error responses.

Every error body carries the request_id so operators can correlate it with
request telemetry. Unhandled exceptions get a generic ``internal_error``
message, and logs retain only a static failure code so secrets cannot escape
through either client responses or traceback text.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.obs.context import get_request_id

logger = logging.getLogger("mnemos.error")


def _body(status: int, detail, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"status": "error", "detail": detail, "request_id": request_id},
        headers={"x-request-id": request_id},
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    rid = get_request_id()
    # 5xx from explicit raises still log; 4xx stay quiet unless DEBUG.
    if exc.status_code >= 500:
        logger.error(
            "http_error status=%s failure_code=http_request_failed",
            exc.status_code,
        )
        detail = (
            "service_unavailable"
            if exc.status_code == 503
            else "internal_error"
        )
    else:
        # Deliberate 4xx validation/authorization codes are caller-facing
        # product contracts, not arbitrary caught exception diagnostics.
        detail = exc.detail
    return _body(exc.status_code, detail, rid)


def _validation_message(error_type: str) -> str:
    if error_type == "missing":
        return "field_required"
    if "length" in error_type or error_type.startswith(("too_long", "too_short")):
        return "invalid_length"
    if "greater_than" in error_type or "less_than" in error_type:
        return "value_out_of_range"
    if error_type in {"literal_error", "enum"}:
        return "unsupported_value"
    if "type" in error_type or "parsing" in error_type:
        return "invalid_type"
    return "invalid_value"


def _safe_validation_errors(exc: RequestValidationError) -> list[dict]:
    safe: list[dict] = []
    for item in exc.errors():
        raw_type = item.get("type")
        error_type = raw_type if isinstance(raw_type, str) else "validation_error"
        raw_location = item.get("loc")
        location = (
            [part for part in raw_location if type(part) in {str, int}]
            if isinstance(raw_location, (list, tuple))
            else []
        )
        safe.append(
            {
                "type": error_type[:80],
                "loc": location[:16],
                "msg": _validation_message(error_type),
            }
        )
    return safe


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    rid = get_request_id()
    return _body(
        422,
        {"errors": _safe_validation_errors(exc)},
        rid,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = get_request_id()
    logger.error("unhandled request failure_code=internal_error")
    return _body(500, "internal_error", rid)


def install(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
