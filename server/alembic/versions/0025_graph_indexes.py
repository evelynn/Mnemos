"""Graph perf indexes — fix O(N^2) edge supersede on large analyses

Revision ID: 0025_graph_indexes
Revises: 0024_summary_fallback_reason
Create Date: 2026-06-17

PR-183 — ``upsert_edge`` supersedes the current edge by
``(project_id, source_id, target_id, kind, valid_to)``. With no index this
full-scans the edges table on every insert → O(N^2) on the calls stage of a
large analysis (observed: a 40k+ edge repo stalled for 30+ minutes). The
busiest read paths have the same gap: ``find_callers`` / in-degree ranking
(by target), ``find_callees`` / neighbours (by source), and kind-filtered
node counts (overview / priority ranking). These indexes turn the scans into
seeks.

Mirrors the ``Index()`` definitions now on the Node/Edge models so a
``create_all`` (docker-free SQLite) and an alembic-migrated Postgres end up
with the same schema.
"""

from alembic import op

revision = "0025_graph_indexes"
down_revision = "0024_summary_fallback_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_edges_identity", "edges",
        ["project_id", "source_id", "target_id", "kind", "valid_to"],
    )
    op.create_index(
        "ix_edges_target", "edges",
        ["project_id", "target_id", "kind", "valid_to"],
    )
    op.create_index(
        "ix_edges_source", "edges",
        ["project_id", "source_id", "valid_to"],
    )
    op.create_index(
        "ix_nodes_project_kind", "nodes",
        ["project_id", "kind", "valid_to"],
    )


def downgrade() -> None:
    op.drop_index("ix_nodes_project_kind", table_name="nodes")
    op.drop_index("ix_edges_source", table_name="edges")
    op.drop_index("ix_edges_target", table_name="edges")
    op.drop_index("ix_edges_identity", table_name="edges")
