"""Bind grounded Summary products to durable semantic candidates.

Revision ID: 0041_summary_llm_provenance
Revises: 0040_llm_semantic_products
Create Date: 2026-07-16

The migration is expand-only: existing deterministic and legacy summaries
retain an all-NULL provenance triplet.  A model-backed writer may later set
the attempt id, semantic binding fingerprint, and projection digest together.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0041_summary_llm_provenance"
down_revision = "0040_llm_semantic_products"
branch_labels = None
depends_on = None


_PROVENANCE_SHAPE = (
    "(llm_attempt_id IS NULL AND "
    "semantic_binding_fingerprint IS NULL AND "
    "projection_sha256 IS NULL) OR "
    "(llm_attempt_id IS NOT NULL AND "
    "semantic_binding_fingerprint IS NOT NULL AND "
    "projection_sha256 IS NOT NULL)"
)

_PROVENANCE_SHA256 = (
    "(semantic_binding_fingerprint IS NULL AND "
    "projection_sha256 IS NULL) OR "
    "(semantic_binding_fingerprint IS NOT NULL AND "
    "projection_sha256 IS NOT NULL AND "
    "length(semantic_binding_fingerprint) = 64 AND "
    "semantic_binding_fingerprint = lower(semantic_binding_fingerprint) AND "
    "length(replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "semantic_binding_fingerprint, '0', ''), '1', ''), '2', ''), '3', ''), "
    "'4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), "
    "'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0 AND "
    "length(projection_sha256) = 64 AND "
    "projection_sha256 = lower(projection_sha256) AND "
    "length(replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "projection_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
    "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
    "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)"
)


def upgrade() -> None:
    # Alembic's batch facade emits ordinary ALTER TABLE on PostgreSQL and
    # performs the required table-copy operation on SQLite, whose ALTER TABLE
    # cannot add named CHECK/FK constraints in place.
    with op.batch_alter_table("summaries") as batch_op:
        batch_op.add_column(
            sa.Column(
                "llm_attempt_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "semantic_binding_fingerprint",
                sa.String(64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "projection_sha256",
                sa.String(64),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_summaries_llm_attempt",
            "llm_calls",
            ["llm_attempt_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_summaries_llm_provenance_shape",
            _PROVENANCE_SHAPE,
        )
        batch_op.create_check_constraint(
            "ck_summaries_llm_provenance_sha256",
            _PROVENANCE_SHA256,
        )

    op.create_index(
        "uq_summaries_llm_attempt",
        "summaries",
        ["llm_attempt_id"],
        unique=True,
        postgresql_where=sa.text("llm_attempt_id IS NOT NULL"),
        sqlite_where=sa.text("llm_attempt_id IS NOT NULL"),
    )


def _guard_downgrade() -> None:
    context = op.get_context()
    if context.dialect.name == "sqlite":
        if context.as_sql:
            raise RuntimeError(
                "cannot emit offline SQLite downgrade 0041: the non-NULL "
                "Summary provenance guard requires an online connection"
            )
        blocker = op.get_bind().execute(
            sa.text(
                "SELECT 1 FROM summaries "
                "WHERE llm_attempt_id IS NOT NULL LIMIT 1"
            )
        ).first()
        if blocker is not None:
            raise RuntimeError(
                "cannot downgrade 0041: durable Summary LLM provenance exists; "
                "retain 0041 or explicitly retire every candidate-bound "
                "Summary product first"
            )
        return

    # A candidate-bound projection has no safe representation before 0041.
    # Refuse before dropping the unique ownership index or any column.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM summaries
                WHERE llm_attempt_id IS NOT NULL
                LIMIT 1
            )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0041: durable Summary LLM provenance exists'
                    USING HINT =
                        'Retain 0041 or explicitly retire every '
                        'candidate-bound Summary product first; dropping this '
                        'binding would permit one physical candidate to be '
                        'projected again without ownership provenance.';
            END IF;
        END
        $mnemos$;
        """
    )


def downgrade() -> None:
    _guard_downgrade()
    op.drop_index(
        "uq_summaries_llm_attempt",
        table_name="summaries",
    )
    with op.batch_alter_table("summaries") as batch_op:
        batch_op.drop_constraint(
            "ck_summaries_llm_provenance_sha256",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_summaries_llm_provenance_shape",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_summaries_llm_attempt",
            type_="foreignkey",
        )
        batch_op.drop_column("projection_sha256")
        batch_op.drop_column("semantic_binding_fingerprint")
        batch_op.drop_column("llm_attempt_id")
