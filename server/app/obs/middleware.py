"""Request-id + structured access log middleware.

Sits ahead of AuditMiddleware so every handled request (including
failures) carries a correlation id in both logs and error bodies.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.obs.context import request_id_ctx

logger = logging.getLogger("mnemos.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Honour a caller-supplied id to preserve traces across services,
        # but regenerate for anything non-hex to keep log fields sane.
        incoming = request.headers.get("x-request-id", "").strip()
        rid = incoming if _is_safe_id(incoming) else uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        request.state.request_id = rid

        started = time.perf_counter()
        try:
            response: Response = await call_next(request)
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            try:
                logger.info(
                    "request",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status": getattr(locals().get("response"), "status_code", 0),
                        "duration_ms": duration_ms,
                        "client": request.client.host if request.client else None,
                    },
                )
            except Exception:  # pragma: no cover — never let logging break the response
                pass
            request_id_ctx.reset(token)

        response.headers["x-request-id"] = rid
        return response


def _is_safe_id(candidate: str) -> bool:
    if not (8 <= len(candidate) <= 64):
        return False
    return all(c.isalnum() or c in "-_" for c in candidate)
