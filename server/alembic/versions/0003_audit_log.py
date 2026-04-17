"""audit_log table

Revision ID: 0003_audit_log
Revises: 0002_projects
Create Date: 2026-04-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_audit_log"
down_revision: Union[str, None] = "0002_projects"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_audit_project_time",
        "audit_log",
        ["project_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "idx_audit_actor_time",
        "audit_log",
        ["actor", sa.text("occurred_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_audit_actor_time", table_name="audit_log")
    op.drop_index("idx_audit_project_time", table_name="audit_log")
    op.drop_table("audit_log")
