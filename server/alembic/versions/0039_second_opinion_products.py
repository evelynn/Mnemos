"""Persist canonical, revision-bound Gate-B second-opinion products.

Revision ID: 0039_second_opinion_products
Revises: 0038_llm_atomic_usd_budget
Create Date: 2026-07-16

The stable operation UUID is the primary key and the optional physical-attempt
foreign key is unique.  A successful provider dispatch can therefore produce
at most one sanitized product, while a no-provider empty-diff review can still
persist an explicit canonical result.  Prompts, raw responses, exceptions, and
diff text have no columns in this table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0039_second_opinion_products"
down_revision = "0038_llm_atomic_usd_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "second_opinion_products",
        sa.Column(
            "operation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "attempt_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("contract_name", sa.String(64), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("diff_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
        sa.Column("coverage", sa.String(16), nullable=False),
        sa.Column("input_coverage", sa.String(16), nullable=False),
        sa.Column("grounding_status", sa.String(24), nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("finding_count", sa.BigInteger(), nullable=False),
        sa.Column("evidence_line_count", sa.BigInteger(), nullable=False),
        sa.Column("rejected_findings", sa.BigInteger(), nullable=False),
        sa.Column(
            "review_run_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "review_source_generation", sa.BigInteger(), nullable=False
        ),
        sa.Column(
            "review_overlay_generation", sa.BigInteger(), nullable=False
        ),
        sa.Column("review_git_sha", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plans.id"],
            name="fk_second_opinion_products_plan",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["llm_calls.id"],
            name="fk_second_opinion_products_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_run_id", "project_id"],
            ["analysis_runs.id", "analysis_runs.project_id"],
            name="fk_second_opinion_product_review_run_project",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "attempt_id", name="uq_second_opinion_products_attempt"
        ),
        sa.CheckConstraint(
            "contract_name = 'mnemos.second_opinion.v1'",
            name="ck_second_opinion_products_contract",
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64 AND length(diff_sha256) = 64",
            name="ck_second_opinion_products_fingerprints",
        ),
        sa.CheckConstraint(
            "coverage IN ('complete', 'incomplete') AND "
            "input_coverage IN ('complete', 'incomplete') AND "
            "grounding_status IN ('fully_grounded', 'rejected')",
            name="ck_second_opinion_products_status_values",
        ),
        sa.CheckConstraint(
            "(coverage = 'complete' AND failure_code IS NULL "
            "AND input_coverage = 'complete' "
            "AND grounding_status = 'fully_grounded' "
            "AND rejected_findings = 0) OR "
            "(coverage = 'incomplete' AND failure_code IS NOT NULL "
            "AND length(failure_code) BETWEEN 1 AND 64)",
            name="ck_second_opinion_products_coverage_shape",
        ),
        sa.CheckConstraint(
            "finding_count >= 0 AND finding_count <= 32 AND "
            "evidence_line_count >= 0 AND evidence_line_count <= 128 AND "
            "rejected_findings >= 0 AND rejected_findings <= 32",
            name="ck_second_opinion_products_counts",
        ),
        sa.CheckConstraint(
            "review_source_generation > 0 AND "
            "review_overlay_generation >= 0 AND "
            "length(review_git_sha) BETWEEN 40 AND 64",
            name="ck_second_opinion_products_review_revision",
        ),
    )
    op.create_index(
        "ix_second_opinion_products_plan_revision",
        "second_opinion_products",
        [
            "plan_id",
            "review_run_id",
            "review_source_generation",
            "review_overlay_generation",
        ],
        unique=False,
    )


def downgrade() -> None:
    # A canonical product has no pre-0039 home.  Deleting it would convert a
    # safely replayable operation into a terminal-attempt-without-product gap.
    # The guard must therefore run before every destructive operation.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (SELECT 1 FROM second_opinion_products LIMIT 1)
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0039: canonical second-opinion products exist'
                    USING HINT =
                        'Retain 0039 or export and explicitly retire every '
                        'revision-bound Gate-B product before retrying; loss '
                        'would make safe replay impossible.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_index(
        "ix_second_opinion_products_plan_revision",
        table_name="second_opinion_products",
    )
    op.drop_table("second_opinion_products")
