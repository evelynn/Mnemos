"""comments + plans.assignee_id + diff_submissions.assignee_id (PR-43)

Revision ID: 0018_comments_and_assignee
Revises: 0017_user_profile_columns
Create Date: 2026-05-13

Closes the 13th-round audit's C4 (no comments) and C5 (no
assignee). One table + two FK columns.

The ``comments`` table is polymorphic via ``target_kind``
(``plan`` or ``diff_submission``) + ``target_id``, keeping the
schema simple while letting future entities (findings, runs)
attach comments without another migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_comments_and_assignee"
down_revision: Union[str, None] = "0017_user_profile_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "comments",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column(
            "target_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "edited_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_comments_target", "comments", ["target_kind", "target_id"]
    )

    # Assignee on plans + diff_submissions.
    op.add_column(
        "plans",
        sa.Column(
            "assignee_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "diff_submissions",
        sa.Column(
            "assignee_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("diff_submissions", "assignee_id")
    op.drop_column("plans", "assignee_id")
    op.drop_index("ix_comments_target", table_name="comments")
    op.drop_table("comments")
