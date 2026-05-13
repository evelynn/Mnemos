"""Brute-force lockout for the login flow (PR-39).

Closes the 13th-round audit's Critical E3: the previous login handler
counted nothing, so a script could try arbitrary passwords against
known usernames forever.

Mechanism — Redis ``INCR`` against a per-username key with an
expiring window:

* Key:  ``mnemos:login_fail:{username_lower}``
* Limit: 5 consecutive failures inside a 15-minute window.
* Outcome: the 6th attempt returns 429 (Too Many Attempts), bumps
  the lockout counter for another 15 minutes, and audit-logs
  ``auth.login_blocked_lockout`` so an operator can see the
  attack pattern.

Successful login clears the counter so a user who mistyped their
password a few times before getting it right doesn't end up locked
out at the next genuine login attempt.

The key intentionally uses the *username string the caller typed*,
not the resolved user id — that way a script trying random
usernames doesn't all collide on a single key (which would let
real users in by riding the same lockout).
"""

from __future__ import annotations

from app.orchestrator.redis_pool import get_redis

_PREFIX = "mnemos:login_fail:"

# Tunables. The 15-minute window matches the break-glass TTL and
# the SSE-strip stale window, so an operator never has to remember
# more than one "short-lockout" interval.
MAX_FAILURES_PER_WINDOW = 5
LOCKOUT_WINDOW_SEC = 15 * 60


def _key(username: str) -> str:
    return _PREFIX + (username or "").strip().lower()


async def is_locked(username: str) -> bool:
    """Return True iff the username has hit the failure ceiling."""
    redis = await get_redis()
    raw = await redis.get(_key(username))
    if raw is None:
        return False
    try:
        return int(raw) >= MAX_FAILURES_PER_WINDOW
    except (TypeError, ValueError):
        return False


async def record_failure(username: str) -> int:
    """Increment the failure counter, set / refresh the TTL, and
    return the new count. Callers use the return value to decide
    whether to log a regular ``login_failed`` or the heavier
    ``login_blocked_lockout`` audit event."""
    redis = await get_redis()
    key = _key(username)
    count = await redis.incr(key)
    if count == 1:
        # First failure starts the window; subsequent INCRs don't
        # reset the TTL so the operator can't infinitely refresh
        # the lockout by guessing one password every 14 minutes.
        await redis.expire(key, LOCKOUT_WINDOW_SEC)
    return int(count)


async def clear(username: str) -> None:
    """Successful login wipes the slate."""
    redis = await get_redis()
    await redis.delete(_key(username))
