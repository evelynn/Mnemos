"""Runtime observation model (OTLP Tier 2 — P2-1 / P2-2).

Each row captures a ``(service, operation, kind)`` triple the OTLP
receiver has seen, scoped to an organisation. When the merge stage
runs it joins these observations against the static graph and
upserts the matching ``Edge.data`` with ``exercised: true``. Rows
that don't match a known edge stay buffered for the next run.

Bounded by a UNIQUE constraint on ``(organization_id, service,
operation, kind)`` so the receiver can use a Postgres
``ON CONFLICT DO UPDATE`` to bump ``seen_count`` + ``last_seen_at``
without a SELECT/INSERT race.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RuntimeObservation(Base):
    __tablename__ = "runtime_observations"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "service",
            "operation",
            "kind",
            name="uq_runtime_obs_org_svc_op_kind",
        ),
        # Durable edge-runtime cursors scope an observation to the project
        # whose graph consumed it.  PostgreSQL requires the exact referenced
        # column set to be unique for that composite FK; ``id`` remains the PK.
        UniqueConstraint(
            "id",
            "project_id",
            name="uq_runtime_obs_id_project",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    service: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="CALLS")
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
