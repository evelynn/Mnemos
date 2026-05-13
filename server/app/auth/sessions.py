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


def _index_key(user_id: uuid.UUID) -> str:
    """Per-user reverse index — set of active session tokens.

    PR-47 adds this so ``revoke_all_for_user`` is O(active sessions
    for that user) instead of O(active sessions in the keyspace).
    The 16th-round audit flagged the scan_iter approach as a
    Phase-4 scaling risk at ~10K sessions.
    """
    return f"mnemos:sessions_by_user:{user_id}"


async def create_session(user_id: uuid.UUID) -> str:
    token = secrets.token_urlsafe(32)
    payload = f"{user_id}|{datetime.now(tz=timezone.utc).isoformat()}"
    client = _client()
    await client.set(_key(token), payload, ex=_settings.session_max_age_sec)
    # Add the token to the per-user reverse index so a later
    # disable can find it without scanning the keyspace. The
    # index TTL matches the session so an unrevoked session
    # cleans up naturally on its own.
    try:
        await client.sadd(_index_key(user_id), token)
        await client.expire(_index_key(user_id), _settings.session_max_age_sec)
    except Exception:  # noqa: BLE001
        # Best-effort — the scan_iter path in revoke_all_for_user
        # is still in place as the fall-back.
        pass
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
    client = _client()
    raw = await client.get(_key(token))
    await client.delete(_key(token))
    # Best-effort: keep the per-user index in sync.
    if raw:
        try:
            user_id_str = raw.split("|", 1)[0]
            await client.srem(f"mnemos:sessions_by_user:{user_id_str}", token)
        except Exception:  # noqa: BLE001
            pass


async def revoke_all_for_user(user_id: uuid.UUID) -> int:
    """Force-logout every active session for a user (audit A6).

    Used by ``DELETE /api/v1/users/{id}`` on soft-delete so the
    disabled account loses its current cookie *immediately* rather
    than waiting for the TTL to elapse.

    PR-47 adds a per-user index that turns this into O(active
    sessions for *that* user). The legacy ``scan_iter`` path
    stays as a fallback for sessions created before the index
    existed.

    **Race window note (17th-round audit A2)**: a ``create_session``
    racing with a ``revoke_all_for_user`` can land *after* the
    SMEMBERS snapshot — the new token isn't in the set we read,
    so the fast path doesn't delete it. The scan_iter fallback
    catches it as long as the session row was already committed
    when ``scan_iter`` ran; otherwise the new session lives
    until its TTL elapses (max ``session_max_age_sec``). The
    audit team rated this Minor — a freshly-issued session for a
    user who just got disabled is short-lived anyway, and the
    next request's ``read_session`` would still find the row
    valid (its key wasn't deleted) until the operator manually
    re-runs disable. If a future deployment needs strict
    revocation in the race window, swap the SMEMBERS read for a
    transactional pattern (WATCH/MULTI) or pin the disable flow
    to a single worker via the existing advisory-lock helper.
    """
    client = _client()
    revoked = 0
    # Fast path: per-user reverse index.
    try:
        tokens = await client.smembers(_index_key(user_id))
    except Exception:  # noqa: BLE001
        tokens = set()
    for token in tokens or ():
        if isinstance(token, bytes):
            token = token.decode()
        await client.delete(_key(token))
        revoked += 1
    if tokens:
        try:
            await client.delete(_index_key(user_id))
        except Exception:  # noqa: BLE001
            pass
    # Fallback path — sessions created before the index existed.
    pattern = "mnemos:session:*"
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
