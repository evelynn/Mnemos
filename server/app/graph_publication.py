"""Run-isolated graph staging and atomic graph publication.

``run_ingest`` writes analyzer candidates through the stage writers in this
module and publishes them only after producer coverage has been sealed.  The
contract keeps half-written Node/Edge generations invisible:

* analyzer output is committed only to ``graph_*_stage`` rows keyed by run;
* a run persists its base generation and sealed producer coverage before promote;
* one publication transaction reserves that base generation with a CAS;
* changed versions, authoritative deletions, the graph-head swap, receipt, and
  the ``AnalysisRun.status='published'`` marker commit together;
* a failed transaction leaves the prior current graph and head unchanged;
* retry after a committed promotion is idempotent by ``current_run_id``.

Migration 0027 bootstraps legacy projects as ``needs_rebuild`` because rows
written before this contract cannot be proven atomic.  Such a graph remains
fail-closed until one complete staged rebuild is promoted.

The transaction is bounded in memory, not yet bounded in database work. When
sealed deletion authority is non-empty, omission reconciliation scans the
project's current graph inside the transaction and is O(graph). A precomputed
deletion-intent/contribution design is required before describing promotion as
short at 50 K-file scale.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Any, Iterable, Mapping, TypeVar

from sqlalchemy import and_, delete, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph_overlays import (
    load_edge_overlays,
    load_node_human_overlays,
    materialize_edge_data,
    materialize_node_data,
    strip_durable_overlay_fields,
)
from app.models.graph import (
    ANALYSIS_RUN_SOURCE_PUBLISHED_STATUSES,
    ANALYSIS_RUN_TERMINAL_STATUSES,
    AnalysisRun,
    Edge,
    GraphEdgeStage,
    GraphHead,
    GraphNodeStage,
    Node,
)

GRAPH_PUBLICATION_CONTRACT = "mnemos.graph-publication.v1"
GRAPH_HEAD_NEEDS_REBUILD = "needs_rebuild"
GRAPH_HEAD_READY = "ready"

_CERTAINTY_RANK = {"inferred": 0, "asserted": 1, "verified": 2}
_NODE_BATCH = 250
_EDGE_BATCH = 150  # three-column tuple predicates stay below old SQLite limits
_STAGE_NODE_BATCH = 250
_STAGE_EDGE_BATCH = 150
_CLEANABLE_RUN_STATUSES = ANALYSIS_RUN_TERMINAL_STATUSES | {"published"}

_StageCandidateT = TypeVar("_StageCandidateT")


class GraphPublicationError(RuntimeError):
    """Base class for a promotion that was rejected before publication."""


class GraphPublicationUsageError(GraphPublicationError):
    """The caller violated the transaction/phase contract."""


class GraphHeadMissing(GraphPublicationError):
    """The project has no explicit fail-closed publication head."""


class GraphHeadNeedsRebuild(GraphPublicationError):
    """A legacy/mixed graph needs one staged full rebuild before publication."""


class StaleGraphGeneration(GraphPublicationError):
    """Another run published after this run captured its base generation."""


class GraphPublicationInvariantError(GraphPublicationError):
    """Stored staging/current rows violate an invariant required for promotion."""


class MultiProducerContributionRequired(GraphPublicationError):
    """A stage cannot replace multi-owner data without complete authority."""


class ActiveStagingCleanupRejected(GraphPublicationError):
    """Staging for a non-terminal run cannot be removed safely."""


class GraphCoverageNotSealed(GraphPublicationError):
    """A run has no immutable persisted producer-coverage decision."""


class GraphCoverageAlreadySealed(GraphPublicationError):
    """A caller tried to change a run's sealed producer coverage."""


class IncompleteFullRebuildCoverage(GraphPublicationError):
    """A full rebuild cannot account for every legacy current fact owner."""


class GraphGenerationChanged(GraphPublicationError):
    """A multi-statement reader crossed a graph publication boundary."""


class LegacyGraphWriteRejected(GraphPublicationError):
    """A production ingest attempted to bypass run-scoped staging."""


class StagedIdentityConflict(GraphPublicationError):
    """Two producers emitted different payloads for one staged identity."""


@dataclass(frozen=True)
class PromotionCounts:
    nodes_inserted: int = 0
    nodes_closed: int = 0
    nodes_unchanged: int = 0
    nodes_downgrade_ignored: int = 0
    edges_inserted: int = 0
    edges_closed: int = 0
    edges_unchanged: int = 0
    edges_downgrade_ignored: int = 0
    stale_nodes_closed: int = 0
    stale_edges_closed: int = 0


def _promotion_counts_payload(counts: PromotionCounts) -> dict[str, int]:
    return {
        name: int(getattr(counts, name))
        for name in PromotionCounts.__dataclass_fields__
    }


def _promotion_counts_from_receipt(value: Any) -> PromotionCounts:
    if not isinstance(value, dict):
        raise GraphPublicationInvariantError(
            "publication receipt is missing promotion counts"
        )
    expected = set(PromotionCounts.__dataclass_fields__)
    if set(value) != expected:
        raise GraphPublicationInvariantError(
            "publication receipt promotion counts have an invalid shape"
        )
    if any(type(value[name]) is not int for name in expected):
        raise GraphPublicationInvariantError(
            "publication receipt promotion counts must be real integers"
        )
    normalized = {name: value[name] for name in expected}
    if any(item < 0 for item in normalized.values()):
        raise GraphPublicationInvariantError(
            "publication receipt promotion counts must be non-negative"
        )
    return PromotionCounts(**normalized)


@dataclass(frozen=True)
class GraphPublicationReceipt:
    """Canonical, validated form of one immutable publication receipt."""

    generation: int
    base_generation: int
    published_at: datetime
    authoritative_sources: tuple[str, ...]
    coverage_sealed_at: datetime
    counts: PromotionCounts

    def to_payload(self) -> dict[str, Any]:
        """Return the one persisted v1 representation without ORM state."""

        return {
            "contract_version": GRAPH_PUBLICATION_CONTRACT,
            "generation": self.generation,
            "base_generation": self.base_generation,
            "published_at": self.published_at.isoformat(),
            "authoritative_sources": list(self.authoritative_sources),
            "coverage_sealed_at": self.coverage_sealed_at.isoformat(),
            "counts": _promotion_counts_payload(self.counts),
            "atomic": True,
        }


@dataclass(frozen=True)
class GraphPromotionResult:
    project_id: uuid.UUID
    run_id: uuid.UUID
    generation: int
    published_at: datetime
    idempotent: bool
    counts: PromotionCounts


@dataclass(frozen=True)
class StageCleanupResult:
    project_id: uuid.UUID
    run_id: uuid.UUID
    nodes_deleted: int
    edges_deleted: int


@dataclass(frozen=True)
class GraphReadStamp:
    """One source+overlay head value for reader revalidation."""

    project_id: uuid.UUID
    generation: int
    overlay_generation: int
    current_run_id: uuid.UUID
    published_at: datetime


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _semantic_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def node_semantic_hash(
    *,
    kind: str,
    data: dict[str, Any],
    certainty: str,
    created_by: Iterable[str],
) -> str:
    """Stable digest used to compare one logical node version portably."""

    return _semantic_hash(
        {
            "kind": kind,
            "data": data,
            "certainty": certainty,
            "created_by": list(created_by),
        }
    )


def edge_semantic_hash(
    *,
    source_id: str,
    target_id: str,
    kind: str,
    data: dict[str, Any],
    certainty: str,
    created_by: Iterable[str],
) -> str:
    """Stable digest for an edge's logical identity and semantic payload."""

    return _semantic_hash(
        {
            "source_id": source_id,
            "target_id": target_id,
            "kind": kind,
            "data": data,
            "certainty": certainty,
            "created_by": list(created_by),
        }
    )


