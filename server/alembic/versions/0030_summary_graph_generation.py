"""Pin current summaries to one atomically published source generation.

Revision ID: 0030_summary_graph_generation
Revises: 0029_analysis_run_lifecycle
Create Date: 2026-07-15

Legacy rows are intentionally left unvalidated. Existing duplicate
unsuperseded rows are first reconciled without deletion so the new writer can
always select one cache predecessor. A current consumer may show a Summary
only when its marker equals the ready GraphHead generation. Exact evidence
cache hits can revalidate an existing row without rewriting its original
analysis-run/content provenance.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_summary_graph_generation"
down_revision = "0029_analysis_run_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The legacy schema had no current-row uniqueness fence. Concurrent old
    # writers could therefore leave several unsuperseded rows and the new
    # cache lookup's scalar contract would fail before it had a chance to
    # repair them. Keep the newest deterministic representative and point all
    # additional physical narratives at it; no content or provenance is
    # deleted. Explicit marker-null historical rows are a new-code feature and
    # are created only after this migration.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                first_value(id) OVER (
                    PARTITION BY project_id, target_id, level
                    ORDER BY generated_at DESC, id DESC
                ) AS keeper_id,
                row_number() OVER (
                    PARTITION BY project_id, target_id, level
                    ORDER BY generated_at DESC, id DESC
                ) AS duplicate_rank
            FROM summaries
            WHERE superseded_by IS NULL
        )
        UPDATE summaries AS summary
           SET superseded_by = ranked.keeper_id
          FROM ranked
         WHERE summary.id = ranked.id
           AND ranked.duplicate_rank > 1
        """
    )
    op.add_column(
        "summaries",
        sa.Column("validated_graph_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "summaries",
        sa.Column("validated_overlay_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "summaries",
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_summaries_graph_validation_marker",
        "summaries",
        "(validated_graph_generation IS NULL AND "
        "validated_overlay_generation IS NULL AND validated_at IS NULL) OR "
        "(validated_graph_generation IS NOT NULL AND "
        "validated_overlay_generation IS NOT NULL AND "
        "validated_at IS NOT NULL AND validated_graph_generation > 0 AND "
        "validated_overlay_generation >= 0)",
    )
    op.create_index(
        "ix_summaries_current_graph_generation",
        "summaries",
        [
            "project_id",
            "validated_graph_generation",
            "validated_overlay_generation",
        ],
        unique=False,
        postgresql_where=sa.text("superseded_by IS NULL"),
    )
    op.create_index(
        "uq_summaries_current_validated_target",
        "summaries",
        ["project_id", "target_id", "level"],
        unique=True,
        postgresql_where=sa.text(
            "superseded_by IS NULL AND "
            "validated_graph_generation IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "superseded_by IS NULL AND "
            "validated_graph_generation IS NOT NULL"
        ),
    )


def downgrade() -> None:
    # Explicit historical L4 results may coexist with the validated current
    # row. The old reader uses scalar_one_or_none over unsuperseded
    # (project,target,level), so dropping the markers in that state turns a
    # valid database into a runtime MultipleResultsFound failure. Refuse the
    # lossy boundary and let the operator choose which row remains current.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM summaries
                WHERE superseded_by IS NULL
                GROUP BY project_id, target_id, level
                HAVING count(*) > 1
            )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0030: multiple unsuperseded summaries share a target'
                    USING HINT =
                        'Choose one current row per (project_id,target_id,level) '
                        'and supersede the others before retrying.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_index(
        "uq_summaries_current_validated_target",
        table_name="summaries",
        postgresql_where=sa.text(
            "superseded_by IS NULL AND "
            "validated_graph_generation IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "superseded_by IS NULL AND "
            "validated_graph_generation IS NOT NULL"
        ),
    )
    op.drop_index(
        "ix_summaries_current_graph_generation",
        table_name="summaries",
        postgresql_where=sa.text("superseded_by IS NULL"),
    )
    op.drop_constraint(
        "ck_summaries_graph_validation_marker",
        "summaries",
        type_="check",
    )
    op.drop_column("summaries", "validated_at")
    op.drop_column("summaries", "validated_overlay_generation")
    op.drop_column("summaries", "validated_graph_generation")
