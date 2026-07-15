"""Invite + password-reset endpoints (PR-44, closes A2 + A3)."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.passwords import PasswordPolicyError, hash_password
from app.auth.rbac import require_admin
from app.auth.sessions import revoke_all_for_user
from app.db import get_session
from app.models.auth import ApiKey, User
from app.models.onboarding import PasswordResetToken, UserInvite
from app.models.organization import Organization
from app.safety.tokens import hash_token

router = APIRouter(tags=["onboarding"])

INVITE_TTL = timedelta(days=7)
RESET_REQUEST_RESPONSE = {"status": "accepted"}
RESET_INVALID_DETAIL = "reset_invalid_or_expired"


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
    username: str = Field(..., min_length=1, max_length=64)


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
    if actor.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization_required",
        )
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
            select(UserInvite)
            .join(
                Organization,
                Organization.id == UserInvite.organization_id,
            )
            .where(
                UserInvite.token_hash == token_h,
                UserInvite.organization_id.is_not(None),
            )
            .with_for_update(of=UserInvite)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    # PR-138h — SQLite (polyglot) returns naive datetimes; Postgres
    # returns aware. Coerce before comparing to ``now`` so the local
    # mode + tests don't TypeError on the comparison.
    expires_at = invite.expires_at if invite else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        invite is None
        or invite.organization_id is None
        or invite.consumed_at is not None
        or expires_at < now
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
) -> dict[str, str]:
    """Acknowledge every syntactically valid request identically.

    Mnemos currently has no configured, authenticated outbound reset-token
    transport.  Returning a token to this anonymous caller (or putting one in
    logs/audit data for manual hand-off) would make knowledge of a username
    sufficient to take over that account.  This endpoint therefore does not
    query ``users`` and does not mint a token.  A future delivery integration
    must authenticate the destination and keep the raw token entirely outside
    HTTP responses, application logs, and audit payloads before token creation
    can be enabled.
    """
    # Deliberately never inspect the username after schema validation.  This
    # keeps known, unknown, and disabled accounts on exactly the same path.
    _ = body
    await audit_record(
        actor="anonymous",
        action="auth.password_reset_requested",
        target=None,
        details={"delivery": "unavailable"},
    )
    return dict(RESET_REQUEST_RESPONSE)


async def _raise_invalid_reset(db: AsyncSession) -> None:
    """Rollback a tentative claim and expose one fixed invalid response."""

    await db.rollback()
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=RESET_INVALID_DETAIL,
    )


@router.post(
    "/api/v1/auth/reset/consume", status_code=status.HTTP_204_NO_CONTENT
)
async def consume_password_reset(
    body: PasswordResetConsume,
    db: AsyncSession = Depends(get_session),
) -> None:
    """Atomically consume a securely delivered reset credential.

    The anonymous request endpoint above never creates such a credential.  If
    a trusted internal delivery mechanism provisions one, consumption locks
    the target user *before* conditionally claiming the token.  That lock order
    serializes different reset tokens for the same account and avoids the
    token-A/user/token-B deadlock that claiming a token first would permit.
    PostgreSQL's conditional ``UPDATE .. RETURNING`` is the single-use
    linearization point; SQLite supports the statement for local mode while
    ignoring ``FOR UPDATE``.
    """
    token_h = hash_token(body.token)

    # This first read is only a candidate lookup.  It takes no lock and grants
    # no authority; every predicate is repeated after the user row is locked.
    now = datetime.now(tz=timezone.utc)
    candidate_user_id = (
        await db.execute(
            select(PasswordResetToken.user_id)
            .join(User, User.id == PasswordResetToken.user_id)
            .where(
                PasswordResetToken.token_hash == token_h,
                PasswordResetToken.consumed_at.is_(None),
                PasswordResetToken.expires_at > now,
                User.disabled_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if candidate_user_id is None:
        await _raise_invalid_reset(db)

    user = (
        await db.execute(
            select(User)
            .where(
                User.id == candidate_user_id,
                User.disabled_at.is_(None),
            )
            .with_for_update(of=User)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None or user.disabled_at is not None:
        await _raise_invalid_reset(db)

    claimed_user_id = (
        await db.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.token_hash == token_h,
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.consumed_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(PasswordResetToken.user_id)
        )
    ).scalar_one_or_none()
    if claimed_user_id != user.id:
        await _raise_invalid_reset(db)

    try:
        new_password_hash = hash_password(body.new_password)
    except PasswordPolicyError as exc:
        # Do not burn an otherwise valid one-time credential when only the new
        # password failed policy validation.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.code
        ) from exc

    user.password_hash = new_password_hash
    await db.flush()

    # A password reset is a credential-compromise boundary.  Revoke every
    # database-backed bearer credential owned by the user and every other
    # outstanding reset credential in the same transaction.
    await db.execute(
        update(ApiKey)
        .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.consumed_at.is_(None),
        )
        .values(
            consumed_at=func.coalesce(PasswordResetToken.consumed_at, now)
        )
    )

    # Redis sessions are external to the SQL transaction.  Revoke them before
    # commit: a Redis failure aborts the password change (fail closed), while a
    # later SQL failure can at worst force a harmless re-login.
    try:
        await revoke_all_for_user(user.id)
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        raise
    await audit_record(
        actor=f"user:{user.id}",
        action="auth.password_reset_consumed",
        target=f"user:{user.id}",
        details={},
    )
