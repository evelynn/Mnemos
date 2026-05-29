"""runtime_observations table (OTLP Tier 2 — P2-1 / P2-2).

Revision ID: 0016_runtime_observations
Revises: 0015_plan_worktree_meta
Create Date: 2026-05-12

The OTLP receiver scrubs incoming spans but Phase 1 had nowhere to
land the resulting `(service, operation)` observations beyond a noop
audit-log entry. Tier 2 of the merge pipeline (spec §7.6) needs to:

* Mark an existing graph edge as ``exercised: true`` when a runtime
  trace confirms a static contract was hit.
* Buffer spans whose caller/callee don't resolve to known
  components yet — the next analysis run replays them through the
  merge stage so a freshly discovered EXPOSES edge picks up its
  exercised-flag retroactively.

Team B's 5th-round critique #2 demanded:

* ``organization_id`` NOT NULL so multi-tenant isolation isn't a
  client-side concern.
* ``project_id`` nullable until reconciliation — early-arrival spans
  often belong to a project that isn't registered yet.
* UNIQUE ``(organization_id, service, operation)`` so the upsert
  path can use ``ON CONFLICT … DO UPDATE`` for the seen_count bump.
* 14-day retention swept by the existing ``retention_purge`` cron.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_runtime_observations"
down_revision: Union[str, None] = "0015_plan_worktree_meta"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_observations",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "organization_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="CALLS"),
        sa.Column(
            "seen_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "organization_id", "service", "operation", "kind",
            name="uq_runtime_obs_org_svc_op_kind",
        ),
    )
    op.create_index(
        "ix_runtime_obs_last_seen",
        "runtime_observations",
        ["last_seen_at"],
    )
    op.create_index(
        "ix_runtime_obs_project",
        "runtime_observations",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_obs_project", table_name="runtime_observations")
    op.drop_index("ix_runtime_obs_last_seen", table_name="runtime_observations")
    op.drop_table("runtime_observations")
