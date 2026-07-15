import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DataSample(Base):
    __tablename__ = "data_samples"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    data_entity_id: Mapped[str] = mapped_column(String, nullable=False)
    sample_rows: Mapped[list] = mapped_column(JSONB, nullable=False)
    # 0005 created this as BIGINT in production; keep the ORM type aligned so
    # estimates for very large source tables are not narrowed by metadata.
    row_count_estimate: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    column_stats: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    masking_applied: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    valid_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Exact graph/source revision whose DataEntity sensitivity and masking
    # policy authorised this sample. Legacy rows remain nullable and current
    # readers deliberately hide them until a fresh capture is performed.
    source_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_git_sha: Mapped[Optional[str]] = mapped_column(
        String(length=64), nullable=True
    )
    source_graph_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    source_overlay_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    # Policy provenance is intentionally historical rather than an FK to
    # project_dbs: deleting/replacing a binding must hide the sample while
    # preserving which binding authorised its capture.  ``present``
    # distinguishes a deliberate default-policy capture from legacy NULLs.
    source_project_db_present: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )
    source_project_db_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_policy_hash: Mapped[Optional[str]] = mapped_column(
        String(length=64), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_run_id", "project_id"],
            ["analysis_runs.id", "analysis_runs.project_id"],
            name="fk_data_samples_source_run_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source_run_id IS NULL AND source_git_sha IS NULL AND "
            "source_graph_generation IS NULL AND "
            "source_overlay_generation IS NULL AND "
            "source_project_db_present IS NULL AND "
            "source_project_db_id IS NULL AND "
            "source_policy_hash IS NULL) OR "
            "(source_run_id IS NOT NULL AND source_git_sha IS NOT NULL AND "
            "source_graph_generation IS NOT NULL AND "
            "source_overlay_generation IS NOT NULL AND "
            "source_project_db_present IS NOT NULL AND "
            "source_policy_hash IS NOT NULL AND "
            "length(source_git_sha) BETWEEN 40 AND 64 AND "
            "length(source_policy_hash) = 64 AND "
            "source_graph_generation > 0 AND "
            "source_overlay_generation >= 0 AND "
            "((source_project_db_present = true AND "
            "source_project_db_id IS NOT NULL) OR "
            "(source_project_db_present = false AND "
            "source_project_db_id IS NULL)))",
            name="ck_data_samples_source_revision_shape",
        ),
        Index(
            "ix_data_samples_current_revision",
            "project_id",
            "data_entity_id",
            "source_run_id",
            "source_graph_generation",
            "source_overlay_generation",
            "source_policy_hash",
            "sampled_at",
        ),
    )


class DataQueryLog(Base):
    __tablename__ = "data_query_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    db_component_id: Mapped[str] = mapped_column(String, nullable=False)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    requester: Mapped[str] = mapped_column(String, nullable=False)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    execution_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
