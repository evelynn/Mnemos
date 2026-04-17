"""analysis_runs, nodes, edges, node_sources

Revision ID: 0004_graph
Revises: 0003_audit_log
Create Date: 2026-04-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_graph"
down_revision: Union[str, None] = "0003_audit_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
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
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("triggered_by", sa.String(), nullable=False),
        sa.Column("git_sha", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default="full"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", postgresql.JSONB(), nullable=True),
        sa.Column("error_log", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_analysis_runs_project_time",
        "analysis_runs",
        ["project_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "nodes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("certainty", sa.String(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.ARRAY(sa.String()),
            nullable=False,
        ),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", "project_id", "valid_from"),
    )
    op.execute(
        "CREATE INDEX idx_nodes_current ON nodes (project_id, id) WHERE valid_to IS NULL"
    )
    op.execute(
        "CREATE INDEX idx_nodes_kind_current ON nodes (project_id, kind) WHERE valid_to IS NULL"
    )
    op.execute("CREATE INDEX idx_nodes_data_gin ON nodes USING gin (data)")

    op.create_table(
        "edges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("certainty", sa.String(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.ARRAY(sa.String()),
            nullable=False,
        ),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", "valid_from"),
    )
    op.execute(
        "CREATE INDEX idx_edges_source_current ON edges (project_id, source_id) WHERE valid_to IS NULL"
    )
    op.execute(
        "CREATE INDEX idx_edges_target_current ON edges (project_id, target_id) WHERE valid_to IS NULL"
    )

    op.create_table(
        "node_sources",
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_name", sa.String(), nullable=False),
        sa.Column(
            "contributed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("raw_data", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint(
            "node_id", "project_id", "source_name", "contributed_at"
        ),
    )


def downgrade() -> None:
    op.drop_table("node_sources")
    op.execute("DROP INDEX IF EXISTS idx_edges_target_current")
    op.execute("DROP INDEX IF EXISTS idx_edges_source_current")
    op.drop_table("edges")
    op.execute("DROP INDEX IF EXISTS idx_nodes_data_gin")
    op.execute("DROP INDEX IF EXISTS idx_nodes_kind_current")
    op.execute("DROP INDEX IF EXISTS idx_nodes_current")
    op.drop_table("nodes")
    op.drop_index("idx_analysis_runs_project_time", table_name="analysis_runs")
    op.drop_table("analysis_runs")
