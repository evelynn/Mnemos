"""Linearizable, durable overlays for human and runtime graph evidence.

Source publication owns graph identity/payload/certainty.  This module owns
facts learned *after* source analysis and projects them onto current rows only
for compatibility with legacy readers.  The overlay tables remain the source
of truth and are keyed by logical identity, so closing a bitemporal row cannot
erase a human judgement or an OTLP observation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Sequence, TypeVar

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Edge, GraphHead, Node
from app.models.overlays import (
    GraphEdgeHumanOverlay,
    GraphEdgeRuntimeCursor,
    GraphEdgeRuntimeOverlay,
    GraphNodeHumanOverlay,
)

HumanAction = Literal["confirm", "dispute"]
EdgeIdentity = tuple[str, str, str]
T = TypeVar("T")

# SQLite defaults to 999 bound parameters.  One edge identity consumes three
# plus the project id, so 250 remains portable and bounded.  PostgreSQL uses
# the same batches to avoid parameter explosions on large graph views.
_NODE_LOOKUP_BATCH = 800
_EDGE_LOOKUP_BATCH = 250
_HUMAN_DATA_KEY = "human_confirmation"
_RUNTIME_DATA_KEYS = frozenset(
    {"exercised", "first_seen_at", "last_seen_at", "hit_count"}
)


class GraphOverlayUnavailable(RuntimeError):
    """The project has no ready graph on which an overlay can be recorded."""


class GraphOverlayFactNotFound(LookupError):
    """The requested logical fact is not current after taking the head lock."""


@dataclass(frozen=True)
class EdgeOverlayBundle:
    human: dict[EdgeIdentity, GraphEdgeHumanOverlay]
    runtime: dict[EdgeIdentity, GraphEdgeRuntimeOverlay]


@dataclass(frozen=True)
class HumanConfirmationResult:
    target_kind: Literal["node", "edge"]
    target_id: str
    source_certainty: str
    effective_certainty: str
    action: HumanAction


@dataclass(frozen=True)
class RuntimeOverlayWriteResult:
    overlay: GraphEdgeRuntimeOverlay
    changed: bool
    new_hits: int


def _chunks(values: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def edge_identity(edge: Edge | tuple[str, str, str]) -> EdgeIdentity:
    if isinstance(edge, tuple):
        return edge
    return (edge.source_id, edge.target_id, edge.kind)


def effective_certainty(
    source_certainty: str,
    human: GraphNodeHumanOverlay | GraphEdgeHumanOverlay | HumanAction | None,
) -> str:
    """Return a display/query certainty without mutating source certainty.

    A human confirmation is one trustworthy assertion, so it can lift only
    ``inferred`` to ``asserted``.  ``verified`` remains reserved for direct
    deterministic/runtime evidence and a dispute never rewrites source truth.
    """

    action = human if isinstance(human, str) else getattr(human, "action", None)
    if action == "confirm" and source_certainty == "inferred":
        return "asserted"
    return source_certainty


def _human_payload(
    overlay: GraphNodeHumanOverlay | GraphEdgeHumanOverlay,
) -> dict[str, Any]:
    payload = dict(overlay.metadata_ or {})
    # Columns are authoritative; metadata is only forward-compatible detail.
    payload.update(
        {
            "action": overlay.action,
            "by": overlay.actor,
            "at": _as_utc(overlay.recorded_at).isoformat(),
            "rationale": overlay.rationale,
        }
    )
    return payload


def _runtime_payload(overlay: GraphEdgeRuntimeOverlay) -> dict[str, Any]:
    return {
        "exercised": "true",
        "first_seen_at": _as_utc(overlay.first_seen_at).isoformat(),
        "last_seen_at": _as_utc(overlay.last_seen_at).isoformat(),
        "hit_count": int(overlay.hit_count),
    }


def strip_durable_overlay_fields(
    data: dict[str, Any] | None, *, target_kind: Literal["node", "edge"]
) -> dict[str, Any]:
    """Remove fields that an analyzer/provider is not allowed to author.

    Stage writers should call this before semantic hashing.  Runtime fields on
    nodes are left alone because some deterministic/demo producers emit a
    direct node-level exercised signal; OTLP-owned fields are edge-only.
    """

    clean = dict(data or {})
    clean.pop(_HUMAN_DATA_KEY, None)
    if target_kind == "edge":
        for key in _RUNTIME_DATA_KEYS:
            clean.pop(key, None)
    return clean


def materialize_node_data(
    source_data: dict[str, Any] | None,
    human: GraphNodeHumanOverlay | None,
) -> dict[str, Any]:
    """Project a durable node overlay without modifying the input mapping."""

    data = dict(source_data or {})
    if human is not None:
        data[_HUMAN_DATA_KEY] = _human_payload(human)
    return data


def materialize_edge_data(
    source_data: dict[str, Any] | None,
    human: GraphEdgeHumanOverlay | None,
    runtime: GraphEdgeRuntimeOverlay | None,
) -> dict[str, Any]:
    """Project durable edge overlays without modifying source ownership."""

    data = dict(source_data or {})
    if human is not None:
        data[_HUMAN_DATA_KEY] = _human_payload(human)
    if runtime is not None:
        data.update(_runtime_payload(runtime))
    return data


def node_read_view(
    node: Node, human: GraphNodeHumanOverlay | None
) -> dict[str, Any]:
    """Canonical certainty/data projection for a node serializer."""

    source_certainty = node.certainty
    effective = effective_certainty(source_certainty, human)
    return {
        "data": materialize_node_data(node.data, human),
        "source_certainty": source_certainty,
        "effective_certainty": effective,
        # Compatibility: existing clients read ``certainty``.  It is now
        # explicitly the effective view while source certainty is also exposed.
        "certainty": effective,
        "confirmed": human.action if human is not None else None,
    }


def edge_read_view(
    edge: Edge,
    human: GraphEdgeHumanOverlay | None,
    runtime: GraphEdgeRuntimeOverlay | None,
) -> dict[str, Any]:
    """Canonical certainty/data/runtime projection for an edge serializer."""

    source_certainty = edge.certainty
    effective = effective_certainty(source_certainty, human)
    data = materialize_edge_data(edge.data, human, runtime)
    return {
        "data": data,
        "source_certainty": source_certainty,
        "effective_certainty": effective,
        "certainty": effective,
        "confirmed": human.action if human is not None else None,
        "exercised": str(data.get("exercised", "")).lower() == "true",
    }


async def load_node_human_overlays(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    node_ids: Iterable[str],
) -> dict[str, GraphNodeHumanOverlay]:
    ids = sorted(set(node_ids))
    result: dict[str, GraphNodeHumanOverlay] = {}
    for batch in _chunks(ids, _NODE_LOOKUP_BATCH):
        rows = (
            await session.execute(
                select(GraphNodeHumanOverlay).where(
                    GraphNodeHumanOverlay.project_id == project_id,
                    GraphNodeHumanOverlay.node_id.in_(batch),
                )
            )
        ).scalars().all()
        result.update((row.node_id, row) for row in rows)
    return result


async def load_edge_overlays(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    identities: Iterable[EdgeIdentity],
) -> EdgeOverlayBundle:
    keys = sorted(set(identities))
    human: dict[EdgeIdentity, GraphEdgeHumanOverlay] = {}
    runtime: dict[EdgeIdentity, GraphEdgeRuntimeOverlay] = {}
    for batch in _chunks(keys, _EDGE_LOOKUP_BATCH):
        human_rows = (
            await session.execute(
                select(GraphEdgeHumanOverlay).where(
                    GraphEdgeHumanOverlay.project_id == project_id,
                    tuple_(
                        GraphEdgeHumanOverlay.source_id,
                        GraphEdgeHumanOverlay.target_id,
                        GraphEdgeHumanOverlay.kind,
                    ).in_(batch),
                )
            )
        ).scalars().all()
        runtime_rows = (
            await session.execute(
                select(GraphEdgeRuntimeOverlay).where(
                    GraphEdgeRuntimeOverlay.project_id == project_id,
                    tuple_(
                        GraphEdgeRuntimeOverlay.source_id,
                        GraphEdgeRuntimeOverlay.target_id,
                        GraphEdgeRuntimeOverlay.kind,
                    ).in_(batch),
                )
            )
        ).scalars().all()
        human.update(
            ((row.source_id, row.target_id, row.kind), row) for row in human_rows
        )
        runtime.update(
            ((row.source_id, row.target_id, row.kind), row)
            for row in runtime_rows
        )
    return EdgeOverlayBundle(human=human, runtime=runtime)


async def lock_ready_graph_for_overlay_write(
    session: AsyncSession, *, project_id: uuid.UUID
) -> GraphHead:
    """Linearize an overlay write with graph promotion.

    ``FOR UPDATE`` conflicts with the promoter and another overlay writer and
    also locks the referenced ``AnalysisRun`` while its canonical receipt is
    checked. It does not pretend that an earlier HTTP guard remains valid.
    After acquiring it every writer re-reads the current logical fact before
    persisting evidence. SQLite ignores row-lock syntax; its single-writer
    transaction still serializes local-mode mutations.
    """

    # Local import avoids an import-time cycle: graph publication materializes
    # durable overlays while this writer needs publication authority.
    from app.graph_publication import (
        GraphPublicationError,
        lock_validated_graph_head,
    )

    try:
        head, _ = await lock_validated_graph_head(
            session, project_id=project_id
        )
    except GraphPublicationError as exc:
        raise GraphOverlayUnavailable(
            "graph overlay requires a ready atomically-published graph"
        ) from exc
    return head


async def advance_overlay_generation_once(
    session: AsyncSession, *, head: GraphHead
) -> int:
    """Advance the overlay revision exactly once in the current transaction.

    Every overlay writer locks ``GraphHead`` first.  Keeping the revision on
    that same row makes evidence mutation and reader invalidation one atomic
    commit.  A transaction-local token prevents a batched runtime reconcile
    (or two helper calls in one explicit transaction) from incrementing more
    than once.
    """

    transaction = session.get_transaction()
    if transaction is None:
        raise GraphOverlayUnavailable(
            "overlay generation requires an active head-locked transaction"
        )
    cached = session.info.get("mnemos_overlay_generation_advanced")
    if (
        isinstance(cached, tuple)
        and len(cached) == 2
        and cached[0] is transaction
        and cached[1] == head.project_id
    ):
        return int(head.overlay_generation)
    head.overlay_generation = int(head.overlay_generation or 0) + 1
    await session.flush([head])
    # Retain the transaction object itself, rather than only ``id(...)``.
    # A long-lived Session may create many transactions and CPython may reuse
    # an object's numeric id after collection. This strong reference makes a
    # later transaction structurally incapable of matching the prior token.
    session.info["mnemos_overlay_generation_advanced"] = (
        transaction,
        head.project_id,
    )
    return int(head.overlay_generation)


async def record_human_confirmation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    target_kind: Literal["node", "edge"],
    target_id: str,
    action: HumanAction,
    actor: str,
    rationale: str,
    recorded_at: datetime | None = None,
) -> HumanConfirmationResult:
    """Persist and materialize one human judgement in the locked snapshot."""

    now = _as_utc(recorded_at or datetime.now(tz=timezone.utc))
    head = await lock_ready_graph_for_overlay_write(
        session, project_id=project_id
    )

    if target_kind == "node":
        row = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == target_id,
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise GraphOverlayFactNotFound(target_id)
        overlay = (
            await session.execute(
                select(GraphNodeHumanOverlay).where(
                    GraphNodeHumanOverlay.project_id == project_id,
                    GraphNodeHumanOverlay.node_id == row.id,
                )
            )
        ).scalar_one_or_none()
        payload = {
            "action": action,
            "by": actor,
            "at": now.isoformat(),
            "rationale": rationale,
        }
        if overlay is None:
            overlay = GraphNodeHumanOverlay(
                project_id=project_id,
                node_id=row.id,
                action=action,
                actor=actor,
                rationale=rationale,
                recorded_at=now,
                metadata_=payload,
            )
            session.add(overlay)
        else:
            overlay.action = action
            overlay.actor = actor
            overlay.rationale = rationale
            overlay.recorded_at = now
            overlay.metadata_ = payload
            overlay.updated_at = now
        # Compatibility materialization; the durable row above is truth.
        row.data = materialize_node_data(row.data, overlay)
        source_certainty = row.certainty
    else:
        try:
            edge_uuid = uuid.UUID(target_id)
        except ValueError as exc:
            raise GraphOverlayFactNotFound(target_id) from exc
        row = (
            await session.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    Edge.id == edge_uuid,
                    Edge.valid_to.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise GraphOverlayFactNotFound(target_id)
        key = edge_identity(row)
        overlay = (
            await session.execute(
                select(GraphEdgeHumanOverlay).where(
                    GraphEdgeHumanOverlay.project_id == project_id,
                    GraphEdgeHumanOverlay.source_id == key[0],
                    GraphEdgeHumanOverlay.target_id == key[1],
                    GraphEdgeHumanOverlay.kind == key[2],
                )
            )
        ).scalar_one_or_none()
        payload = {
            "action": action,
            "by": actor,
            "at": now.isoformat(),
            "rationale": rationale,
        }
        if overlay is None:
            overlay = GraphEdgeHumanOverlay(
                project_id=project_id,
                source_id=key[0],
                target_id=key[1],
                kind=key[2],
                action=action,
                actor=actor,
                rationale=rationale,
                recorded_at=now,
                metadata_=payload,
            )
            session.add(overlay)
        else:
            overlay.action = action
            overlay.actor = actor
            overlay.rationale = rationale
            overlay.recorded_at = now
            overlay.metadata_ = payload
            overlay.updated_at = now
        runtime = (
            await session.execute(
                select(GraphEdgeRuntimeOverlay).where(
                    GraphEdgeRuntimeOverlay.project_id == project_id,
                    GraphEdgeRuntimeOverlay.source_id == key[0],
                    GraphEdgeRuntimeOverlay.target_id == key[1],
                    GraphEdgeRuntimeOverlay.kind == key[2],
                )
            )
        ).scalar_one_or_none()
        row.data = materialize_edge_data(row.data, overlay, runtime)
        source_certainty = row.certainty

    await session.flush()
    await advance_overlay_generation_once(session, head=head)
    return HumanConfirmationResult(
        target_kind=target_kind,
        target_id=target_id,
        source_certainty=source_certainty,
        effective_certainty=effective_certainty(source_certainty, overlay),
        action=action,
    )


async def record_runtime_observation_for_edge(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    edge: Edge,
    observation_id: uuid.UUID,
    observation_seen_count: int,
    first_seen_at: datetime,
    last_seen_at: datetime,
) -> RuntimeOverlayWriteResult:
    """Idempotently advance runtime evidence and materialize the current edge.

    The caller must already hold :func:`lock_ready_graph_for_overlay_write` and
    must have selected ``edge`` afterwards.  A per-observation cursor prevents
    the old bug where every analysis replay added the entire cumulative
    ``RuntimeObservation.seen_count`` again.
    """

    key = edge_identity(edge)
    first = _as_utc(first_seen_at)
    last = _as_utc(last_seen_at)
    if first > last:
        first = last
    seen_count = max(0, int(observation_seen_count))
    changed = False

    runtime = (
        await session.execute(
            select(GraphEdgeRuntimeOverlay).where(
                GraphEdgeRuntimeOverlay.project_id == project_id,
                GraphEdgeRuntimeOverlay.source_id == key[0],
                GraphEdgeRuntimeOverlay.target_id == key[1],
                GraphEdgeRuntimeOverlay.kind == key[2],
            )
        )
    ).scalar_one_or_none()
    cursor = (
        await session.execute(
            select(GraphEdgeRuntimeCursor).where(
                GraphEdgeRuntimeCursor.project_id == project_id,
                GraphEdgeRuntimeCursor.source_id == key[0],
                GraphEdgeRuntimeCursor.target_id == key[1],
                GraphEdgeRuntimeCursor.kind == key[2],
                GraphEdgeRuntimeCursor.observation_id == observation_id,
            )
        )
    ).scalar_one_or_none()

    pending_cursor: GraphEdgeRuntimeCursor | None = None
    if cursor is None:
        # A 0028 backfill may already hold cumulative hits from observations
        # that existed before the migration. Consume that legacy credit once
        # across all such observation identities. A post-migration
        # observation receives no credit and contributes its full count.
        migrated = bool(
            runtime is not None
            and (runtime.metadata_ or {}).get("migrated_from_edge_data")
        )
        baseline = 0
        if migrated and runtime is not None:
            metadata = dict(runtime.metadata_ or {})
            raw_boundary = metadata.get("migrated_at")
            boundary: datetime | None = None
            if isinstance(raw_boundary, datetime):
                boundary = _as_utc(raw_boundary)
            elif isinstance(raw_boundary, str):
                try:
                    boundary = _as_utc(
                        datetime.fromisoformat(
                            raw_boundary.replace("Z", "+00:00")
                        )
                    )
                except ValueError:
                    boundary = None
            if boundary is None and runtime.updated_at is not None:
                # Compatibility for databases migrated by an intermediate
                # 0028 build before the explicit boundary field existed.
                boundary = _as_utc(runtime.updated_at)
            raw_remaining = metadata.get("legacy_hit_credit_remaining")
            try:
                remaining = max(0, int(raw_remaining))
            except (TypeError, ValueError):
                remaining = max(0, int(runtime.hit_count))
            remaining = min(remaining, max(0, int(runtime.hit_count)))
            if boundary is not None and first <= boundary:
                baseline = min(seen_count, remaining)
                remaining -= baseline
            if boundary is not None:
                metadata["migrated_at"] = boundary.isoformat()
            metadata["legacy_hit_credit_remaining"] = remaining
            runtime.metadata_ = metadata
        delta = max(0, seen_count - baseline)
        pending_cursor = GraphEdgeRuntimeCursor(
            project_id=project_id,
            source_id=key[0],
            target_id=key[1],
            kind=key[2],
            observation_id=observation_id,
            applied_seen_count=seen_count,
        )
        changed = True
    else:
        applied = max(0, int(cursor.applied_seen_count))
        delta = max(0, seen_count - applied)
        if seen_count > applied:
            cursor.applied_seen_count = seen_count
            cursor.updated_at = datetime.now(tz=timezone.utc)
            changed = True

    if runtime is None:
        runtime = GraphEdgeRuntimeOverlay(
            project_id=project_id,
            source_id=key[0],
            target_id=key[1],
            kind=key[2],
            first_seen_at=first,
            last_seen_at=last,
            hit_count=delta,
            metadata_={"source": "otlp"},
        )
        session.add(runtime)
        changed = True
        # The cursor has a composite FK to this logical overlay.  Flush the
        # parent explicitly before a later query can autoflush the cursor;
        # this also behaves consistently on SQLite, whose UoW ordering does
        # not infer dependencies between otherwise unrelated ORM objects.
        await session.flush([runtime])
    else:
        next_first = min(_as_utc(runtime.first_seen_at), first)
        next_last = max(_as_utc(runtime.last_seen_at), last)
        next_hits = int(runtime.hit_count) + delta
        if (
            next_first != _as_utc(runtime.first_seen_at)
            or next_last != _as_utc(runtime.last_seen_at)
            or next_hits != int(runtime.hit_count)
        ):
            runtime.first_seen_at = next_first
            runtime.last_seen_at = next_last
            runtime.hit_count = next_hits
            runtime.updated_at = datetime.now(tz=timezone.utc)
            changed = True

    if pending_cursor is not None:
        session.add(pending_cursor)

    human = (
        await session.execute(
            select(GraphEdgeHumanOverlay).where(
                GraphEdgeHumanOverlay.project_id == project_id,
                GraphEdgeHumanOverlay.source_id == key[0],
                GraphEdgeHumanOverlay.target_id == key[1],
                GraphEdgeHumanOverlay.kind == key[2],
            )
        )
    ).scalar_one_or_none()
    materialized = materialize_edge_data(edge.data, human, runtime)
    if materialized != (edge.data or {}):
        edge.data = materialized
        changed = True
    await session.flush()
    return RuntimeOverlayWriteResult(
        overlay=runtime,
        changed=changed,
        new_hits=delta,
    )
