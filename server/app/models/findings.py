import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
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
    # Durable model output is first persisted as an encrypted semantic
    # candidate.  These three fields bind the grounded Summary projection to
    # its final candidate-producing physical attempt without forcing legacy/
    # deterministic rows to invent model provenance. For multi-chunk rollups,
    # ``tokens_used`` and ``model_used`` are the immutable receipt projection
    # for that final call. Whole map+reduce usage is calculated from the
    # component LLMCall ledger rather than copied from mutable caller state.
    # Writers populate the triplet atomically; the database rejects every
    # partially populated shape.
    llm_attempt_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_calls.id", ondelete="RESTRICT"),
        nullable=True,
    )
    semantic_binding_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    projection_sha256: Mapped[Optional[str]] = mapped_column(
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
        Index(
            "uq_summaries_llm_attempt",
            "llm_attempt_id",
            unique=True,
            postgresql_where=text("llm_attempt_id IS NOT NULL"),
            sqlite_where=text("llm_attempt_id IS NOT NULL"),
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
        CheckConstraint(
            "(llm_attempt_id IS NULL AND "
            "semantic_binding_fingerprint IS NULL AND "
            "projection_sha256 IS NULL) OR "
            "(llm_attempt_id IS NOT NULL AND "
            "semantic_binding_fingerprint IS NOT NULL AND "
            "projection_sha256 IS NOT NULL)",
            name="ck_summaries_llm_provenance_shape",
        ),
        CheckConstraint(
            "(semantic_binding_fingerprint IS NULL AND "
            "projection_sha256 IS NULL) OR "
            "(semantic_binding_fingerprint IS NOT NULL AND "
            "projection_sha256 IS NOT NULL AND "
            "length(semantic_binding_fingerprint) = 64 AND "
            "semantic_binding_fingerprint = "
            "lower(semantic_binding_fingerprint) AND "
            "length(replace(replace(replace(replace(replace(replace("
            "replace(replace(replace(replace(replace(replace(replace("
            "replace(replace(replace(semantic_binding_fingerprint, "
            "'0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
            "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), "
            "'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), "
            "'f', '')) = 0 AND "
            "length(projection_sha256) = 64 AND "
            "projection_sha256 = lower(projection_sha256) AND "
            "length(replace(replace(replace(replace(replace(replace("
            "replace(replace(replace(replace(replace(replace(replace("
            "replace(replace(replace(projection_sha256, '0', ''), "
            "'1', ''), '2', ''), '3', ''), '4', ''), '5', ''), "
            "'6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
            "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)",
            name="ck_summaries_llm_provenance_sha256",
        ),
    )


class LLMProjectBudgetAccount(Base):
    """Per-project serialization row and immutable hard-dollar policy snapshot."""

    __tablename__ = "llm_project_budget_accounts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    cap_usd: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    window_sec: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "length(policy_version) BETWEEN 1 AND 32",
            name="ck_llm_project_budget_accounts_policy_version",
        ),
        CheckConstraint(
            "cap_usd > 0 AND window_sec >= 60",
            name="ck_llm_project_budget_accounts_policy",
        ),
    )


