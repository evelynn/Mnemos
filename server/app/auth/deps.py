from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import read_session
from app.config import get_settings
from app.db import get_session
from app.models.auth import User

_settings = get_settings()


async def current_user(
    session_token: Annotated[str | None, Cookie(alias=_settings.session_cookie_name)] = None,
    db: AsyncSession = Depends(get_session),
) -> User:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
    user_id = await read_session(session_token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_session")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user_not_found")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
