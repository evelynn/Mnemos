import logging
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import delete_session, read_session
from app.config import get_settings
from app.db import get_session
from app.models.auth import User

_settings = get_settings()
log = logging.getLogger(__name__)


async def resolve_active_session_user(
    session_token: str | None,
    db: AsyncSession,
) -> User | None:
    """Resolve a cookie to an active user, failing closed on soft deletion.

    This is the single cookie-to-user boundary shared by API dependencies and
    dashboard page routing.  A disabled or deleted user invalidates the Redis
    session immediately, so an already-issued cookie cannot outlive the
    account's database state.
    """
    if not session_token:
        return None
    user_id = await read_session(session_token)
    if user_id is None:
        return None
    user = (
        await db.execute(
            select(User)
            .where(
                User.id == user_id,
                User.disabled_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        # Authentication has already failed. Redis cleanup is best-effort and
        # must never turn that safe 401/redirect into a server error.
        try:
            await delete_session(session_token)
        except Exception:  # noqa: BLE001
            log.warning(
                "failed to revoke a rejected session "
                "failure_code=session_cleanup_failed"
            )
        return None
    return user


async def current_user(
    session_token: Annotated[str | None, Cookie(alias=_settings.session_cookie_name)] = None,
    db: AsyncSession = Depends(get_session),
) -> User:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
    user = await resolve_active_session_user(session_token, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_session")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
