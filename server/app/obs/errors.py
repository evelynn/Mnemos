"""Uniform error responses.

Every error body carries the request_id so operators can grep logs for
the same correlation id users see in their UI. Unhandled exceptions get
a generic ``internal_error`` message so stack traces do not leak to
clients — the full exception is still logged server-side at ERROR level.
"""

from __future__ import annotations

import json
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
        logger.error("http_error %s %s", exc.status_code, exc.detail)
    return _body(exc.status_code, exc.detail, rid)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    rid = get_request_id()
    # pydantic v2 puts the original exception object in ``ctx`` (e.g. a
    # field_validator's ValueError), which JSONResponse can't serialize and
    # would turn a clean 422 into a 500. Round-trip through json with
    # ``default=str`` so any non-JSON value degrades to its string form.
    safe_errors = json.loads(json.dumps(exc.errors(), default=str))
    return _body(
        422,
        {"errors": safe_errors},
        rid,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = get_request_id()
    logger.exception("unhandled %s at %s", exc.__class__.__name__, request.url.path)
    return _body(500, "internal_error", rid)


def install(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
