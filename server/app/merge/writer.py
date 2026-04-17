"""Append-only writer helpers used by analyzer ingestion and Week-3+ merge."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Edge, Node, NodeSource


async def upsert_node(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    node_id: str,
    kind: str,
    data: dict[str, Any],
    certainty: str,
    source_name: str,
) -> None:
    """Supersede any currently-valid node row for this (project, node_id) and
    insert a fresh one. The previous row is kept for snapshot history.
    """
    now = datetime.now(tz=timezone.utc)

    await session.execute(
        update(Node)
        .where(
            and_(
                Node.project_id == project_id,
                Node.id == node_id,
                Node.valid_to.is_(None),
            )
        )
        .values(valid_to=now)
    )

    session.add(
        Node(
            id=node_id,
            project_id=project_id,
            kind=kind,
            data=data,
            certainty=certainty,
            created_by=[source_name],
            valid_from=now,
        )
    )

    stmt = pg_insert(NodeSource).values(
        node_id=node_id,
        project_id=project_id,
        source_name=source_name,
        raw_data=data,
        contributed_at=now,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["node_id", "project_id", "source_name", "contributed_at"]
    )
    await session.execute(stmt)


async def upsert_edge(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    source_id: str,
    target_id: str,
    kind: str,
    data: dict[str, Any],
    certainty: str,
    source_name: str,
) -> None:
    """Close the currently-valid edge of the same (source, target, kind) and
    insert a new one. Multi-source merge arrives in Week 3.
    """
    now = datetime.now(tz=timezone.utc)

    await session.execute(
        update(Edge)
        .where(
            and_(
                Edge.project_id == project_id,
                Edge.source_id == source_id,
                Edge.target_id == target_id,
                Edge.kind == kind,
                Edge.valid_to.is_(None),
            )
        )
        .values(valid_to=now)
    )

    session.add(
        Edge(
            project_id=project_id,
            source_id=source_id,
            target_id=target_id,
            kind=kind,
            data=data,
            certainty=certainty,
            created_by=[source_name],
            valid_from=now,
        )
    )
