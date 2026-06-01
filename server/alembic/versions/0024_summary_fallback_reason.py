"""Summary.fallback_reason — operator-visible "why this is a stub"

Revision ID: 0024_summary_fallback_reason
Revises: 0023_fk_ondelete
Create Date: 2026-05-31

PR-138 added ``ExtractorResult.fallback_reason`` (no_backend /
anthropic_http_error / agent_sdk_timeout / budget_exceeded / …) so an
operator inspecting a stub summary can tell apart the failure modes.
PR-138b makes that reason persistent — without the column, the
information lives only in the encoded ``model_used`` string and the
Prometheus counter. The dashboard's planned "Why did this miss the
LLM?" panel needs a structured field, not a parsed substring.

Nullable + no backfill — pre-PR-138b rows that already carry
``model_used="stub"`` simply leave the column NULL; the dashboard
maps that to "unspecified".
"""

from alembic import op
import sqlalchemy as sa

revision = "0024_summary_fallback_reason"
down_revision = "0023_fk_ondelete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "summaries",
        sa.Column("fallback_reason", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("summaries", "fallback_reason")
