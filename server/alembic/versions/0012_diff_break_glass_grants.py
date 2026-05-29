"""diff break-glass grants — replaces the override=true shortcut

Revision ID: 0012_diff_break_glass_grants
Revises: 0011_perf_indexes
Create Date: 2026-05-12

Spec §2.5: "운영 시스템은 신성하다 — 이 가드를 끌 설정 스위치는 만들지
않음." The previous Gate B veto could be bypassed by passing
``override=true`` + a 20-char rationale on the approve endpoint, with no
re-run of ultrareview and no second pair of eyes. This migration adds
the table that backs the new break-glass workflow:

1. An admin POSTs `/diff_submissions/{id}/break_glass_grant` with a
   rationale ≥200 chars. The endpoint re-runs `safety.review.run_pipeline`
   on the current diff. If the verdict is still ``blocked`` the grant is
   refused.
2. On success a row is inserted with a 15-minute TTL and a sha256 of the
   token (the raw token is only ever returned in the response body).
3. The approve endpoint consumes the grant atomically with a single
   ``UPDATE ... WHERE consumed_at IS NULL AND expires_at > now()
   AND issued_by <> $approver RETURNING id`` — this enforces 2-eyes
   (initiator ≠ approver) and single-use semantics without an explicit
   row lock.

Online, add-only migration; no backfill required.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_diff_break_glass_grants"
down_revision: Union[str, None] = "0011_perf_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "diff_break_glass_grants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("diff_submissions.id"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        sa.Column("issued_by", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("rerun_review_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.String(), nullable=True),
    )
    op.create_index(
        "idx_break_glass_submission_active",
        "diff_break_glass_grants",
        ["submission_id", "consumed_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_break_glass_submission_active",
        table_name="diff_break_glass_grants",
    )
    op.drop_table("diff_break_glass_grants")
