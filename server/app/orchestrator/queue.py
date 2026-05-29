"""Thin wrapper that lazily builds a singleton ARQ client pool."""

from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import get_settings

_settings = get_settings()
_pool: ArqRedis | None = None


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(_settings.redis_url)


async def get_queue() -> ArqRedis:
    global _pool
    # PR-135 — docker-free local mode runs jobs inline (asyncio
    # background task on the API loop) instead of via a Redis-backed
    # ARQ worker. The shim is ``enqueue_job``-compatible so callers
    # don't branch.
    from app.local_mode import is_local_mode

    if is_local_mode():
        from app.local_mode import get_inline_queue

        return get_inline_queue()  # type: ignore[return-value]
    if _pool is None:
        _pool = await create_pool(_redis_settings())
    return _pool
