"""plans: worktree_meta JSONB column

Revision ID: 0015_plan_worktree_meta
Revises: 0014_project_db_disabled_at
Create Date: 2026-05-12

Captures reproducibility hints for the worktree backing each plan —
base_sha (commit the AI starts from), head_sha (current head when the
worktree is materialised), and free-form keys for submodule pinning,
mirror URL, etc. JSONB rather than scalar columns so extending the
shape later doesn't require another migration (Team B critique #2 on
the 2nd-round design).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_plan_worktree_meta"
down_revision: Union[str, None] = "0014_project_db_disabled_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("worktree_meta", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plans", "worktree_meta")
