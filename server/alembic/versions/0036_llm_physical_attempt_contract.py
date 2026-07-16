"""Expand the physical LLM-call ledger with a canonical v1 contract.

Revision ID: 0036_llm_physical_attempt
Revises: 0035_reset_token_invalidation
Create Date: 2026-07-16

The migration is deliberately expand-only for current writers: all v1 facts
are nullable and legacy columns remain intact.  Existing rows are marked as
contract v0 and receive only facts that can be derived without guessing.
Downgrade is refused once a v1 row exists because the legacy columns cannot
represent provider dialect, usage components, cost provenance, or retry
lineage.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0036_llm_physical_attempt"
down_revision = "0035_reset_token_invalidation"
branch_labels = None
depends_on = None


_TOKEN_VALUES = (
    "(estimated_input_tokens IS NULL OR estimated_input_tokens >= 0) AND "
    "(requested_max_output_tokens IS NULL OR "
    "requested_max_output_tokens >= 0) AND "
    "(duration_ms IS NULL OR duration_ms >= 0) AND "
    "(input_tokens IS NULL OR input_tokens >= 0) AND "
    "(uncached_input_tokens IS NULL OR uncached_input_tokens >= 0) AND "
    "(output_tokens IS NULL OR output_tokens >= 0) AND "
    "(visible_output_tokens IS NULL OR visible_output_tokens >= 0) AND "
    "(total_tokens IS NULL OR total_tokens >= 0) AND "
    "(cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0) AND "
    "(cache_write_input_tokens IS NULL OR cache_write_input_tokens >= 0) AND "
    "(reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0) AND "
    "(tool_input_tokens IS NULL OR tool_input_tokens >= 0) AND "
    "(provider_total_tokens IS NULL OR provider_total_tokens >= 0) AND "
    "(provider_turn_count IS NULL OR provider_turn_count >= 0)"
)

_USAGE_RELATIONS = (
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
    "provider_total_tokens = total_tokens)"
)

_USAGE_STATUS_SHAPE = (
    "usage_status IS NULL OR "
    "(usage_status = 'reported_complete' AND input_tokens IS NOT NULL "
    "AND output_tokens IS NOT NULL AND total_tokens IS NOT NULL) OR "
    "(usage_status = 'reported_partial' AND ("
    "input_tokens IS NOT NULL OR uncached_input_tokens IS NOT NULL OR "
    "output_tokens IS NOT NULL OR visible_output_tokens IS NOT NULL OR "
    "total_tokens IS NOT NULL OR cache_read_input_tokens IS NOT NULL OR "
    "cache_write_input_tokens IS NOT NULL OR "
    "reasoning_output_tokens IS NOT NULL OR tool_input_tokens IS NOT NULL OR "
    "provider_total_tokens IS NOT NULL OR provider_turn_count IS NOT NULL OR "
    "reported_cost_usd IS NOT NULL)) OR "
    "(usage_status = 'legacy_total_only' AND total_tokens IS NOT NULL "
    "AND input_tokens IS NULL AND uncached_input_tokens IS NULL AND "
    "output_tokens IS NULL AND visible_output_tokens IS NULL AND "
    "cache_read_input_tokens IS NULL AND cache_write_input_tokens IS NULL AND "
    "reasoning_output_tokens IS NULL AND tool_input_tokens IS NULL AND "
    "provider_turn_count IS NULL AND reported_cost_usd IS NULL) OR "
    "(usage_status IN ('unavailable', 'lost_after_dispatch', "
    "'legacy_unknown') AND input_tokens IS NULL AND "
    "uncached_input_tokens IS NULL AND output_tokens IS NULL AND "
    "visible_output_tokens IS NULL AND total_tokens IS NULL AND "
    "cache_read_input_tokens IS NULL AND cache_write_input_tokens IS NULL AND "
    "reasoning_output_tokens IS NULL AND tool_input_tokens IS NULL AND "
    "provider_total_tokens IS NULL AND provider_turn_count IS NULL AND "
    "reported_cost_usd IS NULL)"
)

_V1_SHAPE = (
    "contract_version = 0 OR ("
    "operation_id IS NOT NULL AND budget_scope_id IS NOT NULL AND "
    "purpose IS NOT NULL AND attempt_no > 0 AND attempt_kind IS NOT NULL AND "
    "provider IS NOT NULL AND provider_mode IS NOT NULL AND "
    "requested_model IS NOT NULL AND input_fingerprint IS NOT NULL AND "
    "estimated_input_tokens IS NOT NULL AND "
    "input_estimate_method IS NOT NULL AND "
    "requested_max_output_tokens > 0 AND attempt_status IS NOT NULL AND "
    "result_status IS NOT NULL AND started_at IS NOT NULL AND "
    "usage_status IS NOT NULL AND usage_source IS NOT NULL)"
)

_V1_ENUMS = (
    "contract_version = 0 OR ("
    "purpose IN ('summary', 'agent_extract', 'flow', 'chat_rewrite', "
    "'chat_answer', 'second_opinion') AND "
    "attempt_kind IN ('primary', 'retry', 'fallback') AND "
    "attempt_status IN ('started', 'completed', 'failed', 'timeout', "
    "'cancelled', 'unknown') AND "
    "result_status IN ('pending', 'accepted', 'empty', 'parse_rejected', "
    "'schema_rejected', 'grounding_rejected', 'output_limit_rejected', "
    "'not_applicable') AND "
    "usage_status IN ('reported_complete', 'reported_partial', "
    "'unavailable', 'lost_after_dispatch') AND "
    "usage_source IN ('provider_api', 'agent_sdk', 'configured_estimate', "
    "'unknown'))"
)

_V1_LIFECYCLE = (
    "contract_version = 0 OR "
    "((attempt_status = 'started' AND completed_at IS NULL AND "
    "duration_ms IS NULL AND result_status = 'pending') OR "
    "(attempt_status <> 'started' AND completed_at IS NOT NULL AND "
    "duration_ms IS NOT NULL)) AND "
    "(result_status <> 'accepted' OR attempt_status = 'completed') AND "
    "(parent_attempt_id IS NULL OR parent_attempt_id <> id)"
)

_ADDED_COLUMNS = (
    "pricing_version",
    "reserved_cost_usd",
    "estimated_cost_usd",
    "reported_cost_usd",
    "provider_turn_count",
    "provider_total_tokens",
    "tool_input_tokens",
    "reasoning_output_tokens",
    "cache_write_input_tokens",
    "cache_read_input_tokens",
    "total_tokens",
    "visible_output_tokens",
    "output_tokens",
    "uncached_input_tokens",
    "input_tokens",
    "usage_source",
    "usage_status",
    "duration_ms",
    "completed_at",
    "started_at",
    "finish_reason",
    "failure_code",
    "result_status",
    "attempt_status",
    "requested_max_output_tokens",
    "input_estimate_method",
    "estimated_input_tokens",
    "input_fingerprint",
    "provider_request_id",
    "resolved_model",
    "requested_model",
    "provider_mode",
    "provider",
    "attempt_kind",
    "parent_attempt_id",
    "attempt_no",
    "purpose",
    "budget_scope_id",
    "operation_id",
    "contract_version",
)


def upgrade() -> None:
    op.add_column(
        "llm_calls",
        sa.Column(
            "contract_version",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "llm_calls",
        sa.Column("budget_scope_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("llm_calls", sa.Column("purpose", sa.String(32), nullable=True))
    op.add_column("llm_calls", sa.Column("attempt_no", sa.Integer(), nullable=True))
    op.add_column(
        "llm_calls",
        sa.Column("parent_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "llm_calls", sa.Column("attempt_kind", sa.String(16), nullable=True)
    )
    op.add_column("llm_calls", sa.Column("provider", sa.String(32), nullable=True))
    op.add_column(
        "llm_calls", sa.Column("provider_mode", sa.String(32), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("requested_model", sa.String(255), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("resolved_model", sa.String(255), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("provider_request_id", sa.String(255), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("input_fingerprint", sa.String(64), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("estimated_input_tokens", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("input_estimate_method", sa.String(32), nullable=True)
    )
    op.add_column(
        "llm_calls",
        sa.Column("requested_max_output_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "llm_calls", sa.Column("attempt_status", sa.String(24), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("result_status", sa.String(32), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("failure_code", sa.String(64), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("finish_reason", sa.String(64), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("duration_ms", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("usage_status", sa.String(32), nullable=True)
    )
    op.add_column(
        "llm_calls", sa.Column("usage_source", sa.String(24), nullable=True)
    )
    for column_name in (
        "input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "visible_output_tokens",
        "total_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
        "tool_input_tokens",
        "provider_total_tokens",
        "provider_turn_count",
    ):
        op.add_column(
            "llm_calls", sa.Column(column_name, sa.BigInteger(), nullable=True)
        )
    for column_name in (
        "reported_cost_usd",
        "estimated_cost_usd",
        "reserved_cost_usd",
    ):
        op.add_column(
            "llm_calls", sa.Column(column_name, sa.Numeric(20, 10), nullable=True)
        )
    op.add_column(
        "llm_calls", sa.Column("pricing_version", sa.String(64), nullable=True)
    )

    # Backfill only facts carried by the legacy row.  In particular, model
    # text does not prove a provider or provider mode, and generated_at does
    # not prove a completion time or duration.
    op.execute(
        """
        UPDATE llm_calls
        SET operation_id = id,
            attempt_no = 1,
            purpose = CASE
                WHEN target_id LIKE 'agent:%' THEN 'agent_extract'
                WHEN target_id LIKE 'flow:%' THEN 'flow'
                WHEN level BETWEEN 1 AND 3 THEN 'summary'
                ELSE 'legacy'
            END,
            resolved_model = model_used,
            started_at = generated_at,
            attempt_status = CASE
                WHEN status = 'timeout' THEN 'timeout'
                WHEN status = 'cancelled' THEN 'cancelled'
                WHEN status IN ('completed', 'legacy_summary') THEN 'completed'
                WHEN status = 'failed' THEN 'failed'
                ELSE 'unknown'
            END,
            usage_status = CASE
                WHEN tokens_used IS NULL THEN 'legacy_unknown'
                ELSE 'legacy_total_only'
            END,
            usage_source = 'legacy',
            total_tokens = tokens_used,
            provider_total_tokens = tokens_used
        """
    )

    op.create_foreign_key(
        "fk_llm_calls_parent_attempt",
        "llm_calls",
        "llm_calls",
        ["parent_attempt_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_llm_calls_operation_attempt",
        "llm_calls",
        ["operation_id", "attempt_no"],
    )
    op.create_check_constraint(
        "ck_llm_calls_contract_version",
        "llm_calls",
        "contract_version IN (0, 1)",
    )
    op.create_check_constraint(
        "ck_llm_calls_input_fingerprint",
        "llm_calls",
        "input_fingerprint IS NULL OR length(input_fingerprint) = 64",
    )
    op.create_check_constraint(
        "ck_llm_calls_token_values", "llm_calls", _TOKEN_VALUES
    )
    op.create_check_constraint(
        "ck_llm_calls_cost_values",
        "llm_calls",
        "(reported_cost_usd IS NULL OR reported_cost_usd >= 0) AND "
        "(estimated_cost_usd IS NULL OR estimated_cost_usd >= 0) AND "
        "(reserved_cost_usd IS NULL OR reserved_cost_usd >= 0)",
    )
    op.create_check_constraint(
        "ck_llm_calls_usage_relations", "llm_calls", _USAGE_RELATIONS
    )
    op.create_check_constraint(
        "ck_llm_calls_usage_status_shape", "llm_calls", _USAGE_STATUS_SHAPE
    )
    op.create_check_constraint("ck_llm_calls_v1_shape", "llm_calls", _V1_SHAPE)
    op.create_check_constraint("ck_llm_calls_v1_enums", "llm_calls", _V1_ENUMS)
    op.create_check_constraint(
        "ck_llm_calls_v1_lifecycle", "llm_calls", _V1_LIFECYCLE
    )
    op.create_index(
        "ix_llm_calls_budget_scope", "llm_calls", ["budget_scope_id"], unique=False
    )
    op.create_index(
        "ix_llm_calls_project_started",
        "llm_calls",
        ["project_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    # This must remain the first operation.  A v1 row contains accounting and
    # retry facts that cannot be represented by the old ledger.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM llm_calls WHERE contract_version = 1
            )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0036: canonical LLM attempts exist'
                    USING HINT =
                        'Export and retain the v1 attempt ledger, then remove '
                        'or explicitly map every contract_version=1 row before '
                        'retrying; legacy columns cannot preserve usage '
                        'components, cost provenance, or retry lineage.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_index("ix_llm_calls_project_started", table_name="llm_calls")
    op.drop_index("ix_llm_calls_budget_scope", table_name="llm_calls")
    op.drop_constraint(
        "ck_llm_calls_v1_lifecycle", "llm_calls", type_="check"
    )
    op.drop_constraint("ck_llm_calls_v1_enums", "llm_calls", type_="check")
    op.drop_constraint("ck_llm_calls_v1_shape", "llm_calls", type_="check")
    op.drop_constraint(
        "ck_llm_calls_usage_status_shape", "llm_calls", type_="check"
    )
    op.drop_constraint(
        "ck_llm_calls_usage_relations", "llm_calls", type_="check"
    )
    op.drop_constraint("ck_llm_calls_cost_values", "llm_calls", type_="check")
    op.drop_constraint("ck_llm_calls_token_values", "llm_calls", type_="check")
    op.drop_constraint(
        "ck_llm_calls_input_fingerprint", "llm_calls", type_="check"
    )
    op.drop_constraint(
        "ck_llm_calls_contract_version", "llm_calls", type_="check"
    )
    op.drop_constraint(
        "uq_llm_calls_operation_attempt", "llm_calls", type_="unique"
    )
    op.drop_constraint(
        "fk_llm_calls_parent_attempt", "llm_calls", type_="foreignkey"
    )
    for column_name in _ADDED_COLUMNS:
        op.drop_column("llm_calls", column_name)
