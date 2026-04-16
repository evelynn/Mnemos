import secrets
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis_asyncio

from app.config import get_settings

_settings = get_settings()
_redis: redis_asyncio.Redis | None = None


def _client() -> redis_asyncio.Redis:
    global _redis
    if _redis is None:
        _redis = redis_asyncio.from_url(_settings.redis_url, decode_responses=True)
    return _redis


def _key(token: str) -> str:
    return f"mnemos:session:{token}"


async def create_session(user_id: uuid.UUID) -> str:
    token = secrets.token_urlsafe(32)
    payload = f"{user_id}|{datetime.now(tz=timezone.utc).isoformat()}"
    await _client().set(_key(token), payload, ex=_settings.session_max_age_sec)
    return token


async def read_session(token: str) -> uuid.UUID | None:
    raw = await _client().get(_key(token))
    if not raw:
        return None
    user_id_str, _ = raw.split("|", 1)
    try:
        return uuid.UUID(user_id_str)
    except ValueError:
        return None


async def delete_session(token: str) -> None:
    await _client().delete(_key(token))
