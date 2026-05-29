"""Finding generation (spec §9.4).

Runs after an analysis run completes and re-scans current-valid graph state
for the Phase-1 Finding taxonomy. Idempotent: a Finding with the same
(project, kind, subject) is updated in place rather than duplicated.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.findings import Finding
from app.models.graph import Edge, Node
from app.merge.risk import remediation_for, score_finding


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
    """True iff the subject node carries the OTLP ``exercised`` flag
    (PR-25 Tier 2). A finding on a live production path outranks one
    on apparently-dead code."""
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
    if node is None:
        return False
    return str((node.data or {}).get("exercised", "")).lower() == "true"


async def _upsert_finding(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    kind: str,
    severity: str,
    detail: dict,
    subject_node_id: str | None = None,
    subject_edge_id: uuid.UUID | None = None,
) -> None:
    now = datetime.now(tz=timezone.utc)
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
                Finding.subject_node_id.is_(subject_node_id)
                if subject_node_id is None
                else Finding.subject_node_id == subject_node_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_seen_at = now
        existing.detail = detail
        # Re-score on every re-scan — blast radius / exercised flag
        # can change as the graph evolves.
        existing.risk_score = risk
        existing.remediation = remediation
        existing.cwe_id = cwe_id
        return
    session.add(
        Finding(
            project_id=project_id,
            kind=kind,
            severity=severity,
            detail=detail,
            subject_node_id=subject_node_id,
            subject_edge_id=subject_edge_id,
            risk_score=risk,
            remediation=remediation,
            cwe_id=cwe_id,
        )
    )


async def detect_duplicate_endpoints(session: AsyncSession, project_id: uuid.UUID) -> int:
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
        )
    return len(rows)


async def detect_unverified_claims(
    session: AsyncSession, project_id: uuid.UUID, *, stale_days: int = 30
) -> int:
    threshold = datetime.now(tz=timezone.utc) - timedelta(days=stale_days)
    rows = (
        await session.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.certainty == "inferred",
                Edge.valid_to.is_(None),
                Edge.valid_from < threshold,
            )
        )
    ).scalars().all()
    for edge in rows:
        await _upsert_finding(
            session,
            project_id=project_id,
            kind="unverified_claim",
            severity="info",
            subject_edge_id=edge.id,
            detail={"source": edge.source_id, "target": edge.target_id, "kind": edge.kind},
        )
    return len(rows)


async def detect_dynamic_calls(session: AsyncSession, project_id: uuid.UUID) -> int:
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
            subject_edge_id=edge.id,
            detail={"source": edge.source_id, "target": edge.target_id},
        )
    return len(rows)


async def detect_dead_paths(
    session: AsyncSession, project_id: uuid.UUID, *, window_days: int = 30
) -> int:
    threshold = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    rows = (
        await session.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
                Edge.valid_from < threshold,
                Edge.data["exercised"].astext != "true",
            )
        )
    ).scalars().all()
    for edge in rows:
        await _upsert_finding(
            session,
            project_id=project_id,
            kind="dead_path_suspected",
            severity="info",
            subject_edge_id=edge.id,
            detail={"source": edge.source_id, "target": edge.target_id},
        )
    return len(rows)


async def detect_schema_mismatches(
    session: AsyncSession, project_id: uuid.UUID
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
            subject_edge_id=edge.id,
            detail={
                "source": edge.source_id,
                "missing_target": edge.target_id,
                "edge_kind": edge.kind,
            },
        )
        count += 1
    return count


async def detect_opaque_failing_components(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    error_ratio_threshold: float = 0.1,
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
        )
        count += 1
    return count


async def run_all(session: AsyncSession, project_id: uuid.UUID) -> dict[str, int]:
    stats = {
        "duplicate_endpoints": await detect_duplicate_endpoints(session, project_id),
        "unverified_claims": await detect_unverified_claims(session, project_id),
        "dynamic_calls": await detect_dynamic_calls(session, project_id),
        "dead_paths": await detect_dead_paths(session, project_id),
        "schema_mismatches": await detect_schema_mismatches(session, project_id),
        "opaque_failing": await detect_opaque_failing_components(session, project_id),
    }
    # PR-104 — snapshot the newly-inserted Finding instances *before*
    # commit clears them out of ``session.new``. Existing-finding
    # updates (the in-place last_seen_at / risk_score path above)
    # land in ``session.dirty`` instead, so they're correctly
    # excluded — operators only want to hear about first sightings,
    # not "this thing is still here".
    new_findings = [o for o in session.new if isinstance(o, Finding)]
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
