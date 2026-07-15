import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


ANALYSIS_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "published",
        "completed",
        "partial",
        "failed",
        "cancelled",
    }
)
# ``published`` is source-terminal but post-processing-active.  Only the
# statuses below close the whole AnalysisRun and therefore carry completed_at.
ANALYSIS_RUN_TERMINAL_STATUSES = frozenset(
    {"completed", "partial", "failed", "cancelled"}
)
ANALYSIS_RUN_SOURCE_PUBLISHED_STATUSES = frozenset(
    {"published", "completed", "partial"}
)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        server_default=func.now(),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    certainty: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    valid_to: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Overview / priority-ranking queries filter nodes by kind + current.
        Index("ix_nodes_project_kind", "project_id", "kind", "valid_to"),
        Index(
            "uq_nodes_current_identity",
            "project_id",
            "id",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
    )


class Edge(Base):
    __tablename__ = "edges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        server_default=func.now(),
        nullable=False,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    certainty: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    valid_to: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # The supersede UPDATE in upsert_edge filters by this exact tuple;
        # without the index it full-scans the edges table → O(N^2) on the
        # calls stage of a large analysis (PR-183).
        Index("ix_edges_identity", "project_id", "source_id", "target_id",
              "kind", "valid_to"),
        # find_callers + in-degree ranking (incoming edges by target).
        Index("ix_edges_target", "project_id", "target_id", "kind", "valid_to"),
        # find_callees + neighbour lookup (outgoing edges by source).
        Index("ix_edges_source", "project_id", "source_id", "valid_to"),
        Index(
            "uq_edges_current_identity",
            "project_id",
            "source_id",
            "target_id",
            "kind",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
    )


class NodeSource(Base):
    __tablename__ = "node_sources"

    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_name: Mapped[str] = mapped_column(String, primary_key=True)
    contributed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        server_default=func.now(),
        nullable=False,
    )
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    triggered_by: Mapped[str] = mapped_column(String, nullable=False)
    git_sha: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False, default="full")
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stats: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error_log: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Atomic graph publication metadata. ``graph_base_generation`` is
    # captured once before any staging write.  Deletion authority is kept
    # separate from free-form ``stats`` and becomes immutable when
    # ``graph_coverage_sealed_at`` is set; the promoter reads these persisted
    # fields instead of trusting retry-time caller arguments.
    graph_base_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    graph_authoritative_sources: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    graph_coverage_sealed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_analysis_runs_id_project"),
        Index(
            "ix_analysis_runs_project_status_completed",
            "project_id",
            "status",
            "completed_at",
        ),
        CheckConstraint(
            "graph_base_generation IS NULL OR graph_base_generation >= 0",
            name="ck_analysis_runs_graph_base_generation",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'published', 'completed', "
            "'partial', 'failed', 'cancelled')",
            name="ck_analysis_runs_status",
        ),
    )


class GraphHead(Base):
    """Atomic publication pointer for one project's current graph.

    Migration 0027 bootstraps every existing project as ``needs_rebuild``;
    only a successful run-scoped staging promotion may move it to ``ready``.
    Database checks enforce the state shape, and the published run is
    deletion-restricted so a ready head cannot silently lose its provenance.
    """

    __tablename__ = "graph_heads"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    current_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    # Source publication and post-publication evidence have independent
    # lifetimes. Multi-statement readers pin both revisions so a human/OTLP
    # overlay commit cannot produce a mixed response while source generation
    # remains unchanged.
    overlay_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    state: Mapped[str] = mapped_column(
        String, nullable=False, server_default="needs_rebuild"
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["current_run_id", "project_id"],
            ["analysis_runs.id", "analysis_runs.project_id"],
            ondelete="RESTRICT",
            name="fk_graph_heads_current_run_project",
        ),
        CheckConstraint(
            "state IN ('needs_rebuild', 'ready')",
            name="ck_graph_heads_state",
        ),
        CheckConstraint(
            "overlay_generation >= 0",
            name="ck_graph_heads_overlay_generation",
        ),
        CheckConstraint(
            "(state = 'needs_rebuild' AND current_run_id IS NULL "
            "AND generation = 0 AND published_at IS NULL) OR "
            "(state = 'ready' AND current_run_id IS NOT NULL "
            "AND generation > 0 AND published_at IS NOT NULL)",
            name="ck_graph_heads_shape",
        ),
        Index("ix_graph_heads_current_run", "current_run_id"),
    )


class GraphNodeStage(Base):
    """One run-isolated materialized node candidate.

    This is not a producer-contribution ledger: the primary key deliberately
    permits one materialized payload per logical identity. ``source_names``
    records every producer that emitted an identical payload in this run;
    conflicting cross-producer payloads are rejected by the stage writer.
    """

    __tablename__ = "graph_node_stage"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    certainty: Mapped[str] = mapped_column(String, nullable=False)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    # ``source_name`` remains as the canonical first owner for migration/index
    # compatibility; publication uses this complete, sorted owner set.
    source_names: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    staged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["analysis_runs.id", "analysis_runs.project_id"],
            ondelete="CASCADE",
            name="fk_graph_node_stage_run_project",
        ),
        Index("ix_graph_node_stage_run_source", "run_id", "source_name"),
        Index("ix_graph_node_stage_project_run", "project_id", "run_id"),
    )


class GraphEdgeStage(Base):
    """One run-isolated materialized edge candidate.

    Like :class:`GraphNodeStage`, identical cross-producer emissions retain a
    complete owner set while conflicting payloads fail closed.
    """

    __tablename__ = "graph_edge_stage"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    certainty: Mapped[str] = mapped_column(String, nullable=False)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    source_names: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    staged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["analysis_runs.id", "analysis_runs.project_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_stage_run_project",
        ),
        Index("ix_graph_edge_stage_run_source", "run_id", "source_name"),
        Index("ix_graph_edge_stage_project_run", "project_id", "run_id"),
    )