class LLMBudgetScope(Base):
    """Crash-durable policy snapshot for optional physical LLM calls.

    The deterministic index-first path does not need a scope.  An opt-in AI
    operation creates one policy snapshot before dispatch, then advances only
    the reservation counters so a worker restart cannot reset its hard limits.
    """

    __tablename__ = "llm_budget_scopes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    analysis_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    scope_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    max_calls: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wall_time_sec: Mapped[int] = mapped_column(BigInteger, nullable=False)
    calls_reserved: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    input_tokens_reserved: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    output_tokens_reserved: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0"), nullable=False
    )
    exhausted_reason: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_llm_budget_scopes_project_started",
            "project_id",
            "started_at",
        ),
        Index("ix_llm_budget_scopes_analysis_run", "analysis_run_id"),
        Index(
            "ix_llm_budget_scopes_deadline",
            "deadline_at",
            "id",
        ),
        CheckConstraint(
            "scope_kind IN ('analysis_run', 'request')",
            name="ck_llm_budget_scopes_scope_kind",
        ),
        CheckConstraint(
            "(scope_kind = 'analysis_run' AND analysis_run_id IS NOT NULL "
            "AND id = analysis_run_id) OR "
            "(scope_kind = 'request' AND analysis_run_id IS NULL)",
            name="ck_llm_budget_scopes_identity",
        ),
        CheckConstraint(
            "length(policy_version) BETWEEN 1 AND 32",
            name="ck_llm_budget_scopes_policy_version",
        ),
        CheckConstraint(
            "max_calls > 0 AND max_input_tokens > 0 AND "
            "max_output_tokens > 0 AND wall_time_sec > 0",
            name="ck_llm_budget_scopes_limits",
        ),
        CheckConstraint(
            "calls_reserved >= 0 AND calls_reserved <= max_calls AND "
            "input_tokens_reserved >= 0 AND "
            "input_tokens_reserved <= max_input_tokens AND "
            "output_tokens_reserved >= 0 AND "
            "output_tokens_reserved <= max_output_tokens",
            name="ck_llm_budget_scopes_reservations",
        ),
        CheckConstraint(
            "deadline_at >= started_at",
            name="ck_llm_budget_scopes_deadline",
        ),
        CheckConstraint(
            "exhausted_reason IS NULL OR "
            "length(exhausted_reason) BETWEEN 1 AND 64",
            name="ck_llm_budget_scopes_exhausted_reason",
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

    # Expand-only v1 physical-attempt contract.  Legacy writers/readers keep
    # using the columns above while integrations migrate one call path at a
    # time.  Unknown provider facts remain NULL; they are never fabricated as
    # zero-token calls.
    contract_version: Mapped[int] = mapped_column(
        SmallInteger,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    operation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    budget_scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    purpose: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    attempt_no: Mapped[Optional[int]] = mapped_column(nullable=True)
    parent_attempt_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_calls.id", ondelete="SET NULL"),
        nullable=True,
    )
    attempt_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    provider_mode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    requested_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    resolved_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_request_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    input_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    estimated_input_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    input_estimate_method: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    requested_max_output_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    reserved_input_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    reserved_output_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    reservation_price_contract_id: Mapped[Optional[str]] = mapped_column(
        String(96), nullable=True
    )
    attempt_status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    result_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    failure_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    finish_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    usage_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    usage_source: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    uncached_input_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    output_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    visible_output_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    total_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    cache_read_input_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    cache_write_input_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    reasoning_output_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    tool_input_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    provider_total_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    provider_turn_count: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    reported_cost_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    estimated_cost_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    reserved_cost_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    pricing_version: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    __table_args__ = (
        Index("ix_llm_calls_project_generated", "project_id", "generated_at"),
        Index("ix_llm_calls_analysis_run", "analysis_run_id"),
        Index("ix_llm_calls_budget_scope", "budget_scope_id"),
        Index(
            "ix_llm_calls_open_budget_started",
            "budget_scope_id",
            "started_at",
            postgresql_where=text(
                "contract_version = 1 AND attempt_status = 'started'"
            ),
            sqlite_where=text(
                "contract_version = 1 AND attempt_status = 'started'"
            ),
        ),
        Index("ix_llm_calls_project_started", "project_id", "started_at"),
        Index(
            "ix_llm_calls_project_reserved_generated",
            "project_id",
            "generated_at",
            postgresql_where=text("reserved_cost_usd IS NOT NULL"),
            sqlite_where=text("reserved_cost_usd IS NOT NULL"),
        ),
        UniqueConstraint(
            "operation_id",
            "attempt_no",
            name="uq_llm_calls_operation_attempt",
        ),
        CheckConstraint(
            "contract_version IN (0, 1)",
            name="ck_llm_calls_contract_version",
        ),
        CheckConstraint(
            "input_fingerprint IS NULL OR length(input_fingerprint) = 64",
            name="ck_llm_calls_input_fingerprint",
        ),
        CheckConstraint(
            "(estimated_input_tokens IS NULL OR estimated_input_tokens >= 0) AND "
            "(requested_max_output_tokens IS NULL OR "
            "requested_max_output_tokens >= 0) AND "
            "(reserved_input_tokens IS NULL OR reserved_input_tokens >= 0) AND "
            "(reserved_output_tokens IS NULL OR reserved_output_tokens >= 0) AND "
            "(duration_ms IS NULL OR duration_ms >= 0) AND "
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(uncached_input_tokens IS NULL OR uncached_input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0) AND "
            "(visible_output_tokens IS NULL OR visible_output_tokens >= 0) AND "
            "(total_tokens IS NULL OR total_tokens >= 0) AND "
            "(cache_read_input_tokens IS NULL OR "
            "cache_read_input_tokens >= 0) AND "
            "(cache_write_input_tokens IS NULL OR "
            "cache_write_input_tokens >= 0) AND "
            "(reasoning_output_tokens IS NULL OR "
            "reasoning_output_tokens >= 0) AND "
            "(tool_input_tokens IS NULL OR tool_input_tokens >= 0) AND "
            "(provider_total_tokens IS NULL OR provider_total_tokens >= 0) AND "
            "(provider_turn_count IS NULL OR provider_turn_count >= 0)",
            name="ck_llm_calls_token_values",
        ),
        CheckConstraint(
            "(reported_cost_usd IS NULL OR reported_cost_usd >= 0) AND "
            "(estimated_cost_usd IS NULL OR estimated_cost_usd >= 0) AND "
            "(reserved_cost_usd IS NULL OR reserved_cost_usd >= 0)",
            name="ck_llm_calls_cost_values",
        ),
        CheckConstraint(
            "(reserved_input_tokens IS NULL AND reserved_output_tokens IS NULL "
            "AND reservation_price_contract_id IS NULL "
            "AND reserved_cost_usd IS NULL) OR "
            "(reserved_input_tokens > 0 AND reserved_output_tokens > 0 "
            "AND reservation_price_contract_id IS NOT NULL "
            "AND length(reservation_price_contract_id) BETWEEN 1 AND 96 "
            "AND reserved_cost_usd > 0 "
            "AND estimated_input_tokens <= reserved_input_tokens "
            "AND requested_max_output_tokens = reserved_output_tokens)",
            name="ck_llm_calls_cost_reservation_shape",
        ),
        CheckConstraint(
            "(input_tokens IS NULL OR uncached_input_tokens IS NULL OR "
            "uncached_input_tokens <= input_tokens) AND "
            "(input_tokens IS NULL OR cache_read_input_tokens IS NULL OR "
            "cache_read_input_tokens <= input_tokens) AND "
            "(input_tokens IS NULL OR cache_write_input_tokens IS NULL OR "
            "cache_write_input_tokens <= input_tokens) AND "
            "(input_tokens IS NULL OR "
            "COALESCE(uncached_input_tokens, 0) + "
            "COALESCE(cache_read_input_tokens, 0) + "
            "COALESCE(cache_write_input_tokens, 0) <= input_tokens) AND "
            "(output_tokens IS NULL OR visible_output_tokens IS NULL OR "
            "visible_output_tokens <= output_tokens) AND "
            "(output_tokens IS NULL OR reasoning_output_tokens IS NULL OR "
            "reasoning_output_tokens <= output_tokens) AND "
            "(output_tokens IS NULL OR "
            "COALESCE(visible_output_tokens, 0) + "
            "COALESCE(reasoning_output_tokens, 0) <= output_tokens) AND "
            "(input_tokens IS NULL OR output_tokens IS NULL OR "
            "(total_tokens IS NOT NULL AND "
            "total_tokens = input_tokens + output_tokens)) AND "
            "(provider_total_tokens IS NULL OR total_tokens IS NULL OR "
            "provider_total_tokens = total_tokens)",
            name="ck_llm_calls_usage_relations",
        ),
        CheckConstraint(
            "usage_status IS NULL OR "
            "(usage_status = 'reported_complete' AND input_tokens IS NOT NULL "
            "AND output_tokens IS NOT NULL AND total_tokens IS NOT NULL) OR "
            "(usage_status = 'reported_partial' AND ("
            "input_tokens IS NOT NULL OR uncached_input_tokens IS NOT NULL OR "
            "output_tokens IS NOT NULL OR visible_output_tokens IS NOT NULL OR "
            "total_tokens IS NOT NULL OR cache_read_input_tokens IS NOT NULL OR "
            "cache_write_input_tokens IS NOT NULL OR "
            "reasoning_output_tokens IS NOT NULL OR "
            "tool_input_tokens IS NOT NULL OR provider_total_tokens IS NOT NULL OR "
            "provider_turn_count IS NOT NULL OR reported_cost_usd IS NOT NULL OR "
            "estimated_cost_usd IS NOT NULL)) OR "
            "(usage_status = 'legacy_total_only' AND total_tokens IS NOT NULL "
            "AND input_tokens IS NULL AND uncached_input_tokens IS NULL AND "
            "output_tokens IS NULL AND visible_output_tokens IS NULL AND "
            "cache_read_input_tokens IS NULL AND "
            "cache_write_input_tokens IS NULL AND "
            "reasoning_output_tokens IS NULL AND tool_input_tokens IS NULL AND "
            "provider_turn_count IS NULL AND reported_cost_usd IS NULL AND "
            "estimated_cost_usd IS NULL) OR "
            "(usage_status IN ('unavailable', 'lost_after_dispatch', "
            "'legacy_unknown') AND input_tokens IS NULL AND "
            "uncached_input_tokens IS NULL AND output_tokens IS NULL AND "
            "visible_output_tokens IS NULL AND total_tokens IS NULL AND "
            "cache_read_input_tokens IS NULL AND "
            "cache_write_input_tokens IS NULL AND "
            "reasoning_output_tokens IS NULL AND tool_input_tokens IS NULL AND "
            "provider_total_tokens IS NULL AND provider_turn_count IS NULL AND "
            "reported_cost_usd IS NULL AND estimated_cost_usd IS NULL)",
            name="ck_llm_calls_usage_status_shape",
        ),
        CheckConstraint(
            "contract_version = 0 OR ("
            "operation_id IS NOT NULL AND budget_scope_id IS NOT NULL AND "
            "purpose IS NOT NULL AND attempt_no > 0 AND "
            "attempt_kind IS NOT NULL AND provider IS NOT NULL AND "
            "provider_mode IS NOT NULL AND requested_model IS NOT NULL AND "
            "input_fingerprint IS NOT NULL AND "
            "estimated_input_tokens IS NOT NULL AND "
            "input_estimate_method IS NOT NULL AND "
            "requested_max_output_tokens > 0 AND "
            "attempt_status IS NOT NULL AND result_status IS NOT NULL AND "
            "started_at IS NOT NULL AND usage_status IS NOT NULL AND "
            "usage_source IS NOT NULL)",
            name="ck_llm_calls_v1_shape",
        ),
        CheckConstraint(
            "contract_version = 0 OR ("
            "purpose IN ('summary', 'agent_extract', 'flow', 'chat_rewrite', "
            "'chat_answer', 'second_opinion') AND "
            "attempt_kind IN ('primary', 'retry', 'fallback') AND "
            "attempt_status IN ('started', 'completed', 'failed', 'timeout', "
            "'cancelled', 'unknown') AND "
            "result_status IN ('pending', 'accepted', 'empty', "
            "'parse_rejected', 'schema_rejected', 'grounding_rejected', "
            "'output_limit_rejected', 'not_applicable') AND "
            "usage_status IN ('reported_complete', 'reported_partial', "
            "'unavailable', 'lost_after_dispatch') AND "
            "usage_source IN ('provider_api', 'agent_sdk', "
            "'configured_estimate', 'unknown'))",
            name="ck_llm_calls_v1_enums",
        ),
        CheckConstraint(
            "contract_version = 0 OR "
            "((attempt_status = 'started' AND completed_at IS NULL AND "
            "duration_ms IS NULL AND result_status = 'pending') OR "
            "(attempt_status <> 'started' AND completed_at IS NOT NULL AND "
            "duration_ms IS NOT NULL)) AND "
            "(result_status <> 'accepted' OR attempt_status = 'completed') AND "
            "(parent_attempt_id IS NULL OR parent_attempt_id <> id)",
            name="ck_llm_calls_v1_lifecycle",
        ),
    )
