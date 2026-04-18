"""Organization model (Phase C multi-tenancy foundation).

Single-tenant deployments keep working because existing rows are
backfilled into a ``default`` organization and all FKs stay nullable
during the transition. Retrofit of every endpoint to enforce
``organization_id`` in queries is tracked under issue/TODO in
api/organizations.py — this module provides the data shape and helpers.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
