import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text, func
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
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detailed: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claims: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    open_questions: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
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