@dataclass(frozen=True, slots=True, init=False)
class StagedNodeCandidate:
    """Immutable input for one run-scoped staged node.

    The payload is captured as canonical JSON instead of retaining the caller's
    mutable dictionary.  ``data`` returns a fresh dictionary on every access,
    so buffering a candidate cannot make staging depend on a later caller-side
    mutation.
    """

    node_id: str
    kind: str
    certainty: str
    source_name: str
    _data_json: str

    def __init__(
        self,
        *,
        node_id: str,
        kind: str,
        data: Mapping[str, Any],
        certainty: str,
        source_name: str,
    ) -> None:
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "certainty", certainty)
        object.__setattr__(self, "source_name", source_name)
        object.__setattr__(self, "_data_json", _canonical_json(dict(data)))

    @property
    def data(self) -> dict[str, Any]:
        value = json.loads(self._data_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise GraphPublicationInvariantError(
                "staged node candidate data must be a JSON object"
            )
        return value


@dataclass(frozen=True, slots=True, init=False)
class StagedEdgeCandidate:
    """Immutable input for one run-scoped staged edge."""

    source_id: str
    target_id: str
    kind: str
    certainty: str
    source_name: str
    _data_json: str

    def __init__(
        self,
        *,
        source_id: str,
        target_id: str,
        kind: str,
        data: Mapping[str, Any],
        certainty: str,
        source_name: str,
    ) -> None:
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "certainty", certainty)
        object.__setattr__(self, "source_name", source_name)
        object.__setattr__(self, "_data_json", _canonical_json(dict(data)))

    @property
    def data(self) -> dict[str, Any]:
        value = json.loads(self._data_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise GraphPublicationInvariantError(
                "staged edge candidate data must be a JSON object"
            )
        return value


def _bounded_stage_input(
    values: Iterable[_StageCandidateT],
    *,
    limit: int,
    label: str,
) -> tuple[_StageCandidateT, ...]:
    """Consume at most ``limit + 1`` values and reject oversized input."""

    batch = tuple(islice(iter(values), limit + 1))
    if len(batch) > limit:
        raise ValueError(f"{label} staging batch exceeds {limit} candidates")
    return batch


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # SQLite's DateTime adapter commonly returns a naive value even when
        # ``timezone=True``.  Stored publication timestamps are UTC by contract.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _next_publication_time(
    previous: datetime | None,
    requested: datetime | None,
    *,
    not_before: Iterable[datetime | None] = (),
    strictly_after: Iterable[datetime | None] = (),
) -> datetime:
    candidate = _as_utc(requested or datetime.now(tz=timezone.utc))
    for floor in not_before:
        if floor is not None:
            candidate = max(candidate, _as_utc(floor))
    for floor in (previous, *strictly_after):
        if floor is not None and candidate <= _as_utc(floor):
            candidate = _as_utc(floor) + timedelta(microseconds=1)
    return candidate


def _would_downgrade(current: str, incoming: str) -> bool:
    return _CERTAINTY_RANK.get(incoming, -1) < _CERTAINTY_RANK.get(current, -1)


