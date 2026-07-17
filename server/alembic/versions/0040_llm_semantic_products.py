"""Add the encrypted semantic-candidate ledger for v1 LLM attempts.

Revision ID: 0040_llm_semantic_products
Revises: 0039_second_opinion_products
Create Date: 2026-07-16

Despite the historical table name, these rows are transport-normalized
*candidates*.  Consumer grounding and classification remain separate.  The
database stores only encrypted canonical JSON plus bounded hashes and binding
metadata; prompts, source, raw provider responses, and exceptions have no
plaintext column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0040_llm_semantic_products"
down_revision = "0039_second_opinion_products"
branch_labels = None
depends_on = None


_MAX_PAYLOAD_BYTES = 1_048_576
_MAX_CIPHERTEXT_BYTES = 1_500_000


def upgrade() -> None:
    op.create_table(
        "llm_semantic_products",
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "operation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.BigInteger(), nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("contract_name", sa.String(96), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("binding_fingerprint", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_bytes", sa.BigInteger(), nullable=False),
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("payload_iv", sa.LargeBinary(), nullable=False),
        sa.Column(
            "candidate_status",
            sa.String(16),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["llm_calls.id"],
            name="fk_llm_semantic_products_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "attempt_id",
            name="pk_llm_semantic_products",
        ),
        sa.UniqueConstraint(
            "operation_id",
            "attempt_no",
            name="uq_llm_semantic_products_operation_attempt",
        ),
        sa.CheckConstraint(
            "attempt_no > 0",
            name="ck_llm_semantic_products_attempt_no",
        ),
        sa.CheckConstraint(
            "purpose IN ('summary', 'agent_extract', 'flow', "
            "'chat_rewrite', 'chat_answer', 'second_opinion')",
            name="ck_llm_semantic_products_purpose",
        ),
        sa.CheckConstraint(
            "length(contract_name) BETWEEN 3 AND 96",
            name="ck_llm_semantic_products_contract_name",
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64 AND "
            "length(binding_fingerprint) = 64 AND "
            "length(payload_sha256) = 64",
            name="ck_llm_semantic_products_hashes",
        ),
        sa.CheckConstraint(
            f"payload_bytes BETWEEN 2 AND {_MAX_PAYLOAD_BYTES} "
            "AND length(payload_ciphertext) BETWEEN 1 AND "
            f"{_MAX_CIPHERTEXT_BYTES} "
            "AND length(payload_iv) = 16",
            name="ck_llm_semantic_products_payload_bounds",
        ),
        sa.CheckConstraint(
            "(candidate_status IN ('candidate', 'accepted') "
            "AND failure_code IS NULL) OR "
            "(candidate_status = 'rejected' AND failure_code IS NOT NULL "
            "AND length(failure_code) BETWEEN 1 AND 64)",
            name="ck_llm_semantic_products_status_shape",
        ),
    )
    op.create_index(
        "ix_llm_semantic_products_project_created",
        "llm_semantic_products",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_llm_semantic_products_purpose_status",
        "llm_semantic_products",
        ["purpose", "candidate_status"],
        unique=False,
    )


def downgrade() -> None:
    # Dropping an encrypted candidate would turn an already-spent stable
    # operation into an unrecoverable terminal-attempt-without-product gap.
    # Refuse before the first destructive DDL statement.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (SELECT 1 FROM llm_semantic_products LIMIT 1)
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0040: encrypted LLM candidates exist'
                    USING HINT =
                        'Retain 0040 or consume and explicitly retire every '
                        'candidate under its stable operation contract first; '
                        'otherwise a terminal-attempt-without-product gap '
                        'would be created.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_index(
        "ix_llm_semantic_products_purpose_status",
        table_name="llm_semantic_products",
    )
    op.drop_index(
        "ix_llm_semantic_products_project_created",
        table_name="llm_semantic_products",
    )
    op.drop_table("llm_semantic_products")
