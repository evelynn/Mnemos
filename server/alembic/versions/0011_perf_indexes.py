"""performance indexes for hot query paths

Revision ID: 0011_perf_indexes
Revises: 0010_organizations
Create Date: 2026-04-18

Added in response to the Phase-B load-test checklist. Every index here
targets a query that shows up either in the API access path (sample/list
endpoints) or the worker ingestion path. None of them are strictly
correctness-critical, so this migration is safe to run online and safe
to drop again if profiling changes the picture.

Uses ``CREATE INDEX CONCURRENTLY`` so large existing tables stay online
during the build. Note that CONCURRENTLY cannot run inside a migration
transaction — alembic default config already sets
``transactional_ddl = False`` for Postgres + ``asyncpg``.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011_perf_indexes"
down_revision: Union[str, None] = "0010_organizations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Each entry: (index_name, DDL). Kept as tuples so downgrade() can mirror.
#
# Only genuinely-new indexes live here. Three candidates from the original
# load-test checklist were dropped because earlier migrations already cover
# them and re-declaring them was either wrong or pure duplication:
#   * audit_log: 0003 already creates idx_audit_actor_time on
#     (actor, occurred_at DESC). The column is occurred_at, NOT created_at,
#     so the old idx_audit_actor DDL failed at CREATE time with
#     "column created_at does not exist" and aborted the whole migration.
#   * data_samples: 0005 already creates idx_samples_entity on
#     (project_id, data_entity_id, sampled_at DESC).
#   * analysis_runs: 0004 already creates idx_analysis_runs_project_time on
#     (project_id, created_at DESC).
_INDEXES: tuple[tuple[str, str], ...] = (
    # data_query_log: operator "recent queries by project" view.
    (
        "idx_data_query_log_project_time",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_data_query_log_project_time "
        "ON data_query_log (project_id, executed_at DESC)",
    ),
    # Finding list filters by project + status; severity used for ranking.
    (
        "idx_findings_project_status",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_findings_project_status "
        "ON findings (project_id, status, severity)",
    ),
)


def upgrade() -> None:
    # Use autocommit so CONCURRENTLY is allowed.
    conn = op.get_bind()
    for _, ddl in _INDEXES:
        conn.exec_driver_sql("COMMIT")
        conn.exec_driver_sql(ddl)
        conn.exec_driver_sql("BEGIN")


def downgrade() -> None:
    conn = op.get_bind()
    for name, _ in _INDEXES:
        conn.exec_driver_sql("COMMIT")
        conn.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        conn.exec_driver_sql("BEGIN")
