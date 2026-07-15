"""Invalidate password-reset credentials minted by the unsafe HTTP flow.

Revision ID: 0035_reset_token_invalidation
Revises: 0034_data_sample_revision
Create Date: 2026-07-15

Before this revision the anonymous reset-request endpoint returned the raw
credential for any active username.  Removing that response is insufficient:
an attacker may already hold an unexpired credential created by an older
application instance.  Deployment therefore consumes every outstanding reset
credential once, before the remediated application starts serving traffic.

Downgrade intentionally does not resurrect credentials.  A consumed security
token is historical state, not reversible schema state.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_reset_token_invalidation"
down_revision: Union[str, None] = "0034_data_sample_revision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE password_reset_tokens "
            "SET consumed_at = CURRENT_TIMESTAMP "
            "WHERE consumed_at IS NULL"
        )
    )


def downgrade() -> None:
    # Fail closed: never make a once-invalidated bearer credential usable.
    # Execute an explicit portable no-op so Alembic records a real downgrade
    # step without pretending that destroyed credential authority is
    # reversible.
    op.execute(sa.text("SELECT 1"))
