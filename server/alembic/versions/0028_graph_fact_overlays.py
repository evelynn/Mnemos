"""Durable human/runtime overlays for logical graph facts.

Revision ID: 0028_graph_fact_overlays
Revises: 0027_atomic_graph_publication
Create Date: 2026-07-15

Analyzer publication replaces physical bitemporal Node/Edge versions.  Human
confirmation and OTLP evidence previously lived only inside those physical
rows, so a refresh could erase them or race an in-flight metadata update.
This revision gives that independently-owned evidence durable logical keys and
backfills current legacy materializations before Phase-B publication is used.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0028_graph_fact_overlays"
down_revision = "0027_atomic_graph_publication"
branch_labels = None
depends_on = None


def _human_columns() -> list[sa.Column]:
    return [
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "graph_heads",
        sa.Column(
            "overlay_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_graph_heads_overlay_generation",
        "graph_heads",
        "overlay_generation >= 0",
    )
    op.create_table(
        "graph_node_human_overlays",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("node_id", sa.String(), primary_key=True, nullable=False),
        *_human_columns(),
        sa.CheckConstraint(
            "action IN ('confirm', 'dispute')",
            name="ck_graph_node_human_overlay_action",
        ),
    )
    op.create_table(
        "graph_edge_human_overlays",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("source_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("target_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(), primary_key=True, nullable=False),
        *_human_columns(),
        sa.CheckConstraint(
            "action IN ('confirm', 'dispute')",
            name="ck_graph_edge_human_overlay_action",
        ),
    )
    op.create_table(
        "graph_edge_runtime_overlays",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("source_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("target_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "hit_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "hit_count >= 0", name="ck_graph_edge_runtime_overlay_hit_count"
        ),
        sa.CheckConstraint(
            "first_seen_at <= last_seen_at",
            name="ck_graph_edge_runtime_overlay_seen_order",
        ),
    )
    op.create_index(
        "ix_graph_edge_runtime_overlay_project_last_seen",
        "graph_edge_runtime_overlays",
        ["project_id", "last_seen_at"],
    )
    op.create_unique_constraint(
        "uq_runtime_obs_id_project",
        "runtime_observations",
        ["id", "project_id"],
    )
    op.create_table(
        "graph_edge_runtime_cursors",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("source_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("target_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "observation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "applied_seen_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "applied_seen_count >= 0",
            name="ck_graph_edge_runtime_cursor_seen_count",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id", "target_id", "kind"],
            [
                "graph_edge_runtime_overlays.project_id",
                "graph_edge_runtime_overlays.source_id",
                "graph_edge_runtime_overlays.target_id",
                "graph_edge_runtime_overlays.kind",
            ],
            ondelete="CASCADE",
            name="fk_graph_edge_runtime_cursor_overlay",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "project_id"],
            ["runtime_observations.id", "runtime_observations.project_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_runtime_cursor_observation_project",
        ),
    )
    op.create_index(
        "ix_graph_edge_runtime_cursor_observation",
        "graph_edge_runtime_cursors",
        ["observation_id"],
    )

    # Existing values were emitted by this application, but a defensive
    # migration must not fail if an older analyzer placed malformed text in
    # the JSONB.  The transaction-local parser converts only valid timestamps.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION pg_temp.mnemos_safe_timestamptz(value text)
        RETURNS timestamptz
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RETURN value::timestamptz;
        EXCEPTION WHEN OTHERS THEN
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        INSERT INTO graph_node_human_overlays
            (project_id, node_id, action, actor, rationale, recorded_at,
             metadata, updated_at)
        SELECT DISTINCT ON (project_id, id)
            project_id,
            id,
            data->'human_confirmation'->>'action',
            COALESCE(NULLIF(data->'human_confirmation'->>'by', ''),
                     'legacy:unknown'),
            COALESCE(data->'human_confirmation'->>'rationale', ''),
            COALESCE(
                pg_temp.mnemos_safe_timestamptz(
                    data->'human_confirmation'->>'at'
                ),
                valid_from,
                CURRENT_TIMESTAMP
            ),
            data->'human_confirmation',
            CURRENT_TIMESTAMP
        FROM nodes
        WHERE jsonb_typeof(data->'human_confirmation') = 'object'
          AND data->'human_confirmation'->>'action' IN ('confirm', 'dispute')
        ORDER BY project_id, id, valid_from DESC
        ON CONFLICT (project_id, node_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO graph_edge_human_overlays
            (project_id, source_id, target_id, kind, action, actor, rationale,
             recorded_at, metadata, updated_at)
        SELECT DISTINCT ON (project_id, source_id, target_id, kind)
            project_id,
            source_id,
            target_id,
            kind,
            data->'human_confirmation'->>'action',
            COALESCE(NULLIF(data->'human_confirmation'->>'by', ''),
                     'legacy:unknown'),
            COALESCE(data->'human_confirmation'->>'rationale', ''),
            COALESCE(
                pg_temp.mnemos_safe_timestamptz(
                    data->'human_confirmation'->>'at'
                ),
                valid_from,
                CURRENT_TIMESTAMP
            ),
            data->'human_confirmation',
            CURRENT_TIMESTAMP
        FROM edges
        WHERE jsonb_typeof(data->'human_confirmation') = 'object'
          AND data->'human_confirmation'->>'action' IN ('confirm', 'dispute')
        ORDER BY project_id, source_id, target_id, kind, valid_from DESC
        ON CONFLICT (project_id, source_id, target_id, kind) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO graph_edge_runtime_overlays
            (project_id, source_id, target_id, kind, first_seen_at,
             last_seen_at, hit_count, metadata, updated_at)
        SELECT DISTINCT ON (project_id, source_id, target_id, kind)
            project_id,
            source_id,
            target_id,
            kind,
            COALESCE(
                pg_temp.mnemos_safe_timestamptz(data->>'first_seen_at'),
                valid_from,
                CURRENT_TIMESTAMP
            ),
            GREATEST(
                COALESCE(
                    pg_temp.mnemos_safe_timestamptz(data->>'last_seen_at'),
                    valid_from,
                    CURRENT_TIMESTAMP
                ),
                COALESCE(
                    pg_temp.mnemos_safe_timestamptz(data->>'first_seen_at'),
                    valid_from,
                    CURRENT_TIMESTAMP
                )
            ),
            CASE
                WHEN data->>'hit_count' ~ '^[0-9]{1,18}$'
                THEN (data->>'hit_count')::bigint
                ELSE 0
            END,
            jsonb_build_object(
                'migrated_from_edge_data', true,
                'migrated_at', CURRENT_TIMESTAMP,
                'legacy_hit_credit_remaining',
                    CASE
                        WHEN data->>'hit_count' ~ '^[0-9]{1,18}$'
                        THEN (data->>'hit_count')::bigint
                        ELSE 0
                    END,
                'legacy_runtime', jsonb_strip_nulls(jsonb_build_object(
                    'first_seen_at', data->>'first_seen_at',
                    'last_seen_at', data->>'last_seen_at',
                    'hit_count', data->'hit_count'
                ))
            ),
            CURRENT_TIMESTAMP
        FROM edges
        WHERE lower(COALESCE(data->>'exercised', '')) = 'true'
        ORDER BY project_id, source_id, target_id, kind, valid_from DESC
        ON CONFLICT (project_id, source_id, target_id, kind) DO NOTHING
        """
    )


