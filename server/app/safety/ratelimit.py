"""Redis-backed sliding-window rate limiter.

Intended for hot endpoints that either hit production DBs or the LLM
(``/data/query``, ``/refresh_sample``, MCP data tools). The limiter is
per-actor (user id or anonymous client IP) so a single abusive account
cannot starve the others.

Design notes:
- Uses Redis sorted-sets: score = unix-ms timestamp, member = a random
  token. An ``exact-N-per-window`` semantics falls out of trimming
  entries older than ``now - window`` and counting the remainder.
- Configured statically per call site (Phase-A); tuning belongs in
  operator settings later.
- Never fails open silently — if Redis is unreachable, the caller gets
  an HTTP 503 so the abuse path does not quietly bypass the limit.
"""

from __future__ import annotations

import time
import uuid

from fastapi import HTTPException, Request, status

from app.obs.metrics import rate_limited_total
from app.orchestrator.redis_pool import get_redis


async def enforce(
    key: str,
    *,
    limit: int,
    window_sec: int,
) -> None:
    """Raise ``HTTPException(429)`` when the actor exceeds ``limit`` hits.

    ``key`` should be a short, stable identifier (e.g. ``"data.query:u/123"``).
    """
    redis = await get_redis()
    now_ms = int(time.time() * 1000)
    window_ms = window_sec * 1000
    member = uuid.uuid4().hex
    pipe = redis.pipeline()
    pipe.zremrangebyscore(key, 0, now_ms - window_ms)
    pipe.zadd(key, {member: now_ms})
    pipe.zcard(key)
    pipe.expire(key, window_sec + 1)
    try:
        _, _, count, _ = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"rate_limit_unavailable: {exc.__class__.__name__}",
        )

    if int(count) > limit:
        # Scope is usually in ``key`` as the second segment ``rl:<scope>:<actor>``.
        scope = key.split(":", 2)[1] if ":" in key else "unknown"
        rate_limited_total.labels(scope=scope).inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(window_sec)},
        )


def actor_key(request: Request, scope: str) -> str:
    """Derive a per-actor key for ``scope``.

    Prefers the authenticated user id (set by ``current_user`` in
    ``request.state.user``); falls back to the client IP so anonymous
    traffic is still bounded.
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        return f"rl:{scope}:u/{user.id}"
    client = request.client.host if request.client else "unknown"
    return f"rl:{scope}:ip/{client}"