def _owners(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    return {str(item) for item in (value or [])}


def _canonical_sources(values: Iterable[str]) -> list[str]:
    return sorted(
        {
            str(value).strip()
            for value in values
            if value is not None and str(value).strip()
        }
    )


_GRAPH_PUBLICATION_RECEIPT_KEYS = frozenset(
    {
        "contract_version",
        "generation",
        "base_generation",
        "published_at",
        "authoritative_sources",
        "coverage_sealed_at",
        "counts",
        "atomic",
    }
)


def _parse_receipt_utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise GraphPublicationInvariantError(f"{field} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GraphPublicationInvariantError(
            f"{field} timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GraphPublicationInvariantError(
            f"{field} timestamp must be timezone-aware"
        )
    if parsed.utcoffset() != timedelta(0):
        raise GraphPublicationInvariantError(f"{field} timestamp must be UTC")
    return parsed.astimezone(timezone.utc)


def validate_graph_publication_receipt(
    run: AnalysisRun,
    *,
    head: GraphHead | None = None,
) -> GraphPublicationReceipt:
    """Validate one v1 receipt against immutable run state and an optional head.

    This is the single pure contract boundary for every publication consumer.
    It deliberately validates persisted ``AnalysisRun`` coverage fields rather
    than trusting their duplicated JSON representation.  When ``head`` is
    supplied, the source pointer must match the same project, run, generation,
    and publication timestamp exactly.
    """

    if run.status not in ANALYSIS_RUN_SOURCE_PUBLISHED_STATUSES:
        raise GraphPublicationInvariantError(
            f"run status {run.status!r} is not a source publication"
        )
    if run.scope not in {"full", "incremental"}:
        raise GraphPublicationInvariantError(
            f"published run scope {run.scope!r} is invalid"
        )
    if not isinstance(run.started_at, datetime):
        raise GraphPublicationInvariantError(
            "source publication has no started_at"
        )

    stats = run.stats if isinstance(run.stats, dict) else None
    receipt = stats.get("graph_publication") if stats is not None else None
    if not isinstance(receipt, dict):
        raise GraphPublicationInvariantError(
            "graph_publication receipt is missing"
        )
    if receipt.get("contract_version") != GRAPH_PUBLICATION_CONTRACT:
        raise GraphPublicationInvariantError(
            "graph_publication contract_version is invalid"
        )
    if receipt.get("atomic") is not True:
        raise GraphPublicationInvariantError(
            "graph_publication receipt is not atomic"
        )

    base_generation = run.graph_base_generation
    if type(base_generation) is not int or base_generation < 0:
        raise GraphPublicationInvariantError(
            "persisted base generation is invalid"
        )
    receipt_base_generation = receipt.get("base_generation")
    generation = receipt.get("generation")
    if (
        type(receipt_base_generation) is not int
        or receipt_base_generation != base_generation
        or type(generation) is not int
        or generation != base_generation + 1
    ):
        raise GraphPublicationInvariantError(
            "publication generation does not match the persisted base"
        )

    persisted_sources = run.graph_authoritative_sources
    if not isinstance(persisted_sources, list) or any(
        type(source) is not str or not source.strip()
        for source in persisted_sources
    ):
        raise GraphPublicationInvariantError(
            "persisted authoritative sources are missing"
        )
    canonical_sources = sorted(set(persisted_sources))
    if persisted_sources != canonical_sources:
        raise GraphPublicationInvariantError(
            "persisted authoritative sources are not canonical"
        )
    if receipt.get("authoritative_sources") != canonical_sources:
        raise GraphPublicationInvariantError(
            "receipt authoritative sources do not match the run"
        )

    persisted_sealed_at = run.graph_coverage_sealed_at
    if not isinstance(persisted_sealed_at, datetime):
        raise GraphPublicationInvariantError(
            "publication coverage seal is missing"
        )
    coverage_sealed_at = _parse_receipt_utc_timestamp(
        receipt.get("coverage_sealed_at"),
        field="coverage seal",
    )
    if coverage_sealed_at != _as_utc(persisted_sealed_at):
        raise GraphPublicationInvariantError(
            "receipt coverage seal does not match the run"
        )
    if _as_utc(run.started_at) > coverage_sealed_at:
        raise GraphPublicationInvariantError(
            "coverage seal precedes the run start"
        )

    published_at = _parse_receipt_utc_timestamp(
        receipt.get("published_at"),
        field="publication",
    )
    if coverage_sealed_at > published_at:
        raise GraphPublicationInvariantError(
            "publication timestamp precedes the coverage seal"
        )
    counts = _promotion_counts_from_receipt(receipt.get("counts"))
    if set(receipt) != _GRAPH_PUBLICATION_RECEIPT_KEYS:
        raise GraphPublicationInvariantError(
            "graph_publication receipt has an invalid shape"
        )

    if run.status == "published":
        if run.completed_at is not None:
            raise GraphPublicationInvariantError(
                "published run must not have completed_at"
            )
    else:
        if not isinstance(run.completed_at, datetime):
            raise GraphPublicationInvariantError(
                "terminal source publication has no completed_at"
            )
        if _as_utc(run.completed_at) < published_at:
            raise GraphPublicationInvariantError(
                "terminal completed_at precedes source publication"
            )

    if head is not None:
        if head.state != GRAPH_HEAD_READY:
            raise GraphPublicationInvariantError(
                "publication head is not ready"
            )
        if head.project_id != run.project_id:
            raise GraphPublicationInvariantError(
                "publication head project does not match the run"
            )
        if head.current_run_id != run.id:
            raise GraphPublicationInvariantError(
                "publication head does not point at the run"
            )
        if type(head.generation) is not int or head.generation != generation:
            raise GraphPublicationInvariantError(
                "publication head generation does not match the receipt"
            )
        if not isinstance(head.published_at, datetime):
            raise GraphPublicationInvariantError(
                "publication head timestamp is missing"
            )
        if _as_utc(head.published_at) != published_at:
            raise GraphPublicationInvariantError(
                "publication head timestamp does not match the receipt"
            )

    return GraphPublicationReceipt(
        generation=generation,
        base_generation=base_generation,
        published_at=published_at,
        authoritative_sources=tuple(canonical_sources),
        coverage_sealed_at=coverage_sealed_at,
        counts=counts,
    )


async def _lock_open_stage_run(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
) -> None:
    """Fence staging writes against the coverage-seal transaction.

    Every database transaction that stages facts locks the owning run before
    its first write. ``seal_graph_coverage`` takes ``FOR UPDATE`` on the same
    row, so it waits for earlier writers and causes every later writer to see
    the immutable seal and fail.  The transaction identity cache avoids one
    lock query per JSONL record while preserving the barrier across commits.
    """

    transaction = session.get_transaction()
    cache_key = (project_id, run_id)
    cached = session.info.get("mnemos_graph_stage_lock")
    if (
        transaction is not None
        and isinstance(cached, tuple)
        and len(cached) == 2
        and cached[0] is transaction
        and cached[1] == cache_key
    ):
        return
    run = (
        await session.execute(
            select(AnalysisRun)
            .where(
                AnalysisRun.id == run_id,
                AnalysisRun.project_id == project_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        raise GraphPublicationInvariantError(
            "staging run does not belong to the graph project"
        )
    if run.status != "running":
        raise GraphPublicationInvariantError(
            f"only a running AnalysisRun can stage graph facts (status={run.status})"
        )
    if run.graph_base_generation is None:
        raise GraphPublicationInvariantError(
            "capture graph_base_generation before staging graph facts"
        )
    if run.graph_coverage_sealed_at is not None:
        raise GraphCoverageAlreadySealed(
            "graph coverage is sealed; late staging writes are rejected"
        )
    transaction = session.get_transaction()
    session.info["mnemos_graph_stage_lock"] = (
        transaction,
        cache_key,
    )


def _merge_staged_node_candidate(
    current: GraphNodeStage | None,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate: StagedNodeCandidate,
) -> tuple[GraphNodeStage, bool]:
    """Apply the historical singleton node reducer to one in-memory row."""

    data = strip_durable_overlay_fields(candidate.data, target_kind="node")
    incoming_hash = node_semantic_hash(
        kind=candidate.kind,
        data=data,
        certainty=candidate.certainty,
        created_by=[candidate.source_name],
    )
    if current is None:
        return (
            GraphNodeStage(
                run_id=run_id,
                project_id=project_id,
                node_id=candidate.node_id,
                kind=candidate.kind,
                data=dict(data),
                certainty=candidate.certainty,
                source_name=candidate.source_name,
                source_names=[candidate.source_name],
                semantic_hash=incoming_hash,
            ),
            True,
        )
    if current.project_id != project_id:
        raise GraphPublicationInvariantError(
            "staged node identity already belongs to a different project"
        )
    owners = _canonical_sources(current.source_names or [current.source_name])
    if current.semantic_hash == incoming_hash:
        return current, False
    same_payload = (
        current.kind == candidate.kind
        and _canonical_json(current.data) == _canonical_json(data)
        and current.certainty == candidate.certainty
    )
    if same_payload:
        merged_owners = _canonical_sources([*owners, candidate.source_name])
        merged_hash = node_semantic_hash(
            kind=candidate.kind,
            data=data,
            certainty=candidate.certainty,
            created_by=merged_owners,
        )
        if current.semantic_hash == merged_hash:
            return current, False
        current.source_name = merged_owners[0]
        current.source_names = merged_owners
        current.semantic_hash = merged_hash
        return current, True
    if set(owners) != {candidate.source_name}:
        raise StagedIdentityConflict(
            f"conflicting staged node payload for identity {candidate.node_id!r}"
        )
    if _would_downgrade(current.certainty, candidate.certainty):
        return current, False
    current.kind = candidate.kind
    current.data = dict(data)
    current.certainty = candidate.certainty
    current.source_name = candidate.source_name
    current.source_names = [candidate.source_name]
    current.semantic_hash = incoming_hash
    return current, True


def _merge_staged_edge_candidate(
    current: GraphEdgeStage | None,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate: StagedEdgeCandidate,
) -> tuple[GraphEdgeStage, bool]:
    """Apply the historical singleton edge reducer to one in-memory row."""

    data = strip_durable_overlay_fields(candidate.data, target_kind="edge")
    incoming_hash = edge_semantic_hash(
        source_id=candidate.source_id,
        target_id=candidate.target_id,
        kind=candidate.kind,
        data=data,
        certainty=candidate.certainty,
        created_by=[candidate.source_name],
    )
    if current is None:
        return (
            GraphEdgeStage(
                run_id=run_id,
                project_id=project_id,
                source_id=candidate.source_id,
                target_id=candidate.target_id,
                kind=candidate.kind,
                data=dict(data),
                certainty=candidate.certainty,
                source_name=candidate.source_name,
                source_names=[candidate.source_name],
                semantic_hash=incoming_hash,
            ),
            True,
        )
    if current.project_id != project_id:
        raise GraphPublicationInvariantError(
            "staged edge identity already belongs to a different project"
        )
    owners = _canonical_sources(current.source_names or [current.source_name])
    if current.semantic_hash == incoming_hash:
        return current, False
    same_payload = (
        _canonical_json(current.data) == _canonical_json(data)
        and current.certainty == candidate.certainty
    )
    if same_payload:
        merged_owners = _canonical_sources([*owners, candidate.source_name])
        merged_hash = edge_semantic_hash(
            source_id=candidate.source_id,
            target_id=candidate.target_id,
            kind=candidate.kind,
            data=data,
            certainty=candidate.certainty,
            created_by=merged_owners,
        )
        if current.semantic_hash == merged_hash:
            return current, False
        current.source_name = merged_owners[0]
        current.source_names = merged_owners
        current.semantic_hash = merged_hash
        return current, True
    if set(owners) != {candidate.source_name}:
        raise StagedIdentityConflict(
            "conflicting staged edge payload for identity "
            f"{(candidate.source_id, candidate.target_id, candidate.kind)!r}"
        )
    if _would_downgrade(current.certainty, candidate.certainty):
        return current, False
    current.data = dict(data)
    current.certainty = candidate.certainty
    current.source_name = candidate.source_name
    current.source_names = [candidate.source_name]
    current.semantic_hash = incoming_hash
    return current, True


async def stage_nodes_batch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidates: Iterable[StagedNodeCandidate],
) -> tuple[bool, ...]:
    """Stage a bounded node batch with one lock and one identity read.

    Reducer outcomes match sequential :func:`stage_node` calls in input order.
    The caller owns flush/commit/rollback, as it did for the singleton writer.
    """

    batch = _bounded_stage_input(
        candidates, limit=_STAGE_NODE_BATCH, label="node"
    )
    if not batch:
        return ()
    await _lock_open_stage_run(
        session, project_id=project_id, run_id=run_id
    )
    node_ids = list(dict.fromkeys(candidate.node_id for candidate in batch))
    current_rows = (
        await session.execute(
            select(GraphNodeStage).where(
                GraphNodeStage.run_id == run_id,
                GraphNodeStage.node_id.in_(node_ids),
            )
        )
    ).scalars().all()
    current_by_id: dict[str, GraphNodeStage] = {}
    for row in current_rows:
        if row.node_id in current_by_id:
            raise GraphPublicationInvariantError(
                f"duplicate staged node identity: {row.node_id!r}"
            )
        current_by_id[row.node_id] = row

    changed: list[bool] = []
    for candidate in batch:
        current = current_by_id.get(candidate.node_id)
        merged, did_change = _merge_staged_node_candidate(
            current,
            project_id=project_id,
            run_id=run_id,
            candidate=candidate,
        )
        if current is None:
            session.add(merged)
            current_by_id[candidate.node_id] = merged
        changed.append(did_change)
    return tuple(changed)


async def stage_edges_batch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidates: Iterable[StagedEdgeCandidate],
) -> tuple[bool, ...]:
    """Stage a bounded edge batch with one lock and one identity read."""

    batch = _bounded_stage_input(
        candidates, limit=_STAGE_EDGE_BATCH, label="edge"
    )
    if not batch:
        return ()
    await _lock_open_stage_run(
        session, project_id=project_id, run_id=run_id
    )
    keys = list(
        dict.fromkeys(
            (candidate.source_id, candidate.target_id, candidate.kind)
            for candidate in batch
        )
    )
    current_rows = (
        await session.execute(
            select(GraphEdgeStage).where(
                GraphEdgeStage.run_id == run_id,
                tuple_(
                    GraphEdgeStage.source_id,
                    GraphEdgeStage.target_id,
                    GraphEdgeStage.kind,
                ).in_(keys),
            )
        )
    ).scalars().all()
    current_by_key: dict[tuple[str, str, str], GraphEdgeStage] = {}
    for row in current_rows:
        key = (row.source_id, row.target_id, row.kind)
        if key in current_by_key:
            raise GraphPublicationInvariantError(
                f"duplicate staged edge identity: {key!r}"
            )
        current_by_key[key] = row

    changed: list[bool] = []
    for candidate in batch:
        key = (candidate.source_id, candidate.target_id, candidate.kind)
        current = current_by_key.get(key)
        merged, did_change = _merge_staged_edge_candidate(
            current,
            project_id=project_id,
            run_id=run_id,
            candidate=candidate,
        )
        if current is None:
            session.add(merged)
            current_by_key[key] = merged
        changed.append(did_change)
    return tuple(changed)


async def stage_node(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str,
    kind: str,
    data: dict[str, Any],
    certainty: str,
    source_name: str,
) -> bool:
    """Materialize one run-scoped node candidate without touching ``nodes``."""

    result = await stage_nodes_batch(
        session,
        project_id=project_id,
        run_id=run_id,
        candidates=(
            StagedNodeCandidate(
                node_id=node_id,
                kind=kind,
                data=data,
                certainty=certainty,
                source_name=source_name,
            ),
        ),
    )
    return result[0]


async def stage_edge(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    source_id: str,
    target_id: str,
    kind: str,
    data: dict[str, Any],
    certainty: str,
    source_name: str,
) -> bool:
    """Materialize one run-scoped edge candidate without touching ``edges``."""

    result = await stage_edges_batch(
        session,
        project_id=project_id,
        run_id=run_id,
        candidates=(
            StagedEdgeCandidate(
                source_id=source_id,
                target_id=target_id,
                kind=kind,
                data=data,
                certainty=certainty,
                source_name=source_name,
            ),
        ),
    )
    return result[0]


async def bootstrap_graph_head(
    session: AsyncSession, *, project_id: uuid.UUID
) -> GraphHead:
    """Create a fail-closed head for a newly-created project.

    This helper never infers that an existing graph is clean.  Both migrated
    and newly-created projects begin at generation zero in ``needs_rebuild``.
    The caller owns the transaction. Project creation and ``run_ingest`` call
    this before the project's first staged run.
    """

    head = await session.get(GraphHead, project_id)
    if head is not None:
        return head
    head = GraphHead(
        project_id=project_id,
        current_run_id=None,
        generation=0,
        state=GRAPH_HEAD_NEEDS_REBUILD,
        published_at=None,
    )
    session.add(head)
    await session.flush()
    return head


async def capture_graph_base_generation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
) -> int:
    """Persist the head generation a run will stage against, exactly once.

    The caller owns the transaction and must call this before any staging
    write.  Repeated calls return the original value even if another run has
    since published; a stale run can therefore never be rebased by changing a
    retry-time function argument.
    """

    head = (
        await session.execute(
            select(GraphHead)
            .where(GraphHead.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if head is None:
        raise GraphHeadMissing(
            "graph head missing; bootstrap it as needs_rebuild first"
        )
    run = (
        await session.execute(
            select(AnalysisRun)
            .where(
                AnalysisRun.id == run_id,
                AnalysisRun.project_id == project_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        raise GraphPublicationInvariantError(
            "AnalysisRun does not belong to the graph project"
        )
    if run.graph_base_generation is not None:
        return int(run.graph_base_generation)
    if run.status not in {"queued", "running"}:
        raise GraphPublicationInvariantError(
            "only a queued/running AnalysisRun can capture a graph generation"
        )
    staged_node = (
        await session.execute(
            select(GraphNodeStage.run_id)
            .where(GraphNodeStage.run_id == run_id)
            .limit(1)
        )
    ).first()
    staged_edge = (
        await session.execute(
            select(GraphEdgeStage.run_id)
            .where(GraphEdgeStage.run_id == run_id)
            .limit(1)
        )
    ).first()
    if staged_node is not None or staged_edge is not None:
        raise GraphPublicationInvariantError(
            "capture graph_base_generation before the first staging write"
        )
    run.graph_base_generation = int(head.generation)
    await session.flush()
    return int(run.graph_base_generation)


async def seal_graph_coverage(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    authoritative_sources: Iterable[str],
) -> tuple[str, ...]:
    """Persist one write-once producer-coverage decision for a staged run.

    ``run_ingest`` calls this only after it has verified complete terminal
    coverage for each supplied producer.  Promotion deliberately has no
    caller-provided deletion-authority argument; it consumes this sealed row
    state instead.  Re-sealing with the identical canonical set is idempotent,
    while changing it fails closed. This helper records the trusted
    internal decision; it does not manufacture or cryptographically verify an
    analyzer terminal-coverage proof.
    """

    sources = _canonical_sources(authoritative_sources)
    run = (
        await session.execute(
            select(AnalysisRun)
            .where(
                AnalysisRun.id == run_id,
                AnalysisRun.project_id == project_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        raise GraphPublicationInvariantError(
            "AnalysisRun does not belong to the graph project"
        )
    if run.graph_base_generation is None:
        raise GraphPublicationInvariantError(
            "capture graph_base_generation before sealing coverage"
        )
    if run.graph_coverage_sealed_at is not None:
        existing = _canonical_sources(run.graph_authoritative_sources or [])
        if existing != sources:
            raise GraphCoverageAlreadySealed(
                "authoritative producer coverage is already sealed"
            )
        session.info.pop("mnemos_graph_stage_lock", None)
        return tuple(existing)
    if run.status != "running":
        raise GraphPublicationInvariantError(
            f"only a running AnalysisRun can seal coverage (status={run.status})"
        )
    run.graph_authoritative_sources = sources
    run.graph_coverage_sealed_at = datetime.now(tz=timezone.utc)
    # The same transaction may have staged rows before taking the seal lock.
    # Invalidate its writer fast-path so a subsequent write rechecks the now
    # immutable seal instead of trusting the pre-seal transaction cache.
    session.info.pop("mnemos_graph_stage_lock", None)
    await session.flush()
    return tuple(sources)


async def read_graph_stamp(
    session: AsyncSession, *, project_id: uuid.UUID
) -> GraphReadStamp:
    """Read a ready head for a multi-statement consumer to pin and recheck.

    Under PostgreSQL READ COMMITTED a consumer must revalidate after
    its final graph query (or use a repeatable-read transaction), otherwise a
    source-publication or overlay commit can fall between statements.
    """

    row = (
        await session.execute(
            select(GraphHead, AnalysisRun)
            .outerjoin(
                AnalysisRun,
                and_(
                    AnalysisRun.id == GraphHead.current_run_id,
                    AnalysisRun.project_id == GraphHead.project_id,
                ),
            )
            .where(GraphHead.project_id == project_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise GraphHeadMissing("graph head missing")
    head, run = row
    if head.state != GRAPH_HEAD_READY:
        raise GraphHeadNeedsRebuild("graph head is not ready")
    if run is None:
        raise GraphPublicationInvariantError(
            "ready graph head has no matching AnalysisRun"
        )
    receipt = validate_graph_publication_receipt(run, head=head)
    if (
        type(head.overlay_generation) is not int
        or head.overlay_generation < 0
    ):
        raise GraphPublicationInvariantError(
            "graph head overlay generation is invalid"
        )
    return GraphReadStamp(
        project_id=project_id,
        generation=receipt.generation,
        overlay_generation=head.overlay_generation,
        current_run_id=(
            run.id
            if isinstance(run.id, uuid.UUID)
            else uuid.UUID(str(run.id))
        ),
        published_at=receipt.published_at,
    )


async def lock_validated_graph_head(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> tuple[GraphHead, GraphPublicationReceipt]:
    """Lock one ready head and prove its run receipt in the same transaction."""

    head = (
        await session.execute(
            select(GraphHead)
            .where(GraphHead.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if head is None:
        raise GraphHeadMissing("graph head missing")
    if head.state != GRAPH_HEAD_READY:
        raise GraphHeadNeedsRebuild("graph head is not ready")
    run = (
        await session.execute(
            select(AnalysisRun)
            .where(
                AnalysisRun.id == head.current_run_id,
                AnalysisRun.project_id == project_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        raise GraphPublicationInvariantError(
            "ready graph head has no matching AnalysisRun"
        )
    receipt = validate_graph_publication_receipt(run, head=head)
    if (
        type(head.overlay_generation) is not int
        or head.overlay_generation < 0
    ):
        raise GraphPublicationInvariantError(
            "graph head overlay generation is invalid"
        )
    return head, receipt


async def revalidate_graph_stamp(
    session: AsyncSession, *, stamp: GraphReadStamp
) -> None:
    """Fail if source or overlay revision changed during graph queries."""

    current = await read_graph_stamp(session, project_id=stamp.project_id)
    if current != stamp:
        raise GraphGenerationChanged(
            "graph revision changed during the read: "
            f"source {stamp.generation}->{current.generation}, overlay "
            f"{stamp.overlay_generation}->{current.overlay_generation}"
        )


async def _reject_cross_project_stage(
    session: AsyncSession, *, project_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    wrong_node = (
        await session.execute(
            select(GraphNodeStage.run_id)
            .where(
                GraphNodeStage.run_id == run_id,
                GraphNodeStage.project_id != project_id,
            )
            .limit(1)
        )
    ).first()
    wrong_edge = (
        await session.execute(
            select(GraphEdgeStage.run_id)
            .where(
                GraphEdgeStage.run_id == run_id,
                GraphEdgeStage.project_id != project_id,
            )
            .limit(1)
        )
    ).first()
    if wrong_node is not None or wrong_edge is not None:
        raise GraphPublicationInvariantError(
            "staged facts must belong to the AnalysisRun project"
        )


async def _validate_full_rebuild_coverage(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    source_names: frozenset[str],
) -> datetime | None:
    """Prove a needs-rebuild promotion can reconcile every legacy fact.

    A full stage is not a rebuild merely because ``AnalysisRun.scope`` says
    ``full``.  Every owner on every legacy current row must be included in
    persisted sealed coverage. Additive partial producers may still stage new
    facts, but they receive no deletion authority. The returned timestamp is
    the newest legacy version boundary, used to avoid closing a row before its
    own ``valid_from`` when clocks or imported data are skewed.
    """

    if not source_names:
        current_node = (
            await session.execute(
                select(Node.id)
                .where(Node.project_id == project_id, Node.valid_to.is_(None))
                .limit(1)
            )
        ).first()
        current_edge = (
            await session.execute(
                select(Edge.id)
                .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
                .limit(1)
            )
        ).first()
        staged_node = (
            await session.execute(
                select(GraphNodeStage.node_id)
                .where(
                    GraphNodeStage.project_id == project_id,
                    GraphNodeStage.run_id == run_id,
                )
                .limit(1)
            )
        ).first()
        staged_edge = (
            await session.execute(
                select(GraphEdgeStage.run_id)
                .where(
                    GraphEdgeStage.project_id == project_id,
                    GraphEdgeStage.run_id == run_id,
                )
                .limit(1)
            )
        ).first()
        if any((current_node, current_edge, staged_node, staged_edge)):
            raise IncompleteFullRebuildCoverage(
                "a non-empty needs_rebuild graph requires non-empty sealed "
                "producer coverage"
            )
        # An authoritatively-scanned empty project is a valid empty snapshot.
        return None
    newest: datetime | None = None
    last_node: tuple[str, datetime] | None = None
    while True:
        stmt = (
            select(Node.id, Node.valid_from, Node.created_by)
            .where(Node.project_id == project_id, Node.valid_to.is_(None))
            .order_by(Node.id, Node.valid_from)
            .limit(_NODE_BATCH)
        )
        if last_node is not None:
            last_id, last_valid_from = last_node
            stmt = stmt.where(
                or_(
                    Node.id > last_id,
                    and_(Node.id == last_id, Node.valid_from > last_valid_from),
                )
            )
        rows = (await session.execute(stmt)).all()
        if not rows:
            break
        for node_id, valid_from, created_by in rows:
            owners = _owners(created_by)
            missing = owners - source_names
            if not owners or missing:
                detail = ", ".join(sorted(missing)) if missing else "<missing>"
                raise IncompleteFullRebuildCoverage(
                    f"legacy node {node_id!r} has uncovered owners: {detail}"
                )
            aware = _as_utc(valid_from)
            newest = aware if newest is None else max(newest, aware)
        last_node = (rows[-1][0], rows[-1][1])

    last_edge: tuple[uuid.UUID, datetime] | None = None
    while True:
        stmt = (
            select(Edge.id, Edge.valid_from, Edge.created_by)
            .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
            .order_by(Edge.id, Edge.valid_from)
            .limit(_EDGE_BATCH)
        )
        if last_edge is not None:
            last_id, last_valid_from = last_edge
            stmt = stmt.where(
                or_(
                    Edge.id > last_id,
                    and_(Edge.id == last_id, Edge.valid_from > last_valid_from),
                )
            )
        rows = (await session.execute(stmt)).all()
        if not rows:
            break
        for edge_id, valid_from, created_by in rows:
            owners = _owners(created_by)
            missing = owners - source_names
            if not owners or missing:
                detail = ", ".join(sorted(missing)) if missing else "<missing>"
                raise IncompleteFullRebuildCoverage(
                    f"legacy edge {edge_id} has uncovered owners: {detail}"
                )
            aware = _as_utc(valid_from)
            newest = aware if newest is None else max(newest, aware)
        last_edge = (rows[-1][0], rows[-1][1])
    return newest


async def _promote_nodes(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    published_at: datetime,
    replacement_authority: frozenset[str],
) -> tuple[int, int, int, int]:
    inserted = closed = unchanged = downgrade_ignored = 0
    last_id: str | None = None
    while True:
        stmt = (
            select(GraphNodeStage)
            .where(
                GraphNodeStage.project_id == project_id,
                GraphNodeStage.run_id == run_id,
            )
            .order_by(GraphNodeStage.node_id)
            .limit(_NODE_BATCH)
        )
        if last_id is not None:
            stmt = stmt.where(GraphNodeStage.node_id > last_id)
        staged_rows = (await session.execute(stmt)).scalars().all()
        if not staged_rows:
            break

        node_ids = [row.node_id for row in staged_rows]
        current_rows = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id.in_(node_ids),
                    Node.valid_to.is_(None),
                )
            )
        ).scalars().all()
        current_by_id: dict[str, Node] = {}
        for current in current_rows:
            if current.id in current_by_id:
                raise GraphPublicationInvariantError(
                    f"duplicate current node identity: {current.id}"
                )
            current_by_id[current.id] = current
        human_overlays = await load_node_human_overlays(
            session,
            project_id=project_id,
            node_ids=node_ids,
        )

        to_close: list[tuple[str, datetime]] = []
        to_insert: list[Node] = []
        for staged in staged_rows:
            staged_owners = _canonical_sources(
                staged.source_names or [staged.source_name]
            )
            if not staged_owners:
                raise GraphPublicationInvariantError(
                    f"staged node has no producer owners: {staged.node_id}"
                )
            expected_hash = node_semantic_hash(
                kind=staged.kind,
                data=staged.data,
                certainty=staged.certainty,
                created_by=staged_owners,
            )
            if staged.semantic_hash != expected_hash:
                raise GraphPublicationInvariantError(
                    f"staged node semantic_hash mismatch: {staged.node_id}"
                )
            current = current_by_id.get(staged.node_id)
            publish_owners = staged_owners
            if current is not None:
                current_source_data = strip_durable_overlay_fields(
                    current.data, target_kind="node"
                )
                current_hash = node_semantic_hash(
                    kind=current.kind,
                    data=current_source_data,
                    certainty=current.certainty,
                    created_by=current.created_by,
                )
                current_owners = _owners(current.created_by)
                same_payload = (
                    current.kind == staged.kind
                    and _canonical_json(current_source_data)
                    == _canonical_json(staged.data)
                    and current.certainty == staged.certainty
                )
                if same_payload:
                    # Identical contributions are additive: retaining all
                    # owners cannot erase an unrefreshed producer. Omission
                    # reconciliation remains the only authority-based removal.
                    publish_owners = _canonical_sources(
                        [*current_owners, *staged_owners]
                    )
                    merged_hash = node_semantic_hash(
                        kind=staged.kind,
                        data=staged.data,
                        certainty=staged.certainty,
                        created_by=publish_owners,
                    )
                    if current_hash == merged_hash:
                        unchanged += 1
                        continue
                elif current_hash == expected_hash:
                    unchanged += 1
                    continue
                if not same_payload and _would_downgrade(
                    current.certainty, staged.certainty
                ):
                    downgrade_ignored += 1
                    continue
                ownership_changes = current_owners != set(staged_owners)
                fully_authorized = bool(current_owners) and current_owners.issubset(
                    replacement_authority
                )
                if not same_payload and ownership_changes and not fully_authorized:
                    # Staging stores one materialized candidate per logical
                    # identity, not one durable contribution per producer.
                    # Replacing this row with [source_name] would silently
                    # erase unrefreshed owner data, so leave the published
                    # graph untouched unless sealed coverage accounts for
                    # every owner.
                    raise MultiProducerContributionRequired(
                        "cannot replace node owned by unrefreshed producers: "
                        f"{staged.node_id}"
                    )
                to_close.append((current.id, current.valid_from))

            to_insert.append(
                Node(
                    id=staged.node_id,
                    project_id=project_id,
                    kind=staged.kind,
                    data=materialize_node_data(
                        staged.data,
                        human_overlays.get(staged.node_id),
                    ),
                    certainty=staged.certainty,
                    created_by=publish_owners,
                    valid_from=published_at,
                )
            )
            inserted += 1

        if to_close:
            await session.execute(
                update(Node)
                .where(
                    Node.project_id == project_id,
                    Node.valid_to.is_(None),
                    tuple_(Node.id, Node.valid_from).in_(to_close),
                )
                .values(valid_to=published_at)
                .execution_options(synchronize_session=False)
            )
            closed += len(to_close)
            # The partial unique current-identity index requires the prior
            # version to be visibly closed before its successor is inserted.
            await session.flush()
        session.add_all(to_insert)
        await session.flush()
        last_id = staged_rows[-1].node_id
    return inserted, closed, unchanged, downgrade_ignored


async def _promote_edges(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    published_at: datetime,
    replacement_authority: frozenset[str],
) -> tuple[int, int, int, int]:
    inserted = closed = unchanged = downgrade_ignored = 0
    last_key: tuple[str, str, str] | None = None
    while True:
        stmt = (
            select(GraphEdgeStage)
            .where(
                GraphEdgeStage.project_id == project_id,
                GraphEdgeStage.run_id == run_id,
            )
            .order_by(
                GraphEdgeStage.source_id,
                GraphEdgeStage.target_id,
                GraphEdgeStage.kind,
            )
            .limit(_EDGE_BATCH)
        )
        if last_key is not None:
            last_source, last_target, last_kind = last_key
            stmt = stmt.where(
                or_(
                    GraphEdgeStage.source_id > last_source,
                    and_(
                        GraphEdgeStage.source_id == last_source,
                        GraphEdgeStage.target_id > last_target,
                    ),
                    and_(
                        GraphEdgeStage.source_id == last_source,
                        GraphEdgeStage.target_id == last_target,
                        GraphEdgeStage.kind > last_kind,
                    ),
                )
            )
        staged_rows = (await session.execute(stmt)).scalars().all()
        if not staged_rows:
            break

        keys = [(row.source_id, row.target_id, row.kind) for row in staged_rows]
        current_rows = (
            await session.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    tuple_(Edge.source_id, Edge.target_id, Edge.kind).in_(keys),
                    Edge.valid_to.is_(None),
                )
            )
        ).scalars().all()
        current_by_key: dict[tuple[str, str, str], Edge] = {}
        for current in current_rows:
            key = (current.source_id, current.target_id, current.kind)
            if key in current_by_key:
                raise GraphPublicationInvariantError(
                    f"duplicate current edge identity: {key!r}"
                )
            current_by_key[key] = current
        overlays = await load_edge_overlays(
            session,
            project_id=project_id,
            identities=keys,
        )

        to_close: list[tuple[uuid.UUID, datetime]] = []
        to_insert: list[Edge] = []
        for staged in staged_rows:
            key = (staged.source_id, staged.target_id, staged.kind)
            staged_owners = _canonical_sources(
                staged.source_names or [staged.source_name]
            )
            if not staged_owners:
                raise GraphPublicationInvariantError(
                    f"staged edge has no producer owners: {key!r}"
                )
            expected_hash = edge_semantic_hash(
                source_id=staged.source_id,
                target_id=staged.target_id,
                kind=staged.kind,
                data=staged.data,
                certainty=staged.certainty,
                created_by=staged_owners,
            )
            if staged.semantic_hash != expected_hash:
                raise GraphPublicationInvariantError(
                    f"staged edge semantic_hash mismatch: {key!r}"
                )
            current = current_by_key.get(key)
            publish_owners = staged_owners
            if current is not None:
                current_source_data = strip_durable_overlay_fields(
                    current.data, target_kind="edge"
                )
                current_hash = edge_semantic_hash(
                    source_id=current.source_id,
                    target_id=current.target_id,
                    kind=current.kind,
                    data=current_source_data,
                    certainty=current.certainty,
                    created_by=current.created_by,
                )
                current_owners = _owners(current.created_by)
                same_payload = (
                    _canonical_json(current_source_data)
                    == _canonical_json(staged.data)
                    and current.certainty == staged.certainty
                )
                if same_payload:
                    publish_owners = _canonical_sources(
                        [*current_owners, *staged_owners]
                    )
                    merged_hash = edge_semantic_hash(
                        source_id=staged.source_id,
                        target_id=staged.target_id,
                        kind=staged.kind,
                        data=staged.data,
                        certainty=staged.certainty,
                        created_by=publish_owners,
                    )
                    if current_hash == merged_hash:
                        unchanged += 1
                        continue
                elif current_hash == expected_hash:
                    unchanged += 1
                    continue
                if not same_payload and _would_downgrade(
                    current.certainty, staged.certainty
                ):
                    downgrade_ignored += 1
                    continue
                ownership_changes = current_owners != set(staged_owners)
                fully_authorized = bool(current_owners) and current_owners.issubset(
                    replacement_authority
                )
                if not same_payload and ownership_changes and not fully_authorized:
                    raise MultiProducerContributionRequired(
                        "cannot replace edge owned by unrefreshed producers: "
                        f"{key!r}"
                    )
                to_close.append((current.id, current.valid_from))

            to_insert.append(
                Edge(
                    project_id=project_id,
                    source_id=staged.source_id,
                    target_id=staged.target_id,
                    kind=staged.kind,
                    data=materialize_edge_data(
                        staged.data,
                        overlays.human.get(key),
                        overlays.runtime.get(key),
                    ),
                    certainty=staged.certainty,
                    created_by=publish_owners,
                    valid_from=published_at,
                )
            )
            inserted += 1

        if to_close:
            await session.execute(
                update(Edge)
                .where(
                    Edge.project_id == project_id,
                    Edge.valid_to.is_(None),
                    tuple_(Edge.id, Edge.valid_from).in_(to_close),
                )
                .values(valid_to=published_at)
                .execution_options(synchronize_session=False)
            )
            closed += len(to_close)
            await session.flush()
        session.add_all(to_insert)
        await session.flush()
        tail = staged_rows[-1]
        last_key = (tail.source_id, tail.target_id, tail.kind)
    return inserted, closed, unchanged, downgrade_ignored


async def _close_missing_authoritative_facts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    source_names: frozenset[str],
    published_at: datetime,
) -> tuple[int, int]:
    """Close current facts omitted from an explicitly-authoritative stage.

    The primitive does not decide whether coverage is authoritative; callers
    must pass only producer names whose complete terminal coverage contract was
    verified.  With no producer names deletion is disabled.
    """

    if not source_names:
        return 0, 0

    closed_nodes = 0
    last_node: tuple[str, datetime] | None = None
    while True:
        stmt = (
            select(Node.id, Node.valid_from, Node.created_by)
            .where(Node.project_id == project_id, Node.valid_to.is_(None))
            .order_by(Node.id, Node.valid_from)
            .limit(_NODE_BATCH)
        )
        if last_node is not None:
            last_id, last_valid_from = last_node
            stmt = stmt.where(
                or_(
                    Node.id > last_id,
                    and_(Node.id == last_id, Node.valid_from > last_valid_from),
                )
            )
        rows = (await session.execute(stmt)).all()
        if not rows:
            break
        ids = [row[0] for row in rows]
        present = set(
            (
                await session.execute(
                    select(GraphNodeStage.node_id).where(
                        GraphNodeStage.project_id == project_id,
                        GraphNodeStage.run_id == run_id,
                        GraphNodeStage.node_id.in_(ids),
                    )
                )
            ).scalars()
        )
        stale = [
            (node_id, valid_from)
            for node_id, valid_from, created_by in rows
            if _owners(created_by)
            and _owners(created_by).issubset(source_names)
            and node_id not in present
        ]
        if stale:
            await session.execute(
                update(Node)
                .where(
                    Node.project_id == project_id,
                    Node.valid_to.is_(None),
                    tuple_(Node.id, Node.valid_from).in_(stale),
                )
                .values(valid_to=published_at)
                .execution_options(synchronize_session=False)
            )
            closed_nodes += len(stale)
        last_node = (rows[-1][0], rows[-1][1])

    closed_edges = 0
    last_edge: tuple[uuid.UUID, datetime] | None = None
    while True:
        stmt = (
            select(
                Edge.id,
                Edge.valid_from,
                Edge.source_id,
                Edge.target_id,
                Edge.kind,
                Edge.created_by,
            )
            .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
            .order_by(Edge.id, Edge.valid_from)
            .limit(_EDGE_BATCH)
        )
        if last_edge is not None:
            last_id, last_valid_from = last_edge
            stmt = stmt.where(
                or_(
                    Edge.id > last_id,
                    and_(Edge.id == last_id, Edge.valid_from > last_valid_from),
                )
            )
        rows = (await session.execute(stmt)).all()
        if not rows:
            break
        keys = [(row[2], row[3], row[4]) for row in rows]
        present = set(
            (
                await session.execute(
                    select(
                        GraphEdgeStage.source_id,
                        GraphEdgeStage.target_id,
                        GraphEdgeStage.kind,
                    ).where(
                        GraphEdgeStage.project_id == project_id,
                        GraphEdgeStage.run_id == run_id,
                        tuple_(
                            GraphEdgeStage.source_id,
                            GraphEdgeStage.target_id,
                            GraphEdgeStage.kind,
                        ).in_(keys),
                    )
                )
            ).all()
        )
        stale = [
            (edge_id, valid_from)
            for edge_id, valid_from, source_id, target_id, kind, created_by in rows
            if _owners(created_by)
            and _owners(created_by).issubset(source_names)
            and (source_id, target_id, kind) not in present
        ]
        if stale:
            await session.execute(
                update(Edge)
                .where(
                    Edge.project_id == project_id,
                    Edge.valid_to.is_(None),
                    tuple_(Edge.id, Edge.valid_from).in_(stale),
                )
                .values(valid_to=published_at)
                .execution_options(synchronize_session=False)
            )
            closed_edges += len(stale)
        last_edge = (rows[-1][0], rows[-1][1])
    return closed_nodes, closed_edges


async def promote_staged_graph(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    allow_full_rebuild: bool = False,
    final_stats: dict[str, Any] | None = None,
    requested_publish_at: datetime | None = None,
    cleanup_stage_on_success: bool = True,
) -> GraphPromotionResult:
    """Atomically publish one run's staged Node/Edge candidates.

    The supplied session must not already own a transaction.  This keeps the
    publication boundary auditable and prevents an outer caller from silently
    committing unrelated work with the graph head.  No network, analyzer,
    progress, findings, runtime, or LLM work belongs inside this transaction.
    Whole-run ``completed_at`` remains unset until those derived products end;
    a post-publication failure can therefore close the run as ``partial``
    without moving the graph head backwards.
    Candidate reconciliation is page-bounded. Omission reconciliation is not:
    when the run has sealed authoritative coverage it scans current Node/Edge
    rows inside the atomic boundary and is O(graph), pending staged deletion
    intents or a durable producer-contribution table.

    ``allow_full_rebuild`` is accepted only for a run whose persisted scope is
    ``full``.  It is the sole path from ``needs_rebuild`` to ``ready``.
    The base generation and deletion authority are derived only from write-once
    run fields captured/sealed before promotion.  A needs-rebuild transition
    additionally proves that every legacy current owner is covered before it
    changes the head.
    """

    if session.in_transaction():
        raise GraphPublicationUsageError(
            "promote_staged_graph requires a session with no active transaction"
        )
    async with session.begin():
        head = (
            await session.execute(
                select(GraphHead)
                .where(GraphHead.project_id == project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if head is None:
            raise GraphHeadMissing(
                "graph head missing; bootstrap it as needs_rebuild first"
            )

        run = (
            await session.execute(
                select(AnalysisRun).where(
                    AnalysisRun.id == run_id,
                    AnalysisRun.project_id == project_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if run is None:
            raise GraphPublicationInvariantError(
                "AnalysisRun does not belong to the publication project"
            )

        if run.graph_base_generation is None:
            raise GraphPublicationInvariantError(
                "run has no captured graph_base_generation"
            )
        base_generation = int(run.graph_base_generation)
        if base_generation < 0:
            raise GraphPublicationInvariantError(
                "persisted graph_base_generation must be non-negative"
            )
        if run.graph_coverage_sealed_at is None:
            raise GraphCoverageNotSealed(
                "seal producer coverage before graph promotion"
            )
        if run.graph_authoritative_sources is None:
            raise GraphPublicationInvariantError(
                "sealed coverage is missing authoritative source data"
            )
        canonical_sources = _canonical_sources(run.graph_authoritative_sources)
        if list(run.graph_authoritative_sources) != canonical_sources:
            raise GraphPublicationInvariantError(
                "sealed authoritative sources are not canonical"
            )
        source_names = frozenset(canonical_sources)
        coverage_sealed_at = _as_utc(run.graph_coverage_sealed_at).isoformat()

        if head.current_run_id == run_id:
            publication = validate_graph_publication_receipt(run, head=head)
            return GraphPromotionResult(
                project_id=project_id,
                run_id=run_id,
                generation=publication.generation,
                published_at=publication.published_at,
                idempotent=True,
                counts=publication.counts,
            )

        if int(head.generation) != int(base_generation):
            raise StaleGraphGeneration(
                f"base generation {base_generation} is stale; "
                f"current generation is {head.generation}"
            )
        if run.status != "running":
            raise GraphPublicationInvariantError(
                f"only a running AnalysisRun can publish (status={run.status})"
            )
        if run.scope not in {"full", "incremental"}:
            raise GraphPublicationInvariantError(
                f"run scope {run.scope!r} does not publish a source graph"
            )
        if head.state == GRAPH_HEAD_NEEDS_REBUILD and not (
            allow_full_rebuild and run.scope == "full"
        ):
            raise GraphHeadNeedsRebuild(
                "legacy graph is untrusted; a staged full rebuild is required"
            )
        if head.state not in {GRAPH_HEAD_NEEDS_REBUILD, GRAPH_HEAD_READY}:
            raise GraphPublicationInvariantError(
                f"unknown graph-head state: {head.state}"
            )

        await _reject_cross_project_stage(
            session, project_id=project_id, run_id=run_id
        )
        legacy_valid_from: datetime | None = None
        if head.state == GRAPH_HEAD_NEEDS_REBUILD:
            legacy_valid_from = await _validate_full_rebuild_coverage(
                session,
                project_id=project_id,
                run_id=run_id,
                source_names=source_names,
            )
        published_at = _next_publication_time(
            head.published_at,
            requested_publish_at,
            not_before=(run.started_at, run.graph_coverage_sealed_at),
            strictly_after=(legacy_valid_from,),
        )
        next_generation = int(base_generation) + 1

        # This CAS is deliberately the first write in the transaction.  It
        # obtains a row lock on PostgreSQL and SQLite's single-writer lock;
        # readers still see the prior committed head/graph until commit.
        reserved = await session.execute(
            update(GraphHead)
            .where(
                GraphHead.project_id == project_id,
                GraphHead.generation == base_generation,
            )
            .values(
                current_run_id=run_id,
                generation=next_generation,
                state=GRAPH_HEAD_READY,
                published_at=published_at,
                updated_at=published_at,
            )
        )
        if reserved.rowcount != 1:
            raise StaleGraphGeneration(
                "graph-head compare-and-swap lost to another publication"
            )

        node_counts = await _promote_nodes(
            session,
            project_id=project_id,
            run_id=run_id,
            published_at=published_at,
            replacement_authority=source_names,
        )
        edge_counts = await _promote_edges(
            session,
            project_id=project_id,
            run_id=run_id,
            published_at=published_at,
            replacement_authority=source_names,
        )
        stale_nodes, stale_edges = await _close_missing_authoritative_facts(
            session,
            project_id=project_id,
            run_id=run_id,
            source_names=source_names,
            published_at=published_at,
        )
        counts = PromotionCounts(
            nodes_inserted=node_counts[0],
            nodes_closed=node_counts[1],
            nodes_unchanged=node_counts[2],
            nodes_downgrade_ignored=node_counts[3],
            edges_inserted=edge_counts[0],
            edges_closed=edge_counts[1],
            edges_unchanged=edge_counts[2],
            edges_downgrade_ignored=edge_counts[3],
            stale_nodes_closed=stale_nodes,
            stale_edges_closed=stale_edges,
        )

        prior_stats = run.stats if isinstance(run.stats, dict) else {}
        publication_stats = {
            "contract_version": GRAPH_PUBLICATION_CONTRACT,
            "generation": next_generation,
            "base_generation": int(base_generation),
            "published_at": published_at.isoformat(),
            "authoritative_sources": canonical_sources,
            "coverage_sealed_at": coverage_sealed_at,
            "counts": _promotion_counts_payload(counts),
            "atomic": True,
        }
        run.stats = {
            **prior_stats,
            **(final_stats or {}),
            "graph_publication": publication_stats,
        }
        run.status = "published"
        run.completed_at = None
        run.error_log = None

        if cleanup_stage_on_success:
            await session.execute(
                delete(GraphNodeStage).where(
                    GraphNodeStage.project_id == project_id,
                    GraphNodeStage.run_id == run_id,
                )
            )
            await session.execute(
                delete(GraphEdgeStage).where(
                    GraphEdgeStage.project_id == project_id,
                    GraphEdgeStage.run_id == run_id,
                )
            )
        await session.flush()

        return GraphPromotionResult(
            project_id=project_id,
            run_id=run_id,
            generation=next_generation,
            published_at=published_at,
            idempotent=False,
            counts=counts,
        )


async def cleanup_staged_graph(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
) -> StageCleanupResult:
    """Delete staging for one terminal run without committing the caller.

    Active staging is never deleted: a stale-run reaper must first prove and
    persist the run's terminal state.  Keeping transaction ownership with the
    caller lets a bounded cron batch several cleanups atomically.
    """

    status = (
        await session.execute(
            select(AnalysisRun.status).where(
                AnalysisRun.id == run_id,
                AnalysisRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if status is None:
        raise GraphPublicationInvariantError("AnalysisRun not found for cleanup")
    if status not in _CLEANABLE_RUN_STATUSES:
        raise ActiveStagingCleanupRejected(
            f"cannot clean staging for active run status={status}"
        )
    node_result = await session.execute(
        delete(GraphNodeStage).where(
            GraphNodeStage.project_id == project_id,
            GraphNodeStage.run_id == run_id,
        )
    )
    edge_result = await session.execute(
        delete(GraphEdgeStage).where(
            GraphEdgeStage.project_id == project_id,
            GraphEdgeStage.run_id == run_id,
        )
    )
    return StageCleanupResult(
        project_id=project_id,
        run_id=run_id,
        nodes_deleted=node_result.rowcount or 0,
        edges_deleted=edge_result.rowcount or 0,
    )
