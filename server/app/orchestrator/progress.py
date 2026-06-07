"""Redis pub/sub that broadcasts analysis-run progress to SSE subscribers."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis_asyncio

from app.config import get_settings

_settings = get_settings()


def _channel(run_id: uuid.UUID) -> str:
    return f"mnemos:run:{run_id}"


class ProgressBus:
    def __init__(self) -> None:
        # Local mode has no real Redis — use the in-process fakeredis server
        # so analysis-run progress pub/sub works docker-free. Mirrors the
        # branch in redis_pool.get_redis() / auth.sessions._client(); without
        # it the inline job crashes dialing localhost:6379 and the run hangs
        # in "running" forever.
        from app.local_mode import is_local_mode

        if is_local_mode():
            from app.local_mode import get_fake_redis

            self._client = get_fake_redis()
        else:
            self._client = redis_asyncio.from_url(
                _settings.redis_url, decode_responses=True
            )

    async def publish(self, run_id: uuid.UUID, event: dict[str, Any]) -> None:
        await self._client.publish(_channel(run_id), json.dumps(event))

    async def subscribe(self, run_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
        pubsub = self._client.pubsub()
        await pubsub.subscribe(_channel(run_id))
        try:
            async for message in pubsub.listen():
                if message is None or message.get("type") != "message":
                    continue
                data = message.get("data")
                if not data:
                    continue
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    yield {"raw": data}
        finally:
            await pubsub.unsubscribe(_channel(run_id))
            await pubsub.close()
