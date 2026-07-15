"""Give graph findings stable logical identity and exact revision markers.

Revision ID: 0031_finding_graph_currentness
Revises: 0030_summary_graph_generation
Create Date: 2026-07-15

Existing identities are reconstructed from the subject node or the referenced
bitemporal edge's logical tuple. Legacy rows whose edge provenance is missing
fall back to their immutable Finding UUID; those rows remain unvalidated and
therefore hidden from current-state consumers until a detector rebuild.
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_finding_graph_currentness"
down_revision = "0030_summary_graph_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column("identity_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "findings",
        sa.Column("validated_graph_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "findings",
        sa.Column("validated_overlay_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "findings",
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # PostgreSQL's built-in sha256(bytea) avoids an extension dependency. The
    # loop keeps each statement's working set bounded while retaining one
    # transactional migration. Canonical fields are byte-length-prefixed so
    # punctuation or Unicode cannot create ambiguous identities. The payload
    # exactly matches ``app.finding_identity.canonical_finding_identity``.
    op.execute(
        sa.text(
            """
            DO $mnemos$
            DECLARE
                updated_count integer;
            BEGIN
                LOOP
                    WITH batch AS (
                        SELECT
                            f.id,
                            f.kind AS finding_kind,
                            f.subject_node_id,
                            f.subject_edge_id,
                            f.detail,
                            e.source_id AS stored_source_id,
                            e.target_id AS stored_target_id,
                            e.kind AS stored_edge_kind
                        FROM findings AS f
                        LEFT JOIN edges AS e
                          ON e.id = f.subject_edge_id
                         AND e.project_id = f.project_id
                        WHERE f.identity_key IS NULL
                        ORDER BY f.id
                        LIMIT 1000
                        FOR UPDATE OF f SKIP LOCKED
                    ),
                    subjects AS (
                        SELECT
                            id,
                            finding_kind,
                            subject_node_id,
                            subject_edge_id,
                            COALESCE(
                                stored_source_id,
                                NULLIF(detail->>'source', '')
                            ) AS edge_source_id,
                            COALESCE(
                                stored_target_id,
                                NULLIF(detail->>'target', ''),
                                NULLIF(detail->>'missing_target', '')
                            ) AS edge_target_id,
                            COALESCE(
                                stored_edge_kind,
                                NULLIF(detail->>'edge_kind', ''),
                                CASE
                                    WHEN finding_kind IN (
                                        'dynamic_call_detected',
                                        'dead_path_suspected'
                                    ) THEN 'CALLS'
                                    ELSE NULL
                                END,
                                NULLIF(detail->>'kind', '')
                            ) AS edge_kind
                        FROM batch
                    ),
                    canonical AS (
                        SELECT
                            id,
                            CASE
                                WHEN edge_source_id IS NOT NULL
                                 AND edge_target_id IS NOT NULL
                                 AND edge_kind IS NOT NULL
                                 AND (
                                     subject_edge_id IS NOT NULL
                                     OR finding_kind IN (
                                         'unverified_claim',
                                         'dynamic_call_detected',
                                         'dead_path_suspected',
                                         'schema_mismatch'
                                     )
                                 )
                                THEN
                                    'mnemos.finding.identity.v1|kind=' ||
                                    octet_length(convert_to(
                                        finding_kind, 'UTF8'
                                    ))::text || ':' ||
                                    finding_kind || '|edge_source=' ||
                                    octet_length(convert_to(
                                        edge_source_id, 'UTF8'
                                    ))::text || ':' ||
                                    edge_source_id || '|edge_target=' ||
                                    octet_length(convert_to(
                                        edge_target_id, 'UTF8'
                                    ))::text || ':' ||
                                    edge_target_id || '|edge_kind=' ||
                                    octet_length(convert_to(
                                        edge_kind, 'UTF8'
                                    ))::text || ':' ||
                                    edge_kind
                                WHEN subject_node_id IS NOT NULL
                                THEN
                                    'mnemos.finding.identity.v1|kind=' ||
                                    octet_length(convert_to(
                                        finding_kind, 'UTF8'
                                    ))::text || ':' ||
                                    finding_kind || '|node=' ||
                                    octet_length(convert_to(
                                        subject_node_id, 'UTF8'
                                    ))::text || ':' ||
                                    subject_node_id
                                ELSE
                                    'mnemos.finding.identity.v1|kind=' ||
                                    octet_length(convert_to(
                                        finding_kind, 'UTF8'
                                    ))::text || ':' ||
                                    finding_kind || '|legacy_id=' ||
                                    octet_length(convert_to(
                                        id::text, 'UTF8'
                                    ))::text || ':' || id::text
                            END AS payload
                        FROM subjects
                    ),
                    changed AS (
                        UPDATE findings AS f
                           SET identity_key = encode(
                               sha256(convert_to(c.payload, 'UTF8')),
                               'hex'
                           )
                          FROM canonical AS c
                         WHERE f.id = c.id
                           AND f.identity_key IS NULL
                        RETURNING f.id
                    )
                    SELECT count(*) INTO updated_count FROM changed;

                    EXIT WHEN updated_count = 0;
                END LOOP;
            END
            $mnemos$;
            """
        )
    )

    # Old application-level upserts had no database uniqueness fence, so two
    # concurrent rebuilds could have persisted duplicate logical rows. Keep a
    # deterministic canonical row (preferring an explicit user disposition)
    # and re-key every additional physical audit row through the documented
    # legacy UUID fallback. Nothing is deleted and all lifecycle fields remain
    # intact. Re-ranking after each <=1000-row update bounds mutation batches.
    op.execute(
        sa.text(
            """
            DO $mnemos$
            DECLARE
                updated_count integer;
            BEGIN
                LOOP
                    WITH ranked AS (
                        SELECT
                            id,
                            kind,
                            row_number() OVER (
                                PARTITION BY project_id, kind, identity_key
                                ORDER BY
                                    CASE
                                        WHEN status = 'false_positive' THEN 0
                                        WHEN status = 'resolved'
                                         AND COALESCE(resolved_by, '') <>
                                             'system:graph-refresh' THEN 0
                                        WHEN status = 'acknowledged' THEN 1
                                        ELSE 2
                                    END,
                                    first_seen_at,
                                    id
                            ) AS duplicate_rank
                        FROM findings
                    ),
                    batch AS (
                        SELECT id, kind
                        FROM ranked
                        WHERE duplicate_rank > 1
                        ORDER BY id
                        LIMIT 1000
                    ),
                    changed AS (
                        UPDATE findings AS f
                           SET identity_key = encode(
                               sha256(convert_to(
                                   'mnemos.finding.identity.v1|kind=' ||
                                   octet_length(convert_to(
                                       b.kind, 'UTF8'
                                   ))::text || ':' ||
                                   b.kind || '|legacy_id=' ||
                                   octet_length(convert_to(
                                       b.id::text, 'UTF8'
                                   ))::text || ':' ||
                                   b.id::text,
                                   'UTF8'
                               )),
                               'hex'
                           )
                          FROM batch AS b
                         WHERE f.id = b.id
                        RETURNING f.id
                    )
                    SELECT count(*) INTO updated_count FROM changed;

                    EXIT WHEN updated_count = 0;
                END LOOP;
            END
            $mnemos$;
            """
        )
    )

    op.alter_column(
        "findings",
        "identity_key",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_findings_project_kind_identity",
        "findings",
        ["project_id", "kind", "identity_key"],
    )
    op.create_check_constraint(
        "ck_findings_identity_key_length",
        "findings",
        "length(identity_key) = 64",
    )
    op.create_check_constraint(
        "ck_findings_graph_validation_marker",
        "findings",
        "(validated_graph_generation IS NULL AND "
        "validated_overlay_generation IS NULL AND validated_at IS NULL) OR "
        "(validated_graph_generation IS NOT NULL AND "
        "validated_overlay_generation IS NOT NULL AND "
        "validated_at IS NOT NULL AND validated_graph_generation > 0 AND "
        "validated_overlay_generation >= 0)",
    )
    op.create_index(
        "ix_findings_project_graph_validation",
        "findings",
        [
            "project_id",
            "validated_graph_generation",
            "validated_overlay_generation",
            "status",
        ],
        unique=False,
    )


def downgrade() -> None:
    # Pre-0031 edge detectors collapse every edge finding of the same kind
    # into the ``subject_node_id IS NULL`` bucket and call
    # scalar_one_or_none(). Multiple logical edge findings are valid in the
    # new model but make that old query crash. Refuse to erase the identity
    # boundary until an operator has explicitly consolidated incompatible
    # rows; never delete audit findings implicitly in a schema rollback.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM findings
                GROUP BY project_id, kind, subject_node_id
                HAVING count(*) > 1
            )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0031: findings exceed the legacy identity cardinality'
                    USING HINT =
                        'Export or consolidate rows until each '
                        '(project_id,kind,subject_node_id) has at most one '
                        'finding, then retry the downgrade.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_index(
        "ix_findings_project_graph_validation",
        table_name="findings",
    )
    op.drop_constraint(
        "ck_findings_graph_validation_marker",
        "findings",
        type_="check",
    )
    op.drop_constraint(
        "ck_findings_identity_key_length",
        "findings",
        type_="check",
    )
    op.drop_constraint(
        "uq_findings_project_kind_identity",
        "findings",
        type_="unique",
    )
    op.drop_column("findings", "validated_at")
    op.drop_column("findings", "validated_overlay_generation")
    op.drop_column("findings", "validated_graph_generation")
    op.drop_column("findings", "identity_key")
