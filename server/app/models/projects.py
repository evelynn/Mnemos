import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    gitlab_project_id: Mapped[int] = mapped_column(Integer, nullable=False)
    gitlab_url: Mapped[str] = mapped_column(String, nullable=False)
    default_branch: Mapped[str] = mapped_column(String, nullable=False, default="main")
    languages: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        # PR-130 — SET NULL: org deletion shouldn't auto-delete
        # projects; admin re-assigns or explicitly deletes them.
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
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


class ProjectDB(Base):
    """Per-project database binding with safety policy (spec §12.2)."""

    __tablename__ = "project_dbs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    component_id: Mapped[str] = mapped_column(String, nullable=False)
    secret_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        # PR-130 — SET NULL: secret deletion shouldn't cascade-delete
        # ProjectDB bindings (they survive as misconfigured rows that
        # the operator can rebind).
        UUID(as_uuid=True),
        ForeignKey("secrets.id", ondelete="SET NULL"),
        nullable=True,
    )
    allow_awr: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    sensitive_tables: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    masking_rules: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    maintenance_windows: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    last_probe_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_probe_result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    disabled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
