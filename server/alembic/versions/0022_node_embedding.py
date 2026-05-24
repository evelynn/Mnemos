"""nodes.embedding column for pgvector search (PR-90 — opt-in)

Revision ID: 0022_node_embedding
Revises: 0021_secret_organization_id
Create Date: 2026-05-23

Spec §11.3 mandates "벡터 + BM25 앙상블 (RRF)". The lexical half
shipped in PR-80; this migration adds the embedding column the
vector half writes to. **Opt-in**: pgvector is in the
``[search]`` extra — install ``mnemos-platform[search]`` and run
``alembic upgrade head`` to apply.

Skips itself cleanly when the ``vector`` extension isn't available
on the connecting Postgres, so the base alembic chain doesn't break
on a deployment that hasn't installed the extra.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_node_embedding"
down_revision: Union[str, None] = "0021_secret_organization_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DIM = 1024  # matches app.mcp.embeddings.EMBEDDING_DIM


def _has_pgvector(bind) -> bool:
    try:
        row = bind.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname='vector'")
        ).first()
        return row is not None
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_pgvector(bind):
        # Operator hasn't installed pgvector — leave the column off so
        # the rest of the schema stays consistent. Document the path:
        # run ``CREATE EXTENSION vector;`` then re-run alembic upgrade.
        op.execute(
            "SELECT 'pgvector_not_installed_skipping_embedding_column'::text"
        )
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        f"ALTER TABLE nodes ADD COLUMN IF NOT EXISTS embedding vector({_DIM})"
    )
    # ivfflat / hnsw is the operator's call (workload-dependent); ship
    # an hnsw index by default as the modern, lower-tune-cost option.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_nodes_embedding_cosine "
        "ON nodes USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_nodes_embedding_cosine")
    op.execute("ALTER TABLE nodes DROP COLUMN IF EXISTS embedding")
