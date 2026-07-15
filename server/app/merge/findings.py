"""Revision-pinned finding generation over the current graph.

One rebuild locks the ready ``GraphHead``, runs every detector, stamps each
logical finding with the exact source+overlay revision, reconciles disappeared
open findings, and commits once. Physical bitemporal edge UUIDs are provenance,
not identity: edge findings key on ``(source_id, target_id, edge.kind)``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.finding_identity import FindingIdentityError, finding_identity_key
from app.graph_publication import (
    GraphPublicationError,
    lock_validated_graph_head,
)
from app.graph_overlays import (
    edge_identity,
    edge_read_view,
    effective_certainty,
    load_edge_overlays,
)
from app.models.findings import Finding
from app.models.graph import Edge, Node
from app.merge.risk import remediation_for, score_finding

_SYSTEM_GRAPH_REFRESH = "system:graph-refresh"


class FindingRebuildUnavailable(RuntimeError):
    """A finding rebuild cannot prove one ready graph revision."""


@dataclass(frozen=True)
class FindingValidationRevision:
    project_id: uuid.UUID
    graph_generation: int
    overlay_generation: int
    validated_at: datetime


def _edge_logical_identity(edge: Edge) -> tuple[str, str, str]:
    return edge.source_id, edge.target_id, edge.kind


async def _blast_radius(
    session: AsyncSession, project_id: uuid.UUID, subject_node_id: str | None
) -> int:
    """Count graph edges touching the subject node — the merge layer
    feeds this into the risk score so a finding on a heavily-connected
    node ranks above one on a leaf."""
    if subject_node_id is None:
        return 0
    out = (
        await session.execute(
            select(func.count())
            .select_from(Edge)
            .where(
                Edge.project_id == project_id,
                Edge.valid_to.is_(None),
                (Edge.source_id == subject_node_id)
                | (Edge.target_id == subject_node_id),
            )
        )
    ).scalar()
    return int(out or 0)


async def _subject_is_exercised(
    session: AsyncSession, project_id: uuid.UUID, subject_node_id: str | None
) -> bool:
    """True iff the subject symbol lies on a live production path (PR-25
    Tier 2). A finding on an exercised path outranks one on apparently-dead
    code.

    A symbol counts as exercised when EITHER the node itself carries the
    ``exercised`` flag (seed / direct marking) OR any CALLS edge incident to
    it does. The Tier-2 reconcile (``merge/runtime.py``) marks *edges*
    exercised from OTLP traces — never nodes — so the original node-only
    check returned False for every trace-derived hit, silently dropping the
    exercised risk multiplier for node-subject findings. This aligns the
    read with where runtime writes the flag (and with
    ``queries.impact_analysis``, which also derives node exercised-ness from
    incident edges)."""
    if subject_node_id is None:
        return False
    node = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == subject_node_id,
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is not None and str((node.data or {}).get("exercised", "")).lower() == "true":
        return True
    # Runtime marks CALLS edges exercised, not nodes — a symbol is exercised
    # if any observed production call goes into or out of it.
    incident_edges = (
        await session.execute(
            select(Edge)
            .where(
                Edge.project_id == project_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
                (Edge.source_id == subject_node_id)
                | (Edge.target_id == subject_node_id),
            )
        )
    ).scalars().all()
    overlays = await load_edge_overlays(
        session,
        project_id=project_id,
        identities=(edge_identity(edge) for edge in incident_edges),
    )
    return any(
        edge_read_view(
            edge,
            overlays.human.get(edge_identity(edge)),
            overlays.runtime.get(edge_identity(edge)),
        )["exercised"]
        for edge in incident_edges
    )


async def _upsert_finding(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    kind: str,
    severity: str,
    detail: dict,
    subject_node_id: str | None = None,
    subject_edge_id: uuid.UUID | None = None,
    subject_edge: Edge | None = None,
    validation: FindingValidationRevision | None = None,
) -> None:
    if validation is not None and validation.project_id != project_id:
        raise FindingIdentityError(
            "validation.project_id: expected the finding project"
        )
    if subject_edge is not None and subject_edge_id is not None:
        raise FindingIdentityError(
            "subject_edge: provide the logical edge row or subject_edge_id, not both"
        )
    if subject_edge is None and subject_edge_id is not None:
        subject_edge = (
            await session.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    Edge.id == subject_edge_id,
                )
            )
        ).scalar_one_or_none()
        if subject_edge is None:
            raise FindingIdentityError(
                "subject_edge_id: edge does not exist in the finding project"
            )
    if subject_edge is not None and subject_edge.project_id != project_id:
        raise FindingIdentityError(
            "subject_edge.project_id: expected the finding project"
        )

    edge_identity = (
        _edge_logical_identity(subject_edge) if subject_edge is not None else None
    )
    identity_key = finding_identity_key(
        kind=kind,
        subject_node_id=subject_node_id,
        subject_edge=edge_identity,
    )
    now = (
        validation.validated_at
        if validation is not None
        else datetime.now(tz=timezone.utc)
    )
    # PR-50 — compute the risk score + remediation hint at upsert
    # time so the dashboard's risk-ordered list is always fresh.
    blast = await _blast_radius(session, project_id, subject_node_id)
    exercised = await _subject_is_exercised(session, project_id, subject_node_id)
    risk = score_finding(
        severity=severity, exercised=exercised, blast_radius=blast
    )
    remediation, cwe_id = remediation_for(kind)
    existing = (
        await session.execute(
            select(Finding).where(
                Finding.project_id == project_id,
                Finding.kind == kind,
                Finding.identity_key == identity_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_seen_at = now
        existing.detail = detail
        existing.severity = severity
        existing.subject_node_id = subject_node_id
        existing.subject_edge_id = subject_edge.id if subject_edge is not None else None
        # Re-score on every re-scan — blast radius / exercised flag
        # can change as the graph evolves.
        existing.risk_score = risk
        existing.remediation = remediation
        existing.cwe_id = cwe_id
        if existing.status == "resolved" and existing.resolved_by == _SYSTEM_GRAPH_REFRESH:
            existing.status = "open"
            existing.resolved_at = None
            existing.resolved_by = None
        if validation is None:
            # A direct detector call is useful for isolated tests/diagnostics,
            # but it did not lock and reconcile one head revision. Never leave
            # its mutable update carrying an old currentness claim.
            existing.validated_graph_generation = None
            existing.validated_overlay_generation = None
            existing.validated_at = None
        else:
            existing.validated_graph_generation = validation.graph_generation
            existing.validated_overlay_generation = validation.overlay_generation
            existing.validated_at = validation.validated_at
        return
    session.add(
        Finding(
            project_id=project_id,
            kind=kind,
            identity_key=identity_key,
            severity=severity,
            detail=detail,
            subject_node_id=subject_node_id,
            subject_edge_id=(subject_edge.id if subject_edge is not None else None),
            risk_score=risk,
            remediation=remediation,
            cwe_id=cwe_id,
            validated_graph_generation=(
                validation.graph_generation if validation is not None else None
            ),
            validated_overlay_generation=(
                validation.overlay_generation if validation is not None else None
            ),
            validated_at=(validation.validated_at if validation is not None else None),
        )
    )


async def detect_duplicate_endpoints(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    validation: FindingValidationRevision | None = None,
) -> int:
    rows = (
        await session.execute(
            select(Edge.target_id, func.count())
            .where(
                Edge.project_id == project_id,
                Edge.kind == "EXPOSES",
                Edge.valid_to.is_(None),
            )
            .group_by(Edge.target_id)
            .having(func.count() > 1)
        )
    ).all()
    for target_id, count in rows:
        await _upsert_finding(
            session,
            project_id=project_id,
            kind="duplicate_endpoint",
            severity="error",
            subject_node_id=target_id,
            detail={"contract_id": target_id, "exposer_count": int(count)},
            validation=validation,
        )
    return len(rows)


async def detect_unverified_claims(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    stale_days: int = 30,
    validation: FindingValidationRevision | None = None,
) -> int:
    threshold = datetime.now(tz=timezone.utc) - timedelta(days=stale_days)
    candidates = (
        await session.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.valid_to.is_(None),
                Edge.valid_from < threshold,
            )
        )
    ).scalars().all()
    overlays = await load_edge_overlays(
        session,
        project_id=project_id,
        identities=(edge_identity(edge) for edge in candidates),
    )
    rows = [
        edge
        for edge in candidates
        if effective_certainty(
            edge.certainty, overlays.human.get(edge_identity(edge))
        )
        == "inferred"
    ]
    for edge in rows:
        await _upsert_finding(
            session,
            project_id=project_id,
            kind="unverified_claim",
            severity="info",
            subject_edge=edge,
            detail={"source": edge.source_id, "target": edge.target_id, "kind": edge.kind},
            validation=validation,
        )
    return len(rows)


async def detect_dynamic_calls(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    validation: FindingValidationRevision | None = None,
) -> int:
    rows = (
        await session.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
                Edge.certainty == "asserted",
                Edge.data["via_runtime"].astext == "true",
            )
        )
    ).scalars().all()
    for edge in rows:
        await _upsert_finding(
            session,
            project_id=project_id,
            kind="dynamic_call_detected",
            severity="warning",
            subject_edge=edge,
            detail={"source": edge.source_id, "target": edge.target_id},
            validation=validation,
        )
    return len(rows)


async def detect_dead_paths(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window_days: int = 30,
    validation: FindingValidationRevision | None = None,
) -> int:
    threshold = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    candidates = (
        await session.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
                Edge.valid_from < threshold,
            )
        )
    ).scalars().all()
    overlays = await load_edge_overlays(
        session,
        project_id=project_id,
        identities=(edge_identity(edge) for edge in candidates),
    )
    rows = [
        edge
        for edge in candidates
        if not edge_read_view(
            edge,
            overlays.human.get(edge_identity(edge)),
            overlays.runtime.get(edge_identity(edge)),
        )["exercised"]
    ]
    for edge in rows:
        await _upsert_finding(
            session,
            project_id=project_id,
            kind="dead_path_suspected",
            severity="info",
            subject_edge=edge,
            detail={"source": edge.source_id, "target": edge.target_id},
            validation=validation,
        )
    return len(rows)


async def detect_schema_mismatches(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    validation: FindingValidationRevision | None = None,
) -> int:
    """Code references a table/entity that the live DB schema doesn't expose.

    READS / WRITES edges land in the graph from analyser ``data_access``
    runs; ``DataEntity`` nodes land from ``live_schema`` runs. When the
    edge target id is not currently a valid DataEntity, the code is
    talking to something the DB doesn't have — usually a renamed table,
    a stale schema reference, or a dropped object.
    """
    edge_rows = (
        await session.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.kind.in_(("READS", "WRITES")),
                Edge.valid_to.is_(None),
            )
        )
    ).scalars().all()
    if not edge_rows:
        return 0

    target_ids = {e.target_id for e in edge_rows}
    valid_entities = {
        nid
        for nid, in (
            await session.execute(
                select(Node.id).where(
                    Node.project_id == project_id,
                    Node.kind == "DataEntity",
                    Node.valid_to.is_(None),
                    Node.id.in_(target_ids),
                )
            )
        ).all()
    }

    count = 0
    for edge in edge_rows:
        if edge.target_id in valid_entities:
            continue
        await _upsert_finding(
            session,
            project_id=project_id,
            kind="schema_mismatch",
            severity="warning",
            subject_edge=edge,
            detail={
                "source": edge.source_id,
                "missing_target": edge.target_id,
                "edge_kind": edge.kind,
            },
            validation=validation,
        )
        count += 1
    return count


async def detect_opaque_failing_components(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    error_ratio_threshold: float = 0.1,
    validation: FindingValidationRevision | None = None,
) -> int:
    """Opaque components (binaries / external services) whose calls error
    out at a high rate.

    Looks at CALLS edges whose target is a Node with ``kind=Component``
    and ``data.opacity in {"opaque","binary"}``; aggregates the runtime
    error counts attached to each edge and flags components whose
    error / total ratio exceeds ``error_ratio_threshold``.
    """
    opaque_ids = {
        nid
        for nid, in (
            await session.execute(
                select(Node.id).where(
                    Node.project_id == project_id,
                    Node.kind == "Component",
                    Node.valid_to.is_(None),
                    Node.data["opacity"].astext.in_(("opaque", "binary")),
                )
            )
        ).all()
    }
    if not opaque_ids:
        return 0

    rows = (
        await session.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
                Edge.target_id.in_(opaque_ids),
            )
        )
    ).scalars().all()

    by_target: dict[str, dict[str, int]] = {}
    for e in rows:
        d = e.data or {}
        try:
            errors = int(d.get("runtime_errors", 0))
            total = int(d.get("runtime_calls", 0))
        except (TypeError, ValueError):
            continue
        if total == 0:
            continue
        agg = by_target.setdefault(e.target_id, {"errors": 0, "total": 0})
        agg["errors"] += errors
        agg["total"] += total

    count = 0
    for target_id, agg in by_target.items():
        ratio = agg["errors"] / max(agg["total"], 1)
        if ratio < error_ratio_threshold:
            continue
        await _upsert_finding(
            session,
            project_id=project_id,
            kind="opaque_component_failing",
            severity="warning",
            subject_node_id=target_id,
            detail={
                "errors": agg["errors"],
                "calls": agg["total"],
                "error_ratio": round(ratio, 4),
            },
            validation=validation,
        )
        count += 1
    return count


async def _lock_finding_revision(
    session: AsyncSession, project_id: uuid.UUID
) -> FindingValidationRevision:
    try:
        head, publication = await lock_validated_graph_head(
            session, project_id=project_id
        )
    except GraphPublicationError as exc:
        raise FindingRebuildUnavailable(
            "finding rebuild requires one ready atomically-published graph head"
        ) from exc
    return FindingValidationRevision(
        project_id=project_id,
        graph_generation=publication.generation,
        overlay_generation=head.overlay_generation,
        validated_at=datetime.now(tz=timezone.utc),
    )


async def _resolve_disappeared_findings(
    session: AsyncSession,
    *,
    validation: FindingValidationRevision,
) -> int:
    """Resolve prior graph-derived open rows not revalidated this revision.

    Detectors first stamp every present identity with ``validation``. One
    set-based update can then reconcile all absent prior rows without loading
    the project backlog into Python memory. Null-marker legacy/manual rows are
    excluded because no graph revision ever claimed them as current.
    """

    result = await session.execute(
        update(Finding)
        .where(
            Finding.project_id == validation.project_id,
            Finding.status.in_(("open", "acknowledged")),
            Finding.validated_graph_generation.is_not(None),
            Finding.validated_overlay_generation.is_not(None),
            or_(
                Finding.validated_graph_generation
                != validation.graph_generation,
                Finding.validated_overlay_generation
                != validation.overlay_generation,
            ),
        )
        .values(
            status="resolved",
            resolved_at=validation.validated_at,
            resolved_by=_SYSTEM_GRAPH_REFRESH,
        )
    )
    return max(0, int(result.rowcount or 0))


async def run_all(session: AsyncSession, project_id: uuid.UUID) -> dict[str, int]:
    """Atomically rebuild and reconcile findings for one locked graph head."""

    try:
        validation = await _lock_finding_revision(session, project_id)
        stats = {
            "duplicate_endpoints": await detect_duplicate_endpoints(
                session, project_id, validation=validation
            ),
            "unverified_claims": await detect_unverified_claims(
                session, project_id, validation=validation
            ),
            "dynamic_calls": await detect_dynamic_calls(
                session, project_id, validation=validation
            ),
            "dead_paths": await detect_dead_paths(
                session, project_id, validation=validation
            ),
            "schema_mismatches": await detect_schema_mismatches(
                session, project_id, validation=validation
            ),
            "opaque_failing": await detect_opaque_failing_components(
                session, project_id, validation=validation
            ),
        }
        new_findings = [o for o in session.new if isinstance(o, Finding)]
        await session.flush()
        stats["auto_resolved"] = await _resolve_disappeared_findings(
            session, validation=validation
        )
        await session.flush()
    except BaseException:
        # ``run_all`` owns the rebuild boundary. A detector, reconciliation,
        # or cancellation failure must not expose a partially stamped set.
        await session.rollback()
        raise

    # PR-104 — snapshot the newly-inserted Finding instances *before*
    # commit clears them out of ``session.new``. Existing-finding
    # updates (the in-place last_seen_at / risk_score path above)
    # land in ``session.dirty`` instead, so they're correctly
    # excluded — operators only want to hear about first sightings,
    # not "this thing is still here".
    await session.commit()
    if new_findings:
        # Refresh so the notifier sees server-assigned IDs in the
        # drill-down URL.
        for f in new_findings:
            try:
                await session.refresh(f)
            except Exception:  # noqa: BLE001
                # A refresh failure is harmless — the notifier just
                # gets an empty id and links to /findings root.
                pass
        try:
            from app.notify.outbound import notify_new_findings

            await notify_new_findings(new_findings)
        except Exception:  # noqa: BLE001
            # Notifier is best-effort; its own internal handler
            # already logs + bumps a metric. Catching here too is
            # belt-and-braces so a config error never breaks merge.
            import logging
            logging.getLogger(__name__).exception("notify_new_findings raised")
    return stats
