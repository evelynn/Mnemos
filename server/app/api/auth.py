from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.passwords import verify_password
from app.auth.sessions import create_session, delete_session, read_session
from app.config import get_settings
from app.db import get_session
from app.models.auth import User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_settings = get_settings()


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: str
    username: str
    role: str


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> UserOut:
    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        await audit_record(
            actor=f"user:{user.id}" if user else "anonymous",
            action="auth.login_failed",
            target=body.username,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        )

    token = await create_session(user.id)
    await audit_record(actor=f"user:{user.id}", action="auth.login")
    response.set_cookie(
        key=_settings.session_cookie_name,
        value=token,
        max_age=_settings.session_max_age_sec,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return UserOut(id=str(user.id), username=user.username, role=user.role)


@router.post("/logout")
async def logout(
    response: Response,
    session_token: Annotated[
        str | None, Cookie(alias=_settings.session_cookie_name)
    ] = None,
) -> dict[str, str]:
    if session_token:
        user_id = await read_session(session_token)
        await delete_session(session_token)
        if user_id is not None:
            await audit_record(actor=f"user:{user_id}", action="auth.logout")
    response.delete_cookie(_settings.session_cookie_name, path="/")
    return {"status": "logged_out"}


@router.get("/me")
async def me(user: CurrentUser) -> UserOut:
    return UserOut(id=str(user.id), username=user.username, role=user.role)
