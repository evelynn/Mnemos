"""secrets: organization_id (PR-88, closes CRIT cross-tenant leak)

Revision ID: 0021_secret_organization_id
Revises: 0020_finding_risk_columns
Create Date: 2026-05-23

The platform review found the ``secrets`` table had no organisation
column at all — ``GET /api/v1/secrets`` returned every tenant's
ciphertext metadata (label, kind, last test result) to any logged-in
user. Adding the column is online: nullable, no backfill mandatory,
no default. Existing rows are read as "legacy / default-org pool"
visible to any admin until an operator assigns them.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0021_secret_organization_id"
down_revision: Union[str, None] = "0020_finding_risk_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "secrets",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_secrets_organization_id", "secrets", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_secrets_organization_id", table_name="secrets")
    op.drop_column("secrets", "organization_id")
