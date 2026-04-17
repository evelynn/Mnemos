"""plans + diff_submissions

Revision ID: 0007_plans
Revises: 0006_findings_summaries
Create Date: 2026-04-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_plans"
down_revision: Union[str, None] = "0006_findings_summaries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="pending_approval"),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("tasks", postgresql.JSONB(), nullable=False),
        sa.Column("impact_report", postgresql.JSONB(), nullable=True),
        sa.Column("requester", sa.String(), nullable=False),
        sa.Column("worktree_path", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
    )

    op.create_table(
        "diff_submissions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plans.id"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending_approval"),
        sa.Column("diff", sa.Text(), nullable=False),
        sa.Column("test_results", postgresql.JSONB(), nullable=True),
        sa.Column("self_review_notes", sa.Text(), nullable=True),
        sa.Column("auto_review_findings", postgresql.JSONB(), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("gitlab_mr_iid", sa.Integer(), nullable=True),
        sa.Column("gitlab_mr_url", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("diff_submissions")
    op.drop_table("plans")
