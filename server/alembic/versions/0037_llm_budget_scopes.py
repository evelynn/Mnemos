"""Add crash-durable hard-budget scopes for optional LLM work.

Revision ID: 0037_llm_budget_scopes
Revises: 0036_llm_physical_attempt
Create Date: 2026-07-16

The policy columns are a creation-time snapshot.  Reservation counters are
stored separately so an interrupted worker cannot restart an analysis or
request with a fresh in-process budget.  The table is intentionally not
referenced by ``llm_calls`` in this expand-only slice; consumers can migrate
one physical-call path at a time without breaking legacy writers.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0037_llm_budget_scopes"
down_revision = "0036_llm_physical_attempt"
branch_labels = None
depends_on = None


_LIMITS = (
    "max_calls > 0 AND max_input_tokens > 0 AND "
    "max_output_tokens > 0 AND wall_time_sec > 0"
)

_RESERVATIONS = (
    "calls_reserved >= 0 AND calls_reserved <= max_calls AND "
    "input_tokens_reserved >= 0 AND "
    "input_tokens_reserved <= max_input_tokens AND "
    "output_tokens_reserved >= 0 AND "
    "output_tokens_reserved <= max_output_tokens"
)

_USAGE_STATUS_SHAPE_0036 = (
    "usage_status IS NULL OR "
    "(usage_status = 'reported_complete' AND input_tokens IS NOT NULL "
    "AND output_tokens IS NOT NULL AND total_tokens IS NOT NULL) OR "
    "(usage_status = 'reported_partial' AND ("
    "input_tokens IS NOT NULL OR uncached_input_tokens IS NOT NULL OR "
    "output_tokens IS NOT NULL OR visible_output_tokens IS NOT NULL OR "
    "total_tokens IS NOT NULL OR cache_read_input_tokens IS NOT NULL OR "
    "cache_write_input_tokens IS NOT NULL OR reasoning_output_tokens IS NOT NULL OR "
    "tool_input_tokens IS NOT NULL OR provider_total_tokens IS NOT NULL OR "
    "provider_turn_count IS NOT NULL OR reported_cost_usd IS NOT NULL)) OR "
    "(usage_status = 'legacy_total_only' AND total_tokens IS NOT NULL "
    "AND input_tokens IS NULL AND uncached_input_tokens IS NULL AND "
    "output_tokens IS NULL AND visible_output_tokens IS NULL AND "
    "cache_read_input_tokens IS NULL AND cache_write_input_tokens IS NULL AND "
    "reasoning_output_tokens IS NULL AND tool_input_tokens IS NULL AND "
    "provider_turn_count IS NULL AND reported_cost_usd IS NULL) OR "
    "(usage_status IN ('unavailable', 'lost_after_dispatch', 'legacy_unknown') "
    "AND input_tokens IS NULL AND uncached_input_tokens IS NULL AND "
    "output_tokens IS NULL AND visible_output_tokens IS NULL AND "
    "total_tokens IS NULL AND cache_read_input_tokens IS NULL AND "
    "cache_write_input_tokens IS NULL AND reasoning_output_tokens IS NULL AND "
    "tool_input_tokens IS NULL AND provider_total_tokens IS NULL AND "
    "provider_turn_count IS NULL AND reported_cost_usd IS NULL)"
)

_USAGE_STATUS_SHAPE_0037 = _USAGE_STATUS_SHAPE_0036.replace(
    "reported_cost_usd IS NOT NULL))",
    "reported_cost_usd IS NOT NULL OR estimated_cost_usd IS NOT NULL))",
).replace(
    "provider_turn_count IS NULL AND reported_cost_usd IS NULL) OR",
    "provider_turn_count IS NULL AND reported_cost_usd IS NULL AND "
    "estimated_cost_usd IS NULL) OR",
).replace(
    "provider_turn_count IS NULL AND reported_cost_usd IS NULL)",
    "provider_turn_count IS NULL AND reported_cost_usd IS NULL AND "
    "estimated_cost_usd IS NULL)",
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_llm_calls_usage_status_shape",
        "llm_calls",
        type_="check",
    )
    op.create_check_constraint(
        "ck_llm_calls_usage_status_shape",
        "llm_calls",
        _USAGE_STATUS_SHAPE_0037,
    )
    op.create_table(
        "llm_budget_scopes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "analysis_run_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("scope_kind", sa.String(24), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("max_calls", sa.BigInteger(), nullable=False),
        sa.Column("max_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("max_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("wall_time_sec", sa.BigInteger(), nullable=False),
        sa.Column(
            "calls_reserved",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "input_tokens_reserved",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "output_tokens_reserved",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("exhausted_reason", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_kind IN ('analysis_run', 'request')",
            name="ck_llm_budget_scopes_scope_kind",
        ),
        sa.CheckConstraint(
            "(scope_kind = 'analysis_run' AND analysis_run_id IS NOT NULL "
            "AND id = analysis_run_id) OR "
            "(scope_kind = 'request' AND analysis_run_id IS NULL)",
            name="ck_llm_budget_scopes_identity",
        ),
        sa.CheckConstraint(
            "length(policy_version) BETWEEN 1 AND 32",
            name="ck_llm_budget_scopes_policy_version",
        ),
        sa.CheckConstraint(
            _LIMITS,
            name="ck_llm_budget_scopes_limits",
        ),
        sa.CheckConstraint(
            _RESERVATIONS,
            name="ck_llm_budget_scopes_reservations",
        ),
        sa.CheckConstraint(
            "deadline_at >= started_at",
            name="ck_llm_budget_scopes_deadline",
        ),
        sa.CheckConstraint(
            "exhausted_reason IS NULL OR "
            "length(exhausted_reason) BETWEEN 1 AND 64",
            name="ck_llm_budget_scopes_exhausted_reason",
        ),
    )
    op.create_index(
        "ix_llm_budget_scopes_project_started",
        "llm_budget_scopes",
        ["project_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_llm_budget_scopes_analysis_run",
        "llm_budget_scopes",
        ["analysis_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_budget_scopes_deadline",
        "llm_budget_scopes",
        ["deadline_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_calls_open_budget_started",
        "llm_calls",
        ["budget_scope_id", "started_at"],
        unique=False,
        postgresql_where=sa.text(
            "contract_version = 1 AND attempt_status = 'started'"
        ),
    )


def downgrade() -> None:
    # The policy snapshot and consumed reservations have no pre-0037 home.
    # Refuse to erase them because a later resume could otherwise exceed the
    # original hard limit after the database is upgraded again.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (SELECT 1 FROM llm_budget_scopes LIMIT 1)
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0037: durable LLM budget scopes exist'
                    USING HINT =
                        'Export and retain the budget scope rows, then drain '
                        'or explicitly retire them before retrying; deleting '
                        'policy snapshots and reservations can let resumed '
                        'optional AI work exceed its original hard limits.';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM llm_calls
                WHERE usage_status = 'reported_partial'
                  AND estimated_cost_usd IS NOT NULL
                  AND input_tokens IS NULL
                  AND uncached_input_tokens IS NULL
                  AND output_tokens IS NULL
                  AND visible_output_tokens IS NULL
                  AND total_tokens IS NULL
                  AND cache_read_input_tokens IS NULL
                  AND cache_write_input_tokens IS NULL
                  AND reasoning_output_tokens IS NULL
                  AND tool_input_tokens IS NULL
                  AND provider_total_tokens IS NULL
                  AND provider_turn_count IS NULL
                  AND reported_cost_usd IS NULL
                LIMIT 1
            )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0037: LLM calls rely on estimated-cost usage shape'
                    USING HINT =
                        'Retain 0037 or explicitly settle/reclassify these '
                        'reported_partial rows before retrying; recreating '
                        'the 0036 usage constraint would otherwise fail.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_constraint(
        "ck_llm_calls_usage_status_shape",
        "llm_calls",
        type_="check",
    )
    op.create_check_constraint(
        "ck_llm_calls_usage_status_shape",
        "llm_calls",
        _USAGE_STATUS_SHAPE_0036,
    )
    op.drop_index(
        "ix_llm_calls_open_budget_started",
        table_name="llm_calls",
    )
    op.drop_index(
        "ix_llm_budget_scopes_deadline",
        table_name="llm_budget_scopes",
    )
    op.drop_index(
        "ix_llm_budget_scopes_analysis_run",
        table_name="llm_budget_scopes",
    )
    op.drop_index(
        "ix_llm_budget_scopes_project_started",
        table_name="llm_budget_scopes",
    )
    op.drop_table("llm_budget_scopes")
