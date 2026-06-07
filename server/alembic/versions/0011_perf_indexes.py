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
_INDEXES: tuple[tuple[str, str], ...] = (
    # NB: idx_audit_project_time is created by 0003_audit_log (it owns the
    # audit_log indexes); re-declaring it here duplicated the name and broke
    # the downgrade cycle (0011 dropped it, then 0003's drop_index failed).
    # /audit also groups by actor for the abuse-detection query.
    (
        "idx_audit_actor",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_actor "
        "ON audit_log (actor, created_at DESC)",
    ),
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
    # Sample lookup "latest sample for entity".
    (
        "idx_samples_entity_time",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_samples_entity_time "
        "ON data_samples (project_id, data_entity_id, sampled_at DESC)",
    ),
    # analysis_runs dashboard sort.
    (
        "idx_runs_project_created",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_runs_project_created "
        "ON analysis_runs (project_id, created_at DESC)",
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
