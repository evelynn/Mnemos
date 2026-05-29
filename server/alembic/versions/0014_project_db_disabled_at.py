"""project_dbs: disabled_at column

Revision ID: 0014_project_db_disabled_at
Revises: 0013_project_db_probe_columns
Create Date: 2026-05-12

Adds ``disabled_at`` so the backfill probe (run separately by
``scripts/probe_existing_dbs.py``) can mark credentials whose read-only
status it cannot confirm without blocking the operator from inspecting
the binding. Kept in its own migration because a downgrade should not
discard the operational decision that a binding is unsafe.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_project_db_disabled_at"
down_revision: Union[str, None] = "0013_project_db_probe_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "project_dbs",
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_dbs", "disabled_at")
