"""Bind development Plans to one canonical published source revision.

Revision ID: 0032_plan_revision_provenance
Revises: 0031_finding_graph_currentness
Create Date: 2026-07-15

Legacy Plans cannot be backfilled safely: their historical worktrees may have
been created from a mutable mirror HEAD.  They remain nullable and application
boundaries reject them until an operator creates a new Plan from a validated
publication.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_plan_revision_provenance"
down_revision = "0031_finding_graph_currentness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column(
            "source_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "plans",
        sa.Column("source_git_sha", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "plans",
        sa.Column("source_graph_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "plans",
        sa.Column("source_overlay_generation", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_plans_source_run_project",
        "plans",
        "analysis_runs",
        ["source_run_id", "project_id"],
        ["id", "project_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_plans_source_revision_shape",
        "plans",
        "(source_run_id IS NULL AND source_git_sha IS NULL AND "
        "source_graph_generation IS NULL AND "
        "source_overlay_generation IS NULL) OR "
        "(source_run_id IS NOT NULL AND source_git_sha IS NOT NULL AND "
        "source_graph_generation IS NOT NULL AND "
        "source_overlay_generation IS NOT NULL AND "
        "length(source_git_sha) BETWEEN 40 AND 64 AND "
        "source_graph_generation > 0 AND source_overlay_generation >= 0)",
    )
    op.create_index(
        "ix_plans_project_source_revision",
        "plans",
        [
            "project_id",
            "source_run_id",
            "source_graph_generation",
            "source_overlay_generation",
        ],
        unique=False,
    )


def downgrade() -> None:
    # Pre-0032 code approves and edits a Plan without checking which graph or
    # source commit the operator reviewed. Refuse to erase the proof while any
    # bound Plan exists; backup restore is the supported rollback for live data.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM plans
                WHERE source_run_id IS NOT NULL
                   OR source_git_sha IS NOT NULL
                   OR source_graph_generation IS NOT NULL
                   OR source_overlay_generation IS NOT NULL
            )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0032: revision-bound plans exist'
                    USING HINT =
                        'Export and remove revision-bound Plans or restore a '
                        'pre-0032 backup; never discard approval provenance.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_index("ix_plans_project_source_revision", table_name="plans")
    op.drop_constraint(
        "ck_plans_source_revision_shape",
        "plans",
        type_="check",
    )
    op.drop_constraint(
        "fk_plans_source_run_project",
        "plans",
        type_="foreignkey",
    )
    op.drop_column("plans", "source_overlay_generation")
    op.drop_column("plans", "source_graph_generation")
    op.drop_column("plans", "source_git_sha")
    op.drop_column("plans", "source_run_id")
