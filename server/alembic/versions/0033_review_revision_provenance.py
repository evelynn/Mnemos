"""Bind Gate-B verdicts and break-glass grants to one graph revision.

Revision ID: 0033_review_revision_provenance
Revises: 0032_plan_revision_provenance
Create Date: 2026-07-15

Legacy rows stay nullable so the migration never fabricates provenance.  The
approval boundary treats that NULL shape as stale and requires a fresh review.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033_review_revision_provenance"
down_revision = "0032_plan_revision_provenance"
branch_labels = None
depends_on = None


_REVIEW_COLUMNS = (
    ("review_run_id", postgresql.UUID(as_uuid=True), None),
    ("review_source_generation", sa.BigInteger(), None),
    ("review_overlay_generation", sa.BigInteger(), None),
    ("review_git_sha", sa.String(length=64), None),
)


def _add_review_revision(table: str, constraint_name: str) -> None:
    for name, type_, default in _REVIEW_COLUMNS:
        op.add_column(
            table,
            sa.Column(name, type_, nullable=True, server_default=default),
        )
    op.create_check_constraint(
        constraint_name,
        table,
        "(review_run_id IS NULL AND review_source_generation IS NULL "
        "AND review_overlay_generation IS NULL AND review_git_sha IS NULL) "
        "OR (review_run_id IS NOT NULL AND review_source_generation IS NOT NULL "
        "AND review_overlay_generation IS NOT NULL AND review_git_sha IS NOT NULL "
        "AND review_source_generation > 0 AND review_overlay_generation >= 0 "
        "AND length(review_git_sha) BETWEEN 40 AND 64)",
    )


def upgrade() -> None:
    _add_review_revision(
        "diff_submissions",
        "ck_diff_submissions_review_revision",
    )
    op.create_foreign_key(
        "fk_diff_submissions_review_run",
        "diff_submissions",
        "analysis_runs",
        ["review_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_diff_submissions_review_revision",
        "diff_submissions",
        [
            "review_run_id",
            "review_source_generation",
            "review_overlay_generation",
        ],
        unique=False,
    )
    _add_review_revision(
        "diff_break_glass_grants",
        "ck_diff_break_glass_review_revision",
    )
    op.create_foreign_key(
        "fk_diff_break_glass_review_run",
        "diff_break_glass_grants",
        "analysis_runs",
        ["review_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # Removing these columns would make an audited approval indistinguishable
    # from a legacy unbound verdict. Refuse lossy rollback; backup restore is
    # the supported rollback once revision-bound decisions exist.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM diff_submissions
                WHERE review_run_id IS NOT NULL
                   OR review_source_generation IS NOT NULL
                   OR review_overlay_generation IS NOT NULL
                   OR review_git_sha IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM diff_break_glass_grants
                WHERE review_run_id IS NOT NULL
                   OR review_source_generation IS NOT NULL
                   OR review_overlay_generation IS NOT NULL
                   OR review_git_sha IS NOT NULL
            )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0033: Gate-B review provenance exists'
                    USING HINT =
                        'Restore a pre-0033 backup, or export and explicitly '
                        'dispose of all revision-bound decisions before retrying.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_constraint(
        "fk_diff_break_glass_review_run",
        "diff_break_glass_grants",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_diff_break_glass_review_revision",
        "diff_break_glass_grants",
        type_="check",
    )
    for name, _type, _default in reversed(_REVIEW_COLUMNS):
        op.drop_column("diff_break_glass_grants", name)

    op.drop_index(
        "ix_diff_submissions_review_revision",
        table_name="diff_submissions",
    )
    op.drop_constraint(
        "fk_diff_submissions_review_run",
        "diff_submissions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_diff_submissions_review_revision",
        "diff_submissions",
        type_="check",
    )
    for name, _type, _default in reversed(_REVIEW_COLUMNS):
        op.drop_column("diff_submissions", name)
