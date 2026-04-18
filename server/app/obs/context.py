"""Per-request correlation id propagated via ContextVar.

Using a ContextVar (rather than stashing the id on ``request.state``) means
the id survives into background tasks, ARQ jobs spawned during a request,
and any logging invoked from nested libraries.
"""

from __future__ import annotations

import contextvars

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mnemos_request_id", default="-"
)


def get_request_id() -> str:
    return request_id_ctx.get()