def downgrade() -> None:
    # These tables are not a disposable cache. Human judgements can outlive a
    # physical graph fact, and replay cursors are the exactly-once boundary for
    # cumulative runtime counts. Silently dropping either makes a later
    # re-upgrade lose evidence or double-apply observations. An operator must
    # explicitly export/retire this state before crossing the compatibility
    # boundary.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (SELECT 1 FROM graph_node_human_overlays LIMIT 1)
               OR EXISTS (SELECT 1 FROM graph_edge_human_overlays LIMIT 1)
               OR EXISTS (SELECT 1 FROM graph_edge_runtime_overlays LIMIT 1)
               OR EXISTS (SELECT 1 FROM graph_edge_runtime_cursors LIMIT 1)
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0028: durable graph overlays or replay cursors exist'
                    USING HINT =
                        'Export the overlay/cursor tables and explicitly retire '
                        'their rows before retrying; otherwise human evidence may '
                        'be lost and runtime counts may be replayed.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_index(
        "ix_graph_edge_runtime_cursor_observation",
        table_name="graph_edge_runtime_cursors",
    )
    op.drop_table("graph_edge_runtime_cursors")
    op.drop_constraint(
        "uq_runtime_obs_id_project",
        "runtime_observations",
        type_="unique",
    )
    op.drop_index(
        "ix_graph_edge_runtime_overlay_project_last_seen",
        table_name="graph_edge_runtime_overlays",
    )
    op.drop_table("graph_edge_runtime_overlays")
    op.drop_table("graph_edge_human_overlays")
    op.drop_table("graph_node_human_overlays")
    op.drop_constraint(
        "ck_graph_heads_overlay_generation",
        "graph_heads",
        type_="check",
    )
    op.drop_column("graph_heads", "overlay_generation")
