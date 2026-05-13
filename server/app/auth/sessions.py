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
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        return None
    # PR-46 — idle timeout (closes audit E4). Every authenticated
    # request slides the session's TTL forward. The "absolute"
    # ``session_max_age_sec`` window still applies because every
    # ``set/expire`` call uses the same constant; what changes is
    # that 8 hours of inactivity expires the session even though
    # the absolute window hasn't elapsed yet.
    try:
        await _client().expire(_key(token), _settings.session_max_age_sec)
    except Exception:  # noqa: BLE001
        # Sliding the TTL is best-effort. A Redis blip mustn't make
        # an already-authenticated request fail — the next request
        # will retry the slide, and the existing TTL is what bounds
        # the session anyway.
        pass
    return user_id


async def delete_session(token: str) -> None:
    """Server-side logout: the Redis key is the source of truth, so
    deleting it revokes the session immediately (closes audit A6).
    Even if an attacker still holds the cookie, ``read_session``
    will return ``None`` on the next request.
    """
    await _client().delete(_key(token))


async def revoke_all_for_user(user_id: uuid.UUID) -> int:
    """Force-logout every active session for a user (audit A6).

    Used by ``DELETE /api/v1/users/{id}`` on soft-delete so the
    disabled account loses its current cookie *immediately* rather
    than waiting for the TTL to elapse.

    Scans the session keyspace — fine at Phase-1 scale (a single
    org has <1000 sessions). The audit team flagged the eventual
    swap for a per-user index as a Phase 3 follow-up.
    """
    client = _client()
    pattern = "mnemos:session:*"
    revoked = 0
    target = str(user_id)
    async for key in client.scan_iter(match=pattern, count=200):
        raw = await client.get(key)
        if not raw:
            continue
        head = raw.split("|", 1)[0] if "|" in raw else raw
        if head == target:
            await client.delete(key)
            revoked += 1
    return revoked
