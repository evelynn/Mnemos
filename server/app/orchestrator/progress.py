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
        self._client = redis_asyncio.from_url(_settings.redis_url, decode_responses=True)

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
