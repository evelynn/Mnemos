"""organizations + org-scoped bindings

Revision ID: 0010_organizations
Revises: 0009_project_dbs
Create Date: 2026-04-18

Introduces the ``organizations`` table and adds an ``organization_id``
column to ``projects`` and ``users``. Existing rows are moved into a
single "default" organization to preserve backward compatibility with
single-tenant deployments — multi-org isolation is opt-in until an
operator starts creating additional organizations.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_organizations"
down_revision: Union[str, None] = "0009_project_dbs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )

    # Backfill: one "default" org to host everything that predates
    # multi-tenancy. New deployments use this too until the operator
    # creates additional orgs.
    op.execute(
        "INSERT INTO organizations (slug, display_name) "
        "VALUES ('default', 'Default organization')"
    )

    op.add_column(
        "projects",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=True,
        ),
    )

    # Backfill existing rows with the default org id.
    op.execute(
        "UPDATE projects SET organization_id = "
        "(SELECT id FROM organizations WHERE slug='default')"
    )
    op.execute(
        "UPDATE users SET organization_id = "
        "(SELECT id FROM organizations WHERE slug='default')"
    )

    op.create_index("idx_projects_org", "projects", ["organization_id"])
    op.create_index("idx_users_org", "users", ["organization_id"])


def downgrade() -> None:
    op.drop_index("idx_users_org", table_name="users")
    op.drop_index("idx_projects_org", table_name="projects")
    op.drop_column("users", "organization_id")
    op.drop_column("projects", "organization_id")
    op.drop_table("organizations")
