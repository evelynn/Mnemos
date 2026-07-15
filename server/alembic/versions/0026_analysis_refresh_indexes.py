"""Refresh indexes and a physical-call ledger for optional LLM work.

Revision ID: 0026_analysis_refresh_indexes
Revises: 0025_graph_indexes
Create Date: 2026-07-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_analysis_refresh_indexes"
down_revision = "0025_graph_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "summaries",
        sa.Column("evidence_hash", sa.String(length=64), nullable=True),
    )
    # Older runners stored this cache digest as a synthetic claim.  Preserve
    # skip behaviour during upgrade; read paths filter the legacy control
    # claim so it can no longer be presented as graph grounding.
    op.execute(
        """
        UPDATE summaries
        SET evidence_hash = (
            SELECT item->'evidence'->0->>'node_id'
            FROM jsonb_array_elements(COALESCE(claims, '[]'::jsonb)) AS item
            WHERE item->>'claim' = '_evidence_hash'
            LIMIT 1
        )
        WHERE evidence_hash IS NULL
          AND EXISTS (
            SELECT 1
            FROM jsonb_array_elements(COALESCE(claims, '[]'::jsonb)) AS item
            WHERE item->>'claim' = '_evidence_hash'
          )
        """
    )
    op.create_index(
        "ix_summaries_project_generated",
        "summaries",
        ["project_id", "generated_at"],
    )
    op.create_index(
        "ix_analysis_runs_project_status_completed",
        "analysis_runs",
        ["project_id", "status", "completed_at"],
    )
    op.create_table(
        "llm_calls",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("model_used", sa.String(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("fallback_reason", sa.String(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_llm_calls_project_generated",
        "llm_calls",
        ["project_id", "generated_at"],
    )
    op.create_index(
        "ix_llm_calls_analysis_run", "llm_calls", ["analysis_run_id"]
    )
    # Preserve the active budget window across upgrade.  Summary ids are UUIDs
    # and make stable ledger ids; future calls are written directly.
    op.execute(
        """
        INSERT INTO llm_calls
            (id, project_id, analysis_run_id, target_id, level, model_used,
             tokens_used, status, fallback_reason, generated_at)
        SELECT id, project_id, analysis_run_id, target_id, level, model_used,
               tokens_used, 'legacy_summary', fallback_reason, generated_at
        FROM summaries
        WHERE model_used NOT LIKE 'stub%' OR COALESCE(tokens_used, 0) > 0
        """
    )


def downgrade() -> None:
    op.drop_index("ix_llm_calls_analysis_run", table_name="llm_calls")
    op.drop_index("ix_llm_calls_project_generated", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_index(
        "ix_analysis_runs_project_status_completed", table_name="analysis_runs"
    )
    op.drop_index("ix_summaries_project_generated", table_name="summaries")
    op.drop_column("summaries", "evidence_hash")
