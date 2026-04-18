"""Singleton redis-py async client.

ARQ has its own connection pool for job queues; for non-ARQ uses like
the sliding-window rate limiter or health checks we need plain redis
pipelines, so we keep a second, tiny singleton here.
"""

from __future__ import annotations

import redis.asyncio as redis_asyncio

from app.config import get_settings

_client: redis_asyncio.Redis | None = None


async def get_redis() -> redis_asyncio.Redis:
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis_asyncio.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
