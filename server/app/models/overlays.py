"""Durable non-source evidence layered over the bitemporal source graph.

Analyzer publication owns :class:`~app.models.graph.Node` and
:class:`~app.models.graph.Edge` versions.  Human judgements and runtime
observations have a different lifetime: they must survive a semantically
unchanged source refresh and must never be mistaken for analyzer output.

These tables are therefore keyed by the graph's *logical* identities rather
than a physical bitemporal row.  They intentionally do not reference
``nodes``/``edges``: a publication may close one physical version and create
the next while the overlay remains valid.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
# ``GraphEdgeRuntimeCursor`` has a string FK to runtime_observations.  Import
# the target model here because API modules may import overlays directly and
# later call ``Base.metadata.create_all`` without going through all_models.
from app.models.runtime import RuntimeObservation as _RuntimeObservation  # noqa: F401,E402


class _HumanOverlayColumns:
    """Shared columns for node/edge human overlays (not a mapped table)."""

    action: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Exact normalized record retained for forward-compatible audit display.
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class GraphNodeHumanOverlay(_HumanOverlayColumns, Base):
    __tablename__ = "graph_node_human_overlays"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    node_id: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        CheckConstraint(
            "action IN ('confirm', 'dispute')",
            name="ck_graph_node_human_overlay_action",
        ),
    )


class GraphEdgeHumanOverlay(_HumanOverlayColumns, Base):
    __tablename__ = "graph_edge_human_overlays"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)

    __table_args__ = (
        CheckConstraint(
            "action IN ('confirm', 'dispute')",
            name="ck_graph_edge_human_overlay_action",
        ),
    )


class GraphEdgeRuntimeOverlay(Base):
    """Cumulative runtime evidence for one logical edge.

    ``hit_count`` is advanced only by the observation cursor below, making a
    replay of the same buffered observation idempotent.  ``metadata`` keeps
    migration/provenance details but is not a source-graph payload.
    """

    __tablename__ = "graph_edge_runtime_overlays"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    hit_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "hit_count >= 0", name="ck_graph_edge_runtime_overlay_hit_count"
        ),
        CheckConstraint(
            "first_seen_at <= last_seen_at",
            name="ck_graph_edge_runtime_overlay_seen_order",
        ),
        Index(
            "ix_graph_edge_runtime_overlay_project_last_seen",
            "project_id",
            "last_seen_at",
        ),
    )


class GraphEdgeRuntimeCursor(Base):
    """How much of one buffered observation was applied to one edge."""

    __tablename__ = "graph_edge_runtime_cursors"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    applied_seen_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "source_id", "target_id", "kind"],
            [
                "graph_edge_runtime_overlays.project_id",
                "graph_edge_runtime_overlays.source_id",
                "graph_edge_runtime_overlays.target_id",
                "graph_edge_runtime_overlays.kind",
            ],
            ondelete="CASCADE",
            name="fk_graph_edge_runtime_cursor_overlay",
        ),
        ForeignKeyConstraint(
            ["observation_id", "project_id"],
            ["runtime_observations.id", "runtime_observations.project_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_runtime_cursor_observation_project",
        ),
        CheckConstraint(
            "applied_seen_count >= 0",
            name="ck_graph_edge_runtime_cursor_seen_count",
        ),
        Index(
            "ix_graph_edge_runtime_cursor_observation",
            "observation_id",
        ),
    )
