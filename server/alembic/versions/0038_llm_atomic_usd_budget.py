"""Add atomic no-refund project-dollar reservations.

Revision ID: 0038_llm_atomic_usd_budget
Revises: 0037_llm_budget_scopes
Create Date: 2026-07-16

The project account is both the persisted monetary-policy snapshot and the
row-lock serialization point for concurrent first calls.  One physical call's
worst-case input/output reservation lives on ``llm_calls`` and is never
refunded after the STARTED transaction commits.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0038_llm_atomic_usd_budget"
down_revision = "0037_llm_budget_scopes"
branch_labels = None
depends_on = None


_TOKEN_VALUES_0037 = (
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

_TOKEN_VALUES_0038 = _TOKEN_VALUES_0037.replace(
    "(duration_ms IS NULL OR duration_ms >= 0) AND ",
    "(reserved_input_tokens IS NULL OR reserved_input_tokens >= 0) AND "
    "(reserved_output_tokens IS NULL OR reserved_output_tokens >= 0) AND "
    "(duration_ms IS NULL OR duration_ms >= 0) AND ",
)

_RESERVATION_SHAPE = (
    "(reserved_input_tokens IS NULL AND reserved_output_tokens IS NULL "
    "AND reservation_price_contract_id IS NULL "
    "AND reserved_cost_usd IS NULL) OR "
    "(reserved_input_tokens > 0 AND reserved_output_tokens > 0 "
    "AND reservation_price_contract_id IS NOT NULL "
    "AND length(reservation_price_contract_id) BETWEEN 1 AND 96 "
    "AND reserved_cost_usd > 0 "
    "AND estimated_input_tokens <= reserved_input_tokens "
    "AND requested_max_output_tokens = reserved_output_tokens)"
)


def upgrade() -> None:
    # ``reserved_cost_usd`` existed as an unused expansion column in 0036.
    # Refuse to reinterpret any out-of-band value without its input/output and
    # immutable-price provenance.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM llm_calls
                WHERE reserved_cost_usd IS NOT NULL
                LIMIT 1
            )
            THEN
                RAISE EXCEPTION
                    'cannot upgrade 0038: unproven reserved_cost_usd rows exist'
                    USING HINT =
                        'Reconcile each value with an immutable price contract '
                        'and explicit input/output ceilings before retrying.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.create_table(
        "llm_project_budget_accounts",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("cap_usd", sa.Numeric(20, 10), nullable=False),
        sa.Column("window_sec", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(policy_version) BETWEEN 1 AND 32",
            name="ck_llm_project_budget_accounts_policy_version",
        ),
        sa.CheckConstraint(
            "cap_usd > 0 AND window_sec >= 60",
            name="ck_llm_project_budget_accounts_policy",
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column("reserved_input_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "llm_calls",
        sa.Column("reserved_output_tokens", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "reservation_price_contract_id",
            sa.String(96),
            nullable=True,
        ),
    )
    op.drop_constraint("ck_llm_calls_token_values", "llm_calls", type_="check")
    op.create_check_constraint(
        "ck_llm_calls_token_values",
        "llm_calls",
        _TOKEN_VALUES_0038,
    )
    op.create_check_constraint(
        "ck_llm_calls_cost_reservation_shape",
        "llm_calls",
        _RESERVATION_SHAPE,
    )
    op.create_index(
        "ix_llm_calls_project_reserved_generated",
        "llm_calls",
        ["project_id", "generated_at"],
        unique=False,
        postgresql_where=sa.text("reserved_cost_usd IS NOT NULL"),
    )


def downgrade() -> None:
    # This guard must precede every destructive operation.  Removing either
    # the account lock or a no-refund reservation can authorize a later call
    # above the policy that originally governed the project.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (SELECT 1 FROM llm_project_budget_accounts LIMIT 1)
               OR EXISTS (
                    SELECT 1 FROM llm_calls
                    WHERE reserved_input_tokens IS NOT NULL
                       OR reserved_output_tokens IS NOT NULL
                       OR reservation_price_contract_id IS NOT NULL
                    LIMIT 1
               )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0038: atomic LLM dollar state exists'
                    USING HINT =
                        'Retain 0038 or export and explicitly retire every '
                        'project policy and no-refund reservation first.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_index(
        "ix_llm_calls_project_reserved_generated",
        table_name="llm_calls",
    )
    op.drop_constraint(
        "ck_llm_calls_cost_reservation_shape",
        "llm_calls",
        type_="check",
    )
    op.drop_constraint("ck_llm_calls_token_values", "llm_calls", type_="check")
    op.create_check_constraint(
        "ck_llm_calls_token_values",
        "llm_calls",
        _TOKEN_VALUES_0037,
    )
    op.drop_column("llm_calls", "reservation_price_contract_id")
    op.drop_column("llm_calls", "reserved_output_tokens")
    op.drop_column("llm_calls", "reserved_input_tokens")
    op.drop_table("llm_project_budget_accounts")
