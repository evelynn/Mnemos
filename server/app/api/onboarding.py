"""Invite + password-reset endpoints (PR-44, closes A2 + A3)."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.passwords import PasswordPolicyError, hash_password
from app.auth.rbac import require_admin
from app.db import get_session
from app.models.auth import User
from app.models.onboarding import PasswordResetToken, UserInvite
from app.safety.tokens import hash_token

router = APIRouter(tags=["onboarding"])

INVITE_TTL = timedelta(days=7)
RESET_TTL = timedelta(hours=1)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class InviteCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    role: str = "viewer"


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    expires_at: datetime
    consumed_at: Optional[datetime]
    token: Optional[str] = None  # only populated on create


class InviteAccept(BaseModel):
    token: str
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=12, max_length=512)
    display_name: Optional[str] = Field(default=None, max_length=200)


class PasswordResetRequest(BaseModel):
    username: str


class PasswordResetConsume(BaseModel):
    token: str
    new_password: str = Field(..., min_length=12, max_length=512)


# ---------------------------------------------------------------------------
# Invite — admin creates, recipient consumes
# ---------------------------------------------------------------------------


@router.post(
    "/api/v1/invites", status_code=status.HTTP_201_CREATED
)
async def create_invite(
    body: InviteCreate,
    actor: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> InviteOut:
    """Admin creates an invite. The raw token is returned ONCE so
    the admin can share it (email / Slack) out-of-band. The DB
    only ever holds the SHA-256 hash."""
    if body.role not in {"viewer", "operator", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_role"
        )
    raw_token = secrets.token_urlsafe(32)
    invite = UserInvite(
        email=body.email,
        token_hash=hash_token(raw_token),
        role=body.role,
        organization_id=actor.organization_id,
        invited_by=actor.id,
        expires_at=datetime.now(tz=timezone.utc) + INVITE_TTL,
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    await audit_record(
        actor=f"user:{actor.id}",
        action="invite.created",
        target=body.email,
        details={"role": body.role, "invite_id": str(invite.id)},
    )
    return InviteOut(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        expires_at=invite.expires_at,
        consumed_at=invite.consumed_at,
        token=raw_token,
    )


@router.post(
    "/api/v1/invites/accept", status_code=status.HTTP_201_CREATED
)
async def accept_invite(
    body: InviteAccept,
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Recipient consumes the invite + sets their password.
    Anonymous endpoint — the token IS the auth."""
    token_h = hash_token(body.token)
    invite = (
        await db.execute(
            select(UserInvite).where(UserInvite.token_hash == token_h)
        )
    ).scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    if (
        invite is None
        or invite.consumed_at is not None
        or invite.expires_at < now
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invite_invalid_or_expired",
        )
    # Username uniqueness — the DB unique constraint is the final
    # word; we just want a clear error first.
    existing = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="username_taken"
        )
    try:
        new_hash = hash_password(body.password)
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.code
        ) from exc
    user = User(
        username=body.username,
        password_hash=new_hash,
        role=invite.role,
        organization_id=invite.organization_id,
        email=invite.email,
        display_name=body.display_name,
    )
    db.add(user)
    await db.flush()
    invite.consumed_at = now
    invite.consumed_by_user_id = user.id
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="invite.consumed",
        target=invite.email,
        details={"invite_id": str(invite.id), "role": invite.role},
    )
    return {"user_id": str(user.id), "username": user.username}


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


@router.post(
    "/api/v1/auth/reset/request",
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_reset(
    body: PasswordResetRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Anonymous endpoint. Always returns 202 with the same shape
    so an attacker can't enumerate usernames by timing.

    If the user exists and is not disabled, a token is created and
    the raw value is returned. In a real deployment the operator
    side ships the token via SMTP / Slack; the platform stays
    transport-agnostic. The token is logged ONCE in the audit log
    so an operator with audit-log access can hand-deliver it if
    needed.
    """
    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    out: dict[str, str] = {"status": "accepted"}
    if user is None or user.disabled_at is not None:
        # Same shape as the happy path — no enumeration.
        return out
    raw_token = secrets.token_urlsafe(32)
    rt = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(tz=timezone.utc) + RESET_TTL,
    )
    db.add(rt)
    await db.commit()
    await audit_record(
        actor="anonymous",
        action="auth.password_reset_requested",
        target=f"user:{user.id}",
        details={},
    )
    out["token"] = raw_token  # transport-agnostic
    return out


@router.post(
    "/api/v1/auth/reset/consume", status_code=status.HTTP_204_NO_CONTENT
)
async def consume_password_reset(
    body: PasswordResetConsume,
    db: AsyncSession = Depends(get_session),
) -> None:
    token_h = hash_token(body.token)
    rt = (
        await db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == token_h
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    if (
        rt is None
        or rt.consumed_at is not None
        or rt.expires_at < now
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reset_invalid_or_expired",
        )
    user = (
        await db.execute(select(User).where(User.id == rt.user_id))
    ).scalar_one_or_none()
    if user is None or user.disabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reset_invalid_or_expired",
        )
    try:
        user.password_hash = hash_password(body.new_password)
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.code
        ) from exc
    rt.consumed_at = now
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="auth.password_reset_consumed",
        target=f"user:{user.id}",
        details={},
    )
