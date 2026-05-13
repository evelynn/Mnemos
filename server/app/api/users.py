"""User management endpoints (PR-38).

Closes the 13th-round audit's A1 / A4 / A5 / A7 Critical findings:

* **A1** — create a user from the GUI (admin only). The CLI
  ``create-user`` path stays as the bootstrap option, but day-2 team
  operations should never need shell access.
* **A4** — every authenticated user can read / update their own
  profile (display name, email, avatar URL, timezone). Username and
  role cannot be self-edited.
* **A5** — admin role-change endpoint. PATCH-only so a misclicked
  Delete doesn't accidentally promote someone to admin.
* **A7** — admin soft-delete (``disabled_at``). The login handler
  refuses any disabled account; the session table is *not* purged
  from here because the cron sweep handles that.

Multi-tenant note: users are scoped by ``organization_id``; the
listing endpoint only returns users in the caller's org. A future
P3 follow-up will add multi-org membership.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.passwords import hash_password
from app.auth.rbac import require_admin
from app.db import get_session
from app.models.auth import User

router = APIRouter(tags=["users"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


VALID_ROLES = {"viewer", "operator", "admin"}
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{2,64}$")


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    role: str
    organization_id: Optional[uuid.UUID]
    email: Optional[str]
    display_name: Optional[str]
    avatar_url: Optional[str]
    timezone: Optional[str]
    disabled_at: Optional[datetime]
    last_login_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=12, max_length=512)
    role: str = "viewer"
    email: Optional[str] = Field(default=None, max_length=200)
    display_name: Optional[str] = Field(default=None, max_length=200)

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str) -> str:
        if not _USERNAME_RE.fullmatch(v):
            raise ValueError(
                "username must be 2-64 chars, alphanumerics + _.- only"
            )
        return v

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        return v


class UserPatch(BaseModel):
    """Self-service profile patch — never touches username or role."""

    email: Optional[str] = Field(default=None, max_length=200)
    display_name: Optional[str] = Field(default=None, max_length=200)
    avatar_url: Optional[str] = Field(default=None, max_length=500)
    timezone: Optional[str] = Field(default=None, max_length=64)


class RoleChange(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _check(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        return v


class PasswordChange(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=12, max_length=512)


def _out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        username=u.username,
        role=u.role,
        organization_id=u.organization_id,
        email=u.email,
        display_name=u.display_name,
        avatar_url=u.avatar_url,
        timezone=u.timezone,
        disabled_at=u.disabled_at,
        last_login_at=u.last_login_at,
        created_at=u.created_at,
        updated_at=u.updated_at,
    )


# ---------------------------------------------------------------------------
# Self-service: current user profile
# ---------------------------------------------------------------------------


@router.get("/api/v1/auth/me")
async def me(user: CurrentUser) -> UserOut:
    """Return the calling user's profile.

    The login flow stores nothing more than the session cookie; the
    GUI calls this to populate the sidebar avatar / display name and
    the Settings → Account tab.
    """
    return _out(user)


@router.patch("/api/v1/auth/me")
async def update_me(
    body: UserPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> UserOut:
    """Self-service profile update.

    Username and role are immutable from this endpoint — those are
    admin-only actions. The audit log records the diff so a future
    incident reviewer can see who changed what about themselves.
    """
    before = {
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "timezone": user.timezone,
    }
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    await audit_record(
        actor=f"user:{user.id}",
        action="user.profile_updated",
        target=f"user:{user.id}",
        details={"before": before, "after": body.model_dump(exclude_unset=True)},
    )
    return _out(user)


@router.post("/api/v1/auth/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    body: PasswordChange,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> None:
    """Self-service password change.

    Requires the current password (defence against session hijack —
    even a stolen cookie can't rotate the password). The 12-char
    minimum is enforced by the Pydantic schema.
    """
    from app.auth.passwords import verify_password

    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="current_password_incorrect",
        )
    user.password_hash = hash_password(body.new_password)
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="user.password_changed_self",
        target=f"user:{user.id}",
        details={},
    )


# ---------------------------------------------------------------------------
# Admin: list / create / role-change / soft-delete
# ---------------------------------------------------------------------------


@router.get("/api/v1/users")
async def list_users(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    include_disabled: bool = False,
) -> list[UserOut]:
    """List users in the caller's organisation.

    Disabled users are hidden by default — the team-management view
    only shows them when the admin explicitly asks for the audit
    history.
    """
    stmt = select(User).where(User.organization_id == user.organization_id)
    if not include_disabled:
        stmt = stmt.where(User.disabled_at.is_(None))
    stmt = stmt.order_by(User.created_at.asc())
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(r) for r in rows]


@router.post("/api/v1/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    actor: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> UserOut:
    """Admin creates a new account in the caller's organisation.

    The new user lands in the *creator's* org — there is no
    cross-tenant create from this endpoint. Email uniqueness is
    enforced by the DB index; username uniqueness by the column.
    """
    # Pre-flight uniqueness checks for clearer error messages.
    if body.email is not None:
        existing_email = (
            await db.execute(select(User).where(User.email == body.email))
        ).scalar_one_or_none()
        if existing_email is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="email_taken"
            )
    existing_username = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if existing_username is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="username_taken"
        )

    u = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        organization_id=actor.organization_id,
        email=body.email,
        display_name=body.display_name,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    await audit_record(
        actor=f"user:{actor.id}",
        action="user.created",
        target=f"user:{u.id}",
        details={"username": body.username, "role": body.role},
    )
    return _out(u)


@router.patch("/api/v1/users/{user_id}/role")
async def change_role(
    user_id: uuid.UUID,
    body: RoleChange,
    actor: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> UserOut:
    """Promote or demote a user in the caller's organisation.

    Two guards:

    * The target user must be in the *caller's* org. Cross-org
      promotion would let a tenant admin attack a different
      tenant's user.
    * An admin cannot demote themselves — otherwise the org could
      end up with zero admins (recoverable only via CLI).
    """
    target = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if target is None or target.organization_id != actor.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if target.id == actor.id and body.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot_demote_self",
        )
    before_role = target.role
    target.role = body.role
    await db.commit()
    await db.refresh(target)
    await audit_record(
        actor=f"user:{actor.id}",
        action="user.role_changed",
        target=f"user:{target.id}",
        details={"from": before_role, "to": body.role},
    )
    return _out(target)


@router.delete("/api/v1/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_user(
    user_id: uuid.UUID,
    actor: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Soft-delete: set ``disabled_at`` so the login flow refuses
    the account but the audit history is preserved.

    Same org-isolation + self-disable guard as ``change_role``.
    """
    target = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if target is None or target.organization_id != actor.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if target.id == actor.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot_disable_self",
        )
    if target.disabled_at is None:
        target.disabled_at = datetime.now(tz=timezone.utc)
        await db.commit()
        await audit_record(
            actor=f"user:{actor.id}",
            action="user.disabled",
            target=f"user:{target.id}",
            details={"username": target.username},
        )


@router.post("/api/v1/users/{user_id}/enable")
async def enable_user(
    user_id: uuid.UUID,
    actor: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> UserOut:
    """Re-enable a soft-deleted user. Useful when an account was
    disabled on suspicion and the suspicion turned out to be a
    false alarm; cheaper than reissuing credentials."""
    target = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if target is None or target.organization_id != actor.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if target.disabled_at is not None:
        target.disabled_at = None
        await db.commit()
        await db.refresh(target)
        await audit_record(
            actor=f"user:{actor.id}",
            action="user.enabled",
            target=f"user:{target.id}",
            details={"username": target.username},
        )
    return _out(target)
