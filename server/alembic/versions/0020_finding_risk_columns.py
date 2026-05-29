"""findings: risk_score + remediation + cwe_id + acknowledged_at (PR-50)

Revision ID: 0020_finding_risk_columns
Revises: 0019_user_invites_and_resets
Create Date: 2026-05-13

The 19th-round value audit scored "솔루션 분석 기능" at 35/100 —
findings were observations with no priority ordering and no fix
guidance. These columns turn each finding into a triageable,
actionable item:

* ``risk_score``    — 0-100, computed by app.merge.risk.
* ``remediation``   — deterministic fix hint keyed off finding kind.
* ``cwe_id``        — CWE catalogue cross-reference where one applies.
* ``acknowledged_at`` — the missing middle state between open and
  resolved, so time-to-acknowledge and time-to-resolve can both be
  measured.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_finding_risk_columns"
down_revision: Union[str, None] = "0019_user_invites_and_resets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column("risk_score", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("findings", sa.Column("remediation", sa.Text(), nullable=True))
    op.add_column("findings", sa.Column("cwe_id", sa.String(), nullable=True))
    op.add_column(
        "findings",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The risk-ordered findings list is the default dashboard view,
    # so an index on (project_id, risk_score desc) keeps it fast.
    op.create_index(
        "ix_findings_project_risk",
        "findings",
        ["project_id", sa.text("risk_score DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_findings_project_risk", table_name="findings")
    op.drop_column("findings", "acknowledged_at")
    op.drop_column("findings", "cwe_id")
    op.drop_column("findings", "remediation")
    op.drop_column("findings", "risk_score")
