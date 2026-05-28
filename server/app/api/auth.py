from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth import brute_force
from app.auth.passwords import verify_password
from app.auth.sessions import create_session, delete_session, read_session
from app.config import get_settings
from app.db import get_session
from app.models.auth import User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_settings = get_settings()


class LoginRequest(BaseModel):
    # PR-133 — strict bounds. Pre-PR: unbounded str → 10 MB password
    # would force bcrypt through its full work-factor on every byte
    # before truncating, burning CPU per request. bcrypt itself
    # truncates at 72 bytes, so capping the input early matches the
    # algorithm's reality.
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=512)


class LoginResponse(BaseModel):
    """PR-132 — distinct schema from users.UserOut to avoid an
    OpenAPI components/schemas name collision (which forced the
    ugly ``app__api__auth__UserOut`` qualifier). Login only needs
    the three fields the GUI uses to render the sidebar avatar
    immediately; the full profile is one ``/api/v1/auth/me`` call
    away."""
    id: str
    username: str
    role: str


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> LoginResponse:
    # PR-39 — brute-force lockout. The username field is checked
    # *before* any password verify so a locked-out attacker can't
    # use the timing-side-channel of valid-vs-invalid password
    # hashing to enumerate users.
    if await brute_force.is_locked(body.username):
        await audit_record(
            actor="anonymous",
            action="auth.login_blocked_lockout",
            target=body.username,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="account_temporarily_locked",
        )

    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        await brute_force.record_failure(body.username)
        await audit_record(
            actor=f"user:{user.id}" if user else "anonymous",
            action="auth.login_failed",
            target=body.username,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        )
    # PR-38: a soft-deleted account (disabled_at set) is treated as
    # if it never existed. The audit log records the attempt with
    # the same "login_failed" action so an admin reviewing the log
    # can see ex-employees still poking at the door — but the
    # response is identical to a wrong password to avoid leaking
    # which accounts are disabled vs deleted.
    if user.disabled_at is not None:
        await audit_record(
            actor=f"user:{user.id}",
            action="auth.login_blocked_disabled",
            target=body.username,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        )

    # Bookkeeping for the admin "active sessions" view + audit trail.
    from datetime import datetime, timezone as _tz

    user.last_login_at = datetime.now(tz=_tz.utc)
    await db.commit()

    # Clear the brute-force counter — a successful login means the
    # earlier failures were almost certainly the same operator
    # mistyping, not an attack.
    await brute_force.clear(body.username)

    token = await create_session(user.id)
    await audit_record(actor=f"user:{user.id}", action="auth.login")
    response.set_cookie(
        key=_settings.session_cookie_name,
        value=token,
        max_age=_settings.session_max_age_sec,
        httponly=True,
        samesite="lax",
        secure=_settings.session_cookie_secure,
        path="/",
    )
    return LoginResponse(id=str(user.id), username=user.username, role=user.role)


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


# PR-132 — ``GET /api/v1/auth/me`` lives in api/users.py.
# It returns the full profile (_out helper, PR-38 lifecycle fields).
# The shorter duplicate here was confusing the OpenAPI generator
# (same operation_id → SDK clients couldn't disambiguate).
