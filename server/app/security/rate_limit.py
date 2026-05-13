"""Global rate-limit middleware (PR-46, closes audit E2).

Phase 1 already had a per-endpoint ``rl_enforce`` helper for the
data-query path. The 14th-round audit's E2 finding was that the
state-changing endpoints — invites, comments, role changes,
break-glass — had no shared throttle, so an authenticated misuser
could hammer them. This middleware closes that gap with a single
token-bucket-ish counter scoped to ``(user_or_ip, endpoint_group)``.

Design notes:

* GET / HEAD / OPTIONS pass through. Read-only traffic is not
  the threat model here.
* Default: 60 mutations per minute, per session. A normal
  operator session bumps maybe 5-10/min; 60 is generous enough
  to never fire on benign use, tight enough that a runaway
  script trips the limit within one second.
* Endpoint groups (``/api/v1/comments``, ``/api/v1/invites``,
  ``/api/v1/users``) get their own counters so a flood of one
  doesn't starve the others.
* The bucket lives in Redis; a Redis outage *fails open* the
  same way ``brute_force.is_locked`` does — the audit team was
  explicit that locking everyone out on a Redis blip is worse
  than letting throttle slip during the blip.
"""

from __future__ import annotations

import os
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.orchestrator.redis_pool import get_redis

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Endpoint-group resolution. Order matters — more specific
# prefixes go first so ``/api/v1/users/{id}/role`` matches the
# ``users`` group, not the catch-all ``/api/v1``.
_GROUPS: tuple[tuple[str, str], ...] = (
    ("/api/v1/invites", "invites"),
    ("/api/v1/comments", "comments"),
    ("/api/v1/users", "users"),
    ("/api/v1/diff_submissions", "diffs"),
    ("/api/v1/break_glass", "break_glass"),
    ("/api/v1/auth", "auth"),
    ("/api/v1", "api"),
)

DEFAULT_LIMIT = int(os.environ.get("MNEMOS_GLOBAL_RATE_LIMIT", "60"))
WINDOW_SEC = int(os.environ.get("MNEMOS_GLOBAL_RATE_WINDOW", "60"))


def _group_for(path: str) -> str:
    for prefix, name in _GROUPS:
        if path.startswith(prefix):
            return name
    return "other"


def _actor_for(request: Request) -> str:
    """Throttle key — session cookie when authenticated, IP
    otherwise. The session cookie is opaque to the operator, so
    using it as the key doesn't leak the user_id."""
    # The session cookie name lives in settings; we read it lazily
    # so this module doesn't depend on every test importing
    # ``get_settings``.
    from app.config import get_settings

    cookie_name = get_settings().session_cookie_name
    token = request.cookies.get(cookie_name)
    if token:
        return "sess:" + token[:24]
    client = request.client
    return "ip:" + (client.host if client else "unknown")


def _exempt_paths() -> Iterable[str]:
    """Paths that bypass the global limit because their own
    pipeline already throttles them tighter (login lockout) or
    because they're high-volume read traffic disguised as POST
    (OTLP)."""
    return (
        "/api/v1/auth/login",   # brute_force counter is tighter
        "/api/v1/auth/logout",  # no need to throttle log-out
        "/api/v1/health",
        "/metrics",
        "/otlp/",
        "/api/v1/webhooks/",
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limit: int = DEFAULT_LIMIT, window_sec: int = WINDOW_SEC):
        super().__init__(app)
        self.limit = limit
        self.window_sec = window_sec

    async def dispatch(self, request: Request, call_next) -> Response:
        method = request.method.upper()
        if method in _SAFE_METHODS:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in _exempt_paths()):
            return await call_next(request)

        actor = _actor_for(request)
        group = _group_for(path)
        key = f"mnemos:rl:{actor}:{group}"

        try:
            redis = await get_redis()
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, self.window_sec)
        except Exception:  # noqa: BLE001
            # Fail open on Redis outage — same posture as the
            # brute-force counter (PR-45 audit closure).
            return await call_next(request)

        if count > self.limit:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "rate_limit_exceeded",
                    "group": group,
                    "retry_after_sec": self.window_sec,
                },
                headers={"Retry-After": str(self.window_sec)},
            )
        return await call_next(request)
