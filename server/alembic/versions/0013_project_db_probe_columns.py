"""project_dbs: probe-result columns

Revision ID: 0013_project_db_probe_columns
Revises: 0012_diff_break_glass_grants
Create Date: 2026-05-12

Adds the two columns the read-only probe needs (spec §2.5, §2.8):

* ``last_probe_at`` — when we last successfully reasoned about the
  credential's privilege set.
* ``last_probe_result`` — full ProbeResult payload (`status`,
  per-schema grants, §2.3 certainty observations).

This migration is intentionally column-add only. The follow-up
``0014_project_db_disabled_at`` adds the ``disabled_at`` column on its
own so an alembic downgrade does not erase the operational decision
"this DB is unsafe" along with the schema bookkeeping.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_project_db_probe_columns"
down_revision: Union[str, None] = "0012_diff_break_glass_grants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "project_dbs",
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "project_dbs",
        sa.Column(
            "last_probe_result",
            postgresql.JSONB(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("project_dbs", "last_probe_result")
    op.drop_column("project_dbs", "last_probe_at")
