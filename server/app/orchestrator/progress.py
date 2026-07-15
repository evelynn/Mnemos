"""Redis pub/sub that broadcasts analysis-run progress to SSE subscribers."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

def _channel(run_id: uuid.UUID) -> str:
    return f"mnemos:run:{run_id}"


class ProgressBus:
    def __init__(self) -> None:
        # Clients and pools are process-wide through redis_pool.get_redis().
        # Creating one pool per SSE stream leaked sockets in long-running
        # deployments.  Resolution stays lazy so importing the API does not
        # connect to Redis and local mode still receives its shared fakeredis.
        self._client = None

    async def _get_client(self):
        if self._client is None:
            from app.orchestrator.redis_pool import get_redis

            self._client = await get_redis()
        return self._client

    async def publish(self, run_id: uuid.UUID, event: dict[str, Any]) -> None:
        client = await self._get_client()
        await client.publish(_channel(run_id), json.dumps(event))

    async def subscribe(self, run_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
        client = await self._get_client()
        pubsub = client.pubsub()
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
