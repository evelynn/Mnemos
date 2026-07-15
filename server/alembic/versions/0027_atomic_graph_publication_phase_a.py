"""Run-isolated graph staging and atomic publication head.

Revision ID: 0027_atomic_graph_publication
Revises: 0026_analysis_refresh_indexes
Create Date: 2026-07-15

This migration installs the persistence contract used by the Phase-B
``run_ingest`` writer. No existing graph is silently blessed as atomic: every
existing project starts in ``needs_rebuild`` and becomes ``ready`` only after
a successful run-scoped staged rebuild and promotion. Deployments must drain
legacy workers before upgrading so old live writers cannot race this contract.

The stage tables hold one materialized payload per logical identity and a
sorted owner set for identical cross-producer emissions. Conflicting producer
payloads fail closed; replacement of published owner data still requires
sealed complete coverage.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0027_atomic_graph_publication"
down_revision = "0026_analysis_refresh_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analysis_runs",
        sa.Column("graph_base_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "analysis_runs",
        sa.Column(
            "graph_authoritative_sources",
            postgresql.ARRAY(sa.String()),
            nullable=True,
        ),
    )
    op.add_column(
        "analysis_runs",
        sa.Column(
            "graph_coverage_sealed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_analysis_runs_graph_base_generation",
        "analysis_runs",
        "graph_base_generation IS NULL OR graph_base_generation >= 0",
    )
    op.create_unique_constraint(
        "uq_analysis_runs_id_project",
        "analysis_runs",
        ["id", "project_id"],
    )
    # A publication transaction closes the old version before inserting the
    # successor. These partial indexes make "one current logical identity" a
    # database invariant instead of an application convention. Existing
    # duplicate-current legacy rows deliberately make migration fail closed.
    op.create_index(
        "uq_nodes_current_identity",
        "nodes",
        ["project_id", "id"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index(
        "uq_edges_current_identity",
        "edges",
        ["project_id", "source_id", "target_id", "kind"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )

    op.create_table(
        "graph_heads",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "current_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["current_run_id", "project_id"],
            ["analysis_runs.id", "analysis_runs.project_id"],
            ondelete="RESTRICT",
            name="fk_graph_heads_current_run_project",
        ),
        sa.Column(
            "state",
            sa.String(),
            server_default=sa.text("'needs_rebuild'"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('needs_rebuild', 'ready')",
            name="ck_graph_heads_state",
        ),
        sa.CheckConstraint(
            "(state = 'needs_rebuild' AND current_run_id IS NULL "
            "AND generation = 0 AND published_at IS NULL) OR "
            "(state = 'ready' AND current_run_id IS NOT NULL "
            "AND generation > 0 AND published_at IS NOT NULL)",
            name="ck_graph_heads_shape",
        ),
    )
    op.create_index(
        "ix_graph_heads_current_run", "graph_heads", ["current_run_id"]
    )

    op.create_table(
        "graph_node_stage",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("node_id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["analysis_runs.id", "analysis_runs.project_id"],
            ondelete="CASCADE",
            name="fk_graph_node_stage_run_project",
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("certainty", sa.String(), nullable=False),
        sa.Column("source_name", sa.String(), nullable=False),
        sa.Column("source_names", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "staged_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_graph_node_stage_run_source",
        "graph_node_stage",
        ["run_id", "source_name"],
    )
    op.create_index(
        "ix_graph_node_stage_project_run",
        "graph_node_stage",
        ["project_id", "run_id"],
    )

    op.create_table(
        "graph_edge_stage",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("source_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("target_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["analysis_runs.id", "analysis_runs.project_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_stage_run_project",
        ),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("certainty", sa.String(), nullable=False),
        sa.Column("source_name", sa.String(), nullable=False),
        sa.Column("source_names", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "staged_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_graph_edge_stage_run_source",
        "graph_edge_stage",
        ["run_id", "source_name"],
    )
    op.create_index(
        "ix_graph_edge_stage_project_run",
        "graph_edge_stage",
        ["project_id", "run_id"],
    )

    # A legacy current graph may already contain rows committed by a failed
    # run.  Timestamp heuristics cannot prove otherwise, so bootstrap is
    # intentionally fail-closed. The new worker must run a staged full rebuild.
    op.execute(
        """
        INSERT INTO graph_heads
            (project_id, current_run_id, generation, state, published_at, updated_at)
        SELECT id, NULL, 0, 'needs_rebuild', NULL, CURRENT_TIMESTAMP
        FROM projects
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_graph_edge_stage_project_run", table_name="graph_edge_stage"
    )
    op.drop_index(
        "ix_graph_edge_stage_run_source", table_name="graph_edge_stage"
    )
    op.drop_table("graph_edge_stage")
    op.drop_index(
        "ix_graph_node_stage_project_run", table_name="graph_node_stage"
    )
    op.drop_index(
        "ix_graph_node_stage_run_source", table_name="graph_node_stage"
    )
    op.drop_table("graph_node_stage")
    op.drop_index("ix_graph_heads_current_run", table_name="graph_heads")
    op.drop_table("graph_heads")
    op.drop_index("uq_edges_current_identity", table_name="edges")
    op.drop_index("uq_nodes_current_identity", table_name="nodes")
    op.drop_constraint(
        "uq_analysis_runs_id_project",
        "analysis_runs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_analysis_runs_graph_base_generation",
        "analysis_runs",
        type_="check",
    )
    op.drop_column("analysis_runs", "graph_coverage_sealed_at")
    op.drop_column("analysis_runs", "graph_authoritative_sources")
    op.drop_column("analysis_runs", "graph_base_generation")
