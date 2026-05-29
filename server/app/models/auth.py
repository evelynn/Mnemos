import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="admin")
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        # PR-130 — SET NULL: org deletion shouldn't cascade-delete users;
        # they revert to org-less and an admin re-assigns.
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # PR-38 — profile + lifecycle columns added when the platform
    # graduated from single-operator self-host to a team-operated
    # product. ``email`` doubles as the (optional) channel for invite /
    # password-reset flows; ``display_name`` is what the dashboard
    # shows next to comments and audit-log entries; ``avatar_url`` is
    # cosmetic and may be empty; ``disabled_at`` is the soft-delete
    # flag the login + session paths refuse on.
    email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timezone: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, default=None
    )
    disabled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        # PR-130 — SET NULL: a deleted user's secrets become orphan
        # (admin-owned), not auto-erased — better aligns with audit
        # retention.
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[Optional["User"]] = relationship(back_populates="api_keys")


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Secret(Base):
    __tablename__ = "secrets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    label: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(nullable=False)
    iv: Mapped[bytes] = mapped_column(nullable=False)
    # Org scoping (PR-88). Without it ``GET /secrets`` returned every
    # tenant's ciphertext metadata to any logged-in user (§2.8). Nullable
    # so the alembic backfill can run online; subsequent inserts MUST set it.
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_test_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
