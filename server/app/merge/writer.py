"""Append-only writer helpers used by analyzer ingestion and Week-3+ merge."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Edge, Node


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
    # NodeSource.raw_data used to store a full copy of ``data`` per node for
    # multi-source provenance — but nothing reads it, and it doubled graph
    # storage (84 MB of dead duplicate of nodes.data on a large repo, PR-184).
    # The source that contributed a node is already on ``Node.created_by``, so
    # the redundant write is dropped. (The table is kept for a future merge
    # that genuinely needs per-source raw payloads.)


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


async def prune_graph_history(
    session: AsyncSession, *, project_id: uuid.UUID, keep_days: int = 7
) -> dict[str, int]:
    """Delete superseded (``valid_to`` set) node/edge rows older than
    ``keep_days``. The current graph (``valid_to IS NULL``) is never touched.

    Re-analysis supersedes rows instead of overwriting them (bitemporal
    history), which grows the DB without bound across runs. Nothing reads
    deep history — recent-diff queries only look back a little — so a short
    retention window keeps those working while bounding growth (PR-184).
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max(0, keep_days))
    n = await session.execute(
        delete(Node).where(
            Node.project_id == project_id,
            Node.valid_to.is_not(None),
            Node.valid_to < cutoff,
        )
    )
    e = await session.execute(
        delete(Edge).where(
            Edge.project_id == project_id,
            Edge.valid_to.is_not(None),
            Edge.valid_to < cutoff,
        )
    )
    return {"nodes": n.rowcount or 0, "edges": e.rowcount or 0}
