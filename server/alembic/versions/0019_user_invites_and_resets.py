"""user_invites + password_reset_tokens (PR-44).

Revision ID: 0019_user_invites_and_resets
Revises: 0018_comments_and_assignee
Create Date: 2026-05-13

Two short-lived token tables backing the team-onboarding paths:

* ``user_invites`` — admin sends an invite link to an email; the
  recipient sets their own password during sign-up.
* ``password_reset_tokens`` — operator-driven password recovery
  without admin intervention.

Both tables follow the same pattern: hashed token at rest, single-
use (``consumed_at`` non-null kills further use), TTL via
``expires_at``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_user_invites_and_resets"
down_revision: Union[str, None] = "0018_comments_and_assignee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_invites",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="viewer"),
        sa.Column(
            "organization_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "invited_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "consumed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "consumed_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_user_invites_email", "user_invites", ["email"])

    op.create_table(
        "password_reset_tokens",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "consumed_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_index(
        "ix_password_reset_user", "password_reset_tokens", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_password_reset_user", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index("ix_user_invites_email", table_name="user_invites")
    op.drop_table("user_invites")
