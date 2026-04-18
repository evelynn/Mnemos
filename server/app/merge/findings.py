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
from app.models.graph import Edge


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
        return
    session.add(
        Finding(
            project_id=project_id,
            kind=kind,
            severity=severity,
            detail=detail,
            subject_node_id=subject_node_id,
            subject_edge_id=subject_edge_id,
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


async def run_all(session: AsyncSession, project_id: uuid.UUID) -> dict[str, int]:
    stats = {
        "duplicate_endpoints": await detect_duplicate_endpoints(session, project_id),
        "unverified_claims": await detect_unverified_claims(session, project_id),
        "dynamic_calls": await detect_dynamic_calls(session, project_id),
        "dead_paths": await detect_dead_paths(session, project_id),
    }
    await session.commit()
    return stats
