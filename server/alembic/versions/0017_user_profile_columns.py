"""users: profile + lifecycle columns (PR-38)

Revision ID: 0017_user_profile_columns
Revises: 0016_runtime_observations
Create Date: 2026-05-13

The platform graduated from single-operator self-host to a team-
operated product. The bare-minimum columns the team workflow needs:

* ``email``         — optional contact, used by the invite + reset flows.
* ``display_name``  — what shows next to a user's audit-log entries
                      and (future) comments; falls back to username.
* ``avatar_url``    — cosmetic; empty string is fine.
* ``timezone``      — IANA TZ for per-user timestamp display; UTC
                      stays the storage default.
* ``disabled_at``   — soft-delete + force-logout flag; the login
                      handler refuses any account whose value is non-NULL.
* ``last_login_at`` — for the admin-side "active sessions" view.
* ``updated_at``    — generic audit column; rows touched by the
                      profile / role-change endpoints bump this.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_user_profile_columns"
down_revision: Union[str, None] = "0016_runtime_observations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.String(), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(), nullable=True))
    op.add_column("users", sa.Column("timezone", sa.String(), nullable=True))
    op.add_column(
        "users", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index(
        "ix_users_disabled_at", "users", ["disabled_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_users_disabled_at", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "updated_at")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "disabled_at")
    op.drop_column("users", "timezone")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "display_name")
    op.drop_column("users", "email")
