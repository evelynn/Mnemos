"""PR-138h — code-wide audit for tz-naive comparison bugs.

PR-138d fixed three tz-naive datetime comparison crashes in
``app/api/findings.py`` (summary, trend, roi). This PR sweeps the
rest of the codebase for the same pattern and locks the fix:

- ``UserInvite.expires_at < now`` in onboarding.py (invite accept)
- password-reset expiry now runs inside a DB predicate, avoiding a Python
  naive/aware comparison entirely

The bug shape is universal: the model declares
``DateTime(timezone=True)`` (PG returns aware), but SQLite has no
native tz so reads come back naive. Comparing naive to aware
``datetime.now(tz=timezone.utc)`` is a TypeError in Python.

Real-execution tests
--------------------
Each test seeds a row, then drives the production code path that
compares to ``now`` and asserts the expected behaviour (not the
crash).
"""

from __future__ import annotations

import secrets
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.models.base import Base  # noqa: E402
from app.models import auth as _auth  # noqa: E402,F401
from app.models import audit as _audit  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.auth.passwords import hash_password  # noqa: E402
from app.models.auth import User  # noqa: E402
from app.models.onboarding import PasswordResetToken, UserInvite  # noqa: E402


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(
            lambda connection: Base.metadata.create_all(
                connection,
                tables=[
                    _org.Organization.__table__,
                    User.__table__,
                    UserInvite.__table__,
                    PasswordResetToken.__table__,
                ],
            )
        )
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await eng.dispose()


# ─── onboarding.py invite accept ──────────────────────────────────


def _hash_token(raw: str) -> str:
    """Match the storage format the production accept_invite uses."""
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_onboarding_module_coerces_naive_expires_before_compare():
    """Source-level guard: the patched code path must coerce
    ``expires_at`` to UTC-aware before comparing to ``now`` — exact
    fix pattern PR-138h applied. (Real-execution happy path requires
    rebinding ``app.db.SessionLocal`` because ``audit_record`` opens
    its own session; covered indirectly via the rejection path
    test below, which raises before audit_record is reached.)
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "api" / "onboarding.py"
    ).read_text(encoding="utf-8")
    # Invite acceptance still performs a Python comparison and must coerce the
    # SQLite-naive value. Password-reset consumption compares in SQL instead.
    for marker in ("accept_invite",):
        idx = src.find("async def " + marker)
        assert idx >= 0, f"{marker} handler missing"
        body = src[idx:idx + 2500]
        assert "tzinfo is None" in body or "tzinfo=timezone.utc" in body, (
            f"{marker} no longer coerces naive expires_at"
        )
    reset_idx = src.find("async def consume_password_reset")
    reset_body = src[reset_idx:reset_idx + 5000]
    assert "PasswordResetToken.expires_at > now" in reset_body
    assert ".returning(PasswordResetToken.user_id)" in reset_body


@pytest.mark.asyncio
async def test_invite_with_past_expiry_is_rejected_cleanly(session):
    """The expired path must raise HTTP 400 — NOT TypeError. Pre-fix
    SQLite would crash before this branch even ran."""
    raw = "tok-" + secrets.token_urlsafe(16)
    inv = UserInvite(
        id=_uuid.uuid4(),
        token_hash=_hash_token(raw),
        email="x@y.test",
        role="operator",
        expires_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        consumed_at=None,
    )
    session.add(inv)
    await session.commit()

    from fastapi import HTTPException

    from app.api.onboarding import InviteAccept, accept_invite
    body = InviteAccept(
        token=raw, username="newop2",
        password="Demo-A8secure7",
    )
    with pytest.raises(HTTPException) as exc:
        await accept_invite(body=body, db=session)
    assert exc.value.status_code == 400


# ─── onboarding.py password reset ─────────────────────────────────


# Happy-path acceptance is covered by ``test_pr138d_http_e2e_pack``
# which uses a fully wired ASGI session (audit_record + everything).
# Here we only test the expired-path comparison: it raises BEFORE
# audit_record, so it's safe to drive with an in-memory SQLite
# session without rebinding SessionLocal.


@pytest.mark.asyncio
async def test_password_reset_with_past_expiry_returns_400(session):
    user = User(
        id=_uuid.uuid4(),
        username="resetuser2",
        password_hash=hash_password("Initial-Pw-12345"),
        role="operator",
    )
    session.add(user)
    raw = "rst-" + secrets.token_urlsafe(16)
    rt = PasswordResetToken(
        id=_uuid.uuid4(),
        token_hash=_hash_token(raw),
        user_id=user.id,
        expires_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        consumed_at=None,
    )
    session.add(rt)
    await session.commit()

    from fastapi import HTTPException

    from app.api.onboarding import PasswordResetConsume, consume_password_reset
    body = PasswordResetConsume(token=raw, new_password="Reset-A8secure7")
    with pytest.raises(HTTPException) as exc:
        await consume_password_reset(body=body, db=session)
    assert exc.value.status_code == 400


# ─── forward-guard: source patterns that hint at unfixed
#                    naive-vs-aware comparisons. ─────────────────


def test_no_unguarded_expires_at_lt_now_in_api():
    """A new ``foo.expires_at < now`` written without the
    ``replace(tzinfo=timezone.utc)`` guard re-introduces the bug.
    This grep is intentionally narrow (looks for the literal
    pattern) so legitimate datetime arithmetic that's safe (e.g.
    DB-side ``expires_at <= now()`` in raw SQL) isn't flagged.
    """
    import re
    from pathlib import Path

    api_dir = (
        Path(__file__).resolve().parents[1] / "app" / "api"
    )
    pattern = re.compile(
        r"\.expires_at\s*<\s*now\b|\.expires_at\s*>\s*now\b"
    )
    failures: list[str] = []
    for f in api_dir.rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        for m in pattern.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            expression_start = src.rfind("\n", 0, m.start()) + 1
            expression = src[expression_start:m.end()]
            # ORM class attributes are compiled into a DB-side comparison;
            # Python never compares the datetime values.
            if "PasswordResetToken.expires_at" in expression:
                continue
            window_start = max(0, m.start() - 300)
            window = src[window_start:m.start()]
            # The fix pattern: a recent ``.replace(tzinfo=timezone.utc)``
            # within ~300 chars before the comparison.
            if "tzinfo" not in window:
                failures.append(f"{f.relative_to(api_dir)}:{line}")
    assert not failures, (
        "unguarded ``.expires_at < now`` comparison(s) — "
        "coerce naive→UTC first:\n  " + "\n  ".join(failures)
    )
