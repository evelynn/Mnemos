"""Append-only writer helpers used by analyzer ingestion and Week-3+ merge."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Edge, Node

_CERTAINTY_RANK = {"inferred": 0, "asserted": 1, "verified": 2}


class GraphHistoryPruneDisabled(RuntimeError):
    """Destructive history retention has no auditable watermark yet."""


def _would_downgrade(current: str, incoming: str) -> bool:
    return _CERTAINTY_RANK.get(incoming, -1) < _CERTAINTY_RANK.get(current, -1)


def _canonical_json(value: Any) -> str:
    """Return a stable representation for a JSON/JSONB value.

    Analyzer payloads are JSON objects, so object-key order is not semantic.
    Comparing their canonical form avoids creating graph history merely because
    two producers constructed the same mapping in a different insertion order.
    Array order remains significant, as it is in the stored payload.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _node_matches(
    current: Node,
    *,
    kind: str,
    data: dict[str, Any],
    certainty: str,
    source_name: str,
) -> bool:
    """Compare only the semantic fields of a node version."""
    return (
        current.kind == kind
        and _canonical_json(current.data) == _canonical_json(data)
        and current.certainty == certainty
        and current.created_by == [source_name]
    )


def _edge_matches(
    current: Edge,
    *,
    source_id: str,
    target_id: str,
    kind: str,
    data: dict[str, Any],
    certainty: str,
    source_name: str,
) -> bool:
    """Compare an edge by logical identity and semantic payload.

    The physical edge UUID and bitemporal timestamps intentionally do not
    participate: regenerating a row identifier is not a source-code change.
    """
    return (
        current.source_id == source_id
        and current.target_id == target_id
        and current.kind == kind
        and _canonical_json(current.data) == _canonical_json(data)
        and current.certainty == certainty
        and current.created_by == [source_name]
    )


async def upsert_node(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    node_id: str,
    kind: str,
    data: dict[str, Any],
    certainty: str,
    source_name: str,
    skip_close: bool = False,
) -> None:
    """Insert or semantically supersede a current node version.

    An identical current version is a no-op: its ``valid_from`` stays stable,
    so re-analyzing unchanged source does not manufacture bitemporal history or
    a false ``compare_runs`` modification. A changed version closes the old
    row and inserts a fresh one, preserving real source history.

    ``skip_close`` omits the supersede-UPDATE. Callers set it ONLY when they
    can guarantee no currently-valid row exists for this id — the analyzer
    ingest of a fresh (empty) graph, where every UPDATE would match zero rows.
    On a large first pass this halves the write statements (PR-200). Passing it
    when a current row *does* exist would leave a duplicate current version, so
    the default is False and the ingest path gates it on the graph being empty
    AND the id not already written this run.
    """
    current: list[Node] = []
    if not skip_close:
        current = (
            await session.execute(
                select(Node)
                .where(
                    and_(
                        Node.project_id == project_id,
                        Node.id == node_id,
                        Node.valid_to.is_(None),
                    )
                )
                .limit(2)
            )
        ).scalars().all()
        if len(current) == 1 and _node_matches(
            current[0],
            kind=kind,
            data=data,
            certainty=certainty,
            source_name=source_name,
        ):
            return
        if len(current) == 1 and _would_downgrade(
            current[0].certainty, certainty
        ):
            # Optional inferred analysis may enrich its isolated namespace,
            # but it must never replace an asserted/verified deterministic
            # fact that happens to share an ID.
            return
    now = datetime.now(tz=timezone.utc)
    if current:
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
    skip_close: bool = False,
) -> None:
    """Insert or semantically supersede an edge by logical identity.

    The physical UUID is deliberately ignored when deciding whether an edge
    changed. Identical re-analysis therefore leaves one stable current row;
    changed payload, certainty, or provenance still produces history.

    ``skip_close`` — see :func:`upsert_node`. Same fresh-graph fast path
    (PR-200): omit the supersede-UPDATE when no current edge of this identity
    can exist. Default False.
    """
    current: list[Edge] = []
    if not skip_close:
        current = (
            await session.execute(
                select(Edge)
                .where(
                    and_(
                        Edge.project_id == project_id,
                        Edge.source_id == source_id,
                        Edge.target_id == target_id,
                        Edge.kind == kind,
                        Edge.valid_to.is_(None),
                    )
                )
                .limit(2)
            )
        ).scalars().all()
        if len(current) == 1 and _edge_matches(
            current[0],
            source_id=source_id,
            target_id=target_id,
            kind=kind,
            data=data,
            certainty=certainty,
            source_name=source_name,
        ):
            return
        if len(current) == 1 and _would_downgrade(
            current[0].certainty, certainty
        ):
            return
    now = datetime.now(tz=timezone.utc)
    if current:
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
    """Reject destructive bitemporal pruning until retention is observable.

    ``compare_runs`` reads historical Node/Edge versions. Deleting them
    without an atomic ``history_retained_from`` watermark and independent
    history revision makes an old diff silently wrong and lets a READ
    COMMITTED request straddle the delete.  The former seven-day automatic
    prune did exactly that, so correctness takes precedence over reclaiming
    storage. A future bounded-retention implementation must persist the
    watermark/revision in the same transaction and make readers fail closed
    outside the retained interval.
    """

    _ = (session, project_id, keep_days)
    raise GraphHistoryPruneDisabled(
        "graph history pruning is disabled until a retained-from watermark "
        "and history revision are part of the reader contract"
    )
