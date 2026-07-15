import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # Stable logical subject identity. A bitemporal Edge UUID changes on
    # publication, so edge findings hash (kind, source, target, edge kind)
    # instead and merely refresh ``subject_edge_id`` as provenance.
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    subject_node_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    subject_edge_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # PR-50 — solution-analysis columns. The 19th-round value audit
    # found findings were "observations, not decisions": severity was
    # a static 3-level string with no business-priority ordering and
    # no fix guidance. These columns turn a finding into something
    # an operator can triage and act on.
    #
    # ``risk_score`` 0-100 — computed by app.merge.risk from severity ×
    # whether the subject is exercised in production × blast radius.
    # ``remediation`` — a deterministic fix hint keyed off ``kind``.
    # ``cwe_id`` — the CWE catalogue entry where one applies, so a
    # finding can be cross-referenced against a compliance matrix.
    risk_score: Mapped[int] = mapped_column(nullable=False, default=0)
    remediation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cwe_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    validated_graph_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    validated_overlay_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    validated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "kind",
            "identity_key",
            name="uq_findings_project_kind_identity",
        ),
        Index(
            "ix_findings_project_graph_validation",
            "project_id",
            "validated_graph_generation",
            "validated_overlay_generation",
            "status",
        ),
        CheckConstraint(
            "length(identity_key) = 64",
            name="ck_findings_identity_key_length",
        ),
        CheckConstraint(
            "(validated_graph_generation IS NULL AND "
            "validated_overlay_generation IS NULL AND validated_at IS NULL) OR "
            "(validated_graph_generation IS NOT NULL AND "
            "validated_overlay_generation IS NOT NULL AND "
            "validated_at IS NOT NULL AND validated_graph_generation > 0 AND "
            "validated_overlay_generation >= 0)",
            name="ck_findings_graph_validation_marker",
        ),
    )


class Summary(Base):
    __tablename__ = "summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    level: Mapped[int] = mapped_column(nullable=False)
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # A narrative is current only when this marker exactly matches the ready
    # GraphHead source generation. ``analysis_run_id`` remains immutable
    # provenance when an exact-evidence cache hit refreshes this marker.
    validated_graph_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    validated_overlay_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    validated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detailed: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claims: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    open_questions: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # Cache/provenance metadata is not a model-authored claim.  Keeping the
    # digest in its own column prevents internal control data from appearing
    # as source evidence through MCP consumers.
    evidence_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    model_used: Mapped[str] = mapped_column(String, nullable=False)
    tokens_used: Mapped[Optional[int]] = mapped_column(nullable=True)
    # PR-138b — when the extractor fell through to the stub path, this
    # field carries the reason ("agent_sdk_timeout" / "budget_exceeded"
    # / "anthropic_http_error" / …). Null on the happy path. Lets the
    # dashboard "Why did this miss the LLM?" panel render structured
    # data instead of parsing the model_used string.
    fallback_reason: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    superseded_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        # Enabled budget checks sum recent tokens for one project.
        Index("ix_summaries_project_generated", "project_id", "generated_at"),
        Index(
            "ix_summaries_current_graph_generation",
            "project_id",
            "validated_graph_generation",
            "validated_overlay_generation",
            postgresql_where=text("superseded_by IS NULL"),
            sqlite_where=text("superseded_by IS NULL"),
        ),
        Index(
            "uq_summaries_current_validated_target",
            "project_id",
            "target_id",
            "level",
            unique=True,
            postgresql_where=text(
                "superseded_by IS NULL AND "
                "validated_graph_generation IS NOT NULL"
            ),
            sqlite_where=text(
                "superseded_by IS NULL AND "
                "validated_graph_generation IS NOT NULL"
            ),
        ),
        CheckConstraint(
            "(validated_graph_generation IS NULL AND "
            "validated_overlay_generation IS NULL AND validated_at IS NULL) OR "
            "(validated_graph_generation IS NOT NULL AND "
            "validated_overlay_generation IS NOT NULL AND "
            "validated_at IS NOT NULL AND validated_graph_generation > 0 AND "
            "validated_overlay_generation >= 0)",
            name="ck_summaries_graph_validation_marker",
        ),
    )


class LLMCall(Base):
    """One physical optional-analysis model invocation.

    Summary rows are products, not a reliable call ledger: map/reduce partials
    and malformed responses may never become summaries.  This table makes the
    budget and run-level ROI account for each attempted physical call once.
    """

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    level: Mapped[int] = mapped_column(nullable=False)
    model_used: Mapped[str] = mapped_column(String, nullable=False)
    tokens_used: Mapped[Optional[int]] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fallback_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_llm_calls_project_generated", "project_id", "generated_at"),
        Index("ix_llm_calls_analysis_run", "analysis_run_id"),
    )
