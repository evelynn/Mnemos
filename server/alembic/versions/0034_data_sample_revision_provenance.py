"""Bind DataSample decisions to one canonical graph and masking policy.

Revision ID: 0034_data_sample_revision
Revises: 0033_review_revision_provenance
Create Date: 2026-07-15

Legacy samples cannot prove which DataEntity sensitivity/masking policy or
ProjectDB binding authorised them. They remain nullable and current readers
hide them until a fresh sample is captured under a validated publication and
the exact current effective policy.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034_data_sample_revision"
down_revision = "0033_review_revision_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_samples",
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "data_samples",
        sa.Column("source_git_sha", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "data_samples",
        sa.Column("source_graph_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "data_samples",
        sa.Column("source_overlay_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "data_samples",
        sa.Column("source_project_db_present", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "data_samples",
        sa.Column(
            "source_project_db_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "data_samples",
        sa.Column("source_policy_hash", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_data_samples_source_run_project",
        "data_samples",
        "analysis_runs",
        ["source_run_id", "project_id"],
        ["id", "project_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_data_samples_source_revision_shape",
        "data_samples",
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
        "source_graph_generation > 0 AND source_overlay_generation >= 0 AND "
        "((source_project_db_present = true AND "
        "source_project_db_id IS NOT NULL) OR "
        "(source_project_db_present = false AND "
        "source_project_db_id IS NULL)))",
    )
    op.create_index(
        "ix_data_samples_current_revision",
        "data_samples",
        [
            "project_id",
            "data_entity_id",
            "source_run_id",
            "source_graph_generation",
            "source_overlay_generation",
            "source_policy_hash",
            "sampled_at",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM data_samples
                WHERE source_run_id IS NOT NULL
                   OR source_git_sha IS NOT NULL
                   OR source_graph_generation IS NOT NULL
                   OR source_overlay_generation IS NOT NULL
                   OR source_project_db_present IS NOT NULL
                   OR source_project_db_id IS NOT NULL
                   OR source_policy_hash IS NOT NULL
            )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0034: revision-bound data samples exist'
                    USING HINT =
                        'Restore a pre-0034 backup, or export and remove the '
                        'bound samples before retrying; never expose them '
                        'without their policy provenance.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_index(
        "ix_data_samples_current_revision", table_name="data_samples"
    )
    op.drop_constraint(
        "ck_data_samples_source_revision_shape",
        "data_samples",
        type_="check",
    )
    op.drop_constraint(
        "fk_data_samples_source_run_project",
        "data_samples",
        type_="foreignkey",
    )
    op.drop_column("data_samples", "source_policy_hash")
    op.drop_column("data_samples", "source_project_db_id")
    op.drop_column("data_samples", "source_project_db_present")
    op.drop_column("data_samples", "source_overlay_generation")
    op.drop_column("data_samples", "source_graph_generation")
    op.drop_column("data_samples", "source_git_sha")
    op.drop_column("data_samples", "source_run_id")
