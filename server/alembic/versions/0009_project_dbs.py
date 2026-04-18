"""project_dbs for per-project DB bindings and policies

Revision ID: 0009_project_dbs
Revises: 0008_analysis_stages
Create Date: 2026-04-18

Spec: Mnemos_spec.md §12.2 — links a project to one or more databases
(MSSQL/Oracle) and carries the per-DB safety policy consulted by the
data sampler and the query landing zone.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_project_dbs"
down_revision: Union[str, None] = "0008_analysis_stages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_dbs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        # component_id matches the graph Node id convention 'db.<kind>.<name>'
        # so api/data.py can resolve policy by db_component_id alone.
        sa.Column("component_id", sa.String(), nullable=False),
        sa.Column(
            "secret_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secrets.id"),
            nullable=True,
        ),
        sa.Column(
            "allow_awr",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "sensitive_tables",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("ARRAY[]::text[]"),
        ),
        sa.Column(
            "masking_rules",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "maintenance_windows",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("ARRAY[]::text[]"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_project_dbs_project",
        "project_dbs",
        ["project_id"],
    )
    op.create_unique_constraint(
        "uq_project_dbs_component",
        "project_dbs",
        ["project_id", "component_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_project_dbs_component", "project_dbs", type_="unique")
    op.drop_index("idx_project_dbs_project", table_name="project_dbs")
    op.drop_table("project_dbs")
