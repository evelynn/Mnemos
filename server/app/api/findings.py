import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.api.graph_guard import require_readable_current_graph
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org, resolve_project_org, same_org
from app.auth.rbac import require_operator
from app.db import get_session
from app.finding_currentness import (
    FINDING_NOT_CURRENT_CODE,
    FindingNotCurrent,
    current_findings_select,
    lock_current_finding_for_action,
)
from app.merge.findings import run_all
from app.models.findings import Finding

router = APIRouter(tags=["findings"])


class FindingOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    severity: str
    status: str
    subject_node_id: str | None
    subject_edge_id: uuid.UUID | None
    detail: dict[str, Any]
    # PR-50 — solution-analysis fields.
    risk_score: int
    priority: str
    remediation: str | None
    cwe_id: str | None
    # PR-56 — compliance cross-reference.
    compliance_tags: list[str]
    first_seen_at: datetime
    last_seen_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None


class FindingPatch(BaseModel):
    status: Literal["open", "acknowledged", "resolved", "false_positive"]


def _out(f: Finding) -> FindingOut:
    from app.merge.compliance import compliance_tags
    from app.merge.risk import priority_label

    return FindingOut(
        id=f.id,
        project_id=f.project_id,
        kind=f.kind,
        severity=f.severity,
        status=f.status,
        subject_node_id=f.subject_node_id,
        subject_edge_id=f.subject_edge_id,
        detail=f.detail,
        risk_score=f.risk_score,
        priority=priority_label(f.risk_score),
        remediation=f.remediation,
        cwe_id=f.cwe_id,
        compliance_tags=compliance_tags(f.cwe_id),
        first_seen_at=f.first_seen_at,
        last_seen_at=f.last_seen_at,
        acknowledged_at=f.acknowledged_at,
        resolved_at=f.resolved_at,
    )


@router.get(
    "/api/v1/projects/{project_id}/findings",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def list_findings(
    project_id: uuid.UUID,
    _: CurrentUser,
    severity: str | None = None,
    status: str | None = None,
    sort: str = "risk",
    limit: int = 50,
    db: AsyncSession = Depends(get_session),
) -> list[FindingOut]:
    """List findings, default-ordered by risk score descending.

    PR-50 — the dashboard's whole point is "what should I fix
    first?". Sorting by ``last_seen_at`` (the old default) answered
    "what changed most recently?" instead. ``sort=recent`` keeps
    the old behaviour for anyone who wants it.
    """
    stmt = current_findings_select(project_id).limit(max(1, min(limit, 500)))
    if sort == "recent":
        stmt = stmt.order_by(Finding.last_seen_at.desc())
    else:
        # Risk-first, recency as the tie-breaker.
        stmt = stmt.order_by(
            Finding.risk_score.desc(), Finding.last_seen_at.desc()
        )
    if severity:
        stmt = stmt.where(Finding.severity == severity)
    if status:
        stmt = stmt.where(Finding.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(f) for f in rows]


@router.patch(
    "/api/v1/findings/{finding_id}",
    dependencies=[Depends(require_operator)],
)
async def patch_finding(
    finding_id: uuid.UUID,
    body: FindingPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> FindingOut:
    f = (
        await db.execute(
            select(Finding.project_id).where(Finding.id == finding_id)
        )
    ).one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="not_found")
    # This route keys off finding_id, so require_project_org (which
    # needs a project_id path param) can't gate it — resolve the
    # finding's org by hand and 404 on a cross-org id so a tenant
    # can't mutate, or probe the existence of, another org's finding.
    org_id = await resolve_project_org(db, f.project_id)
    if not same_org(user, org_id):
        raise HTTPException(status_code=404, detail="not_found")
    project_id = f.project_id
    try:
        f = await lock_current_finding_for_action(
            db,
            project_id=project_id,
            finding_id=finding_id,
        )
    except FindingNotCurrent as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=FINDING_NOT_CURRENT_CODE,
        ) from exc
    f.status = body.status
    now = datetime.now(tz=timezone.utc)
    # PR-50 — record the acknowledged → resolved lifecycle timestamps
    # so the dashboard can compute time-to-acknowledge / time-to-
    # resolve (audit B6).
    if body.status == "acknowledged" and f.acknowledged_at is None:
        f.acknowledged_at = now
    if body.status in {"resolved", "false_positive"}:
        f.resolved_at = now
        f.resolved_by = f"user:{user.id}"
    await db.commit()
    await db.refresh(f)
    await audit_record(
        actor=f"user:{user.id}",
        action="finding.update",
        target=str(finding_id),
        project_id=f.project_id,
        details={"status": body.status},
    )
    return _out(f)


@router.post(
    "/api/v1/projects/{project_id}/findings/rebuild",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_operator),
    ],
)
async def rebuild_findings(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    stats = await run_all(db, project_id)
    await audit_record(
        actor=f"user:{user.id}",
        action="finding.rebuild",
        target=str(project_id),
        project_id=project_id,
        details=stats,
    )
    return stats


@router.get(
    "/api/v1/projects/{project_id}/findings/summary",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def findings_summary(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Business-analysis rollup for the project (PR-51, audit C).

    The 19th-round value audit scored "비즈니스 분석 기능" at
    30/100 — the platform had no health overview, no trend, no
    coverage metric. This endpoint is the single call the
    dashboard's "System health" panel makes:

      {
        "total": int,
        "open": int,
        "by_priority": {"P1": n, "P2": n, "P3": n, "P4": n},
        "by_status": {"open": n, "acknowledged": n, "resolved": n,
                      "false_positive": n},
        "by_kind": {"duplicate_endpoint": n, …},
        "risk_index": int,          # 0-100 weighted health number
        "mean_time_to_resolve_h": float | None,
        "open_p1": int,             # the "fix these now" count
        "resolved_last_7d": int,    # progress signal
        "new_last_7d": int,         # influx signal
      }

    ``risk_index`` is the headline: the mean risk_score across all
    *open* findings. A team chasing P1s down sees this number
    fall week over week — that's the ROI signal the audit's C6
    asked for.
    """
    from app.merge.risk import priority_label

    rows = (
        await db.execute(current_findings_select(project_id))
    ).scalars().all()

    by_priority = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    open_risk_total = 0
    open_count = 0
    open_p1 = 0
    resolve_durations: list[float] = []
    now = datetime.now(tz=timezone.utc)
    week_ago = now - timedelta(days=7)
    resolved_last_7d = 0
    new_last_7d = 0

    def _aware(ts: datetime | None) -> datetime | None:
        # PR-138d — SQLite (polyglot) returns naive datetimes; the
        # model declares ``DateTime(timezone=True)`` so Postgres
        # yields aware. Coerce so comparisons with aware ``week_ago``
        # work on both.
        if ts is not None and ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts

    for f in rows:
        by_status[f.status] = by_status.get(f.status, 0) + 1
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        is_open = f.status in {"open", "acknowledged"}
        if is_open:
            open_count += 1
            open_risk_total += f.risk_score
            label = priority_label(f.risk_score)
            by_priority[label] += 1
            if label == "P1":
                open_p1 += 1
        first_seen = _aware(f.first_seen_at)
        resolved = _aware(f.resolved_at)
        if first_seen and first_seen >= week_ago:
            new_last_7d += 1
        if resolved is not None:
            if resolved >= week_ago:
                resolved_last_7d += 1
            if first_seen is not None:
                hours = (resolved - first_seen).total_seconds() / 3600.0
                if hours >= 0:
                    resolve_durations.append(hours)

    mttr = (
        round(sum(resolve_durations) / len(resolve_durations), 1)
        if resolve_durations
        else None
    )
    risk_index = round(open_risk_total / open_count) if open_count else 0

    return {
        "total": len(rows),
        "open": open_count,
        "by_priority": by_priority,
        "by_status": by_status,
        "by_kind": by_kind,
        "risk_index": risk_index,
        "mean_time_to_resolve_h": mttr,
        "open_p1": open_p1,
        "resolved_last_7d": resolved_last_7d,
        "new_last_7d": new_last_7d,
    }


@router.get(
    "/api/v1/projects/{project_id}/findings/trend",
    dependencies=[Depends(require_project_org())],
)
async def findings_trend(
    project_id: uuid.UUID,
    _: CurrentUser,
    days: int = 90,
    bucket_days: int = 7,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Long-horizon trend buckets for the executive report (PR-55,
    audit C2).

    PR-51's summary gave a single 7-day delta — "is the backlog
    growing this week?". The audit's C2 asked for the longer
    horizon: a PM wants the 90-day shape, not a one-week snapshot.

    Returns a list of time buckets, oldest first:

      {
        "buckets": [
          {"start": iso, "end": iso,
           "new": int,        # findings first seen in the bucket
           "resolved": int,   # findings resolved in the bucket
           "net": int},       # new − resolved (positive = growing)
          …
        ],
        "bucket_days": int,
        "total_new": int,
        "total_resolved": int,
      }

    A backlog that's under control shows ``net`` trending toward
    zero or negative across the buckets. A team can paste the
    chart straight into a status update.
    """
    days = max(7, min(days, 365))
    bucket_days = max(1, min(bucket_days, 30))
    now = datetime.now(tz=timezone.utc)
    window_start = now - timedelta(days=days)

    rows = (
        await db.execute(
            select(Finding.first_seen_at, Finding.resolved_at).where(
                Finding.project_id == project_id
            )
        )
    ).all()

    # Build the bucket skeleton.
    n_buckets = (days + bucket_days - 1) // bucket_days
    buckets: list[dict[str, Any]] = []
    for i in range(n_buckets):
        b_start = window_start + timedelta(days=i * bucket_days)
        b_end = b_start + timedelta(days=bucket_days)
        buckets.append(
            {
                "start": b_start.isoformat(),
                "end": b_end.isoformat(),
                "new": 0,
                "resolved": 0,
                "net": 0,
            }
        )

    def _bucket_index(ts: datetime) -> int | None:
        # PR-138d — SQLite (polyglot) returns timezone-naive datetimes
        # since it has no native tz storage. The model declares
        # ``DateTime(timezone=True)`` so Postgres yields aware values.
        # Normalise to UTC-aware so the comparison works in both.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < window_start or ts >= now:
            return None
        delta_days = (ts - window_start).total_seconds() / 86400.0
        idx = int(delta_days // bucket_days)
        return idx if 0 <= idx < n_buckets else None

    total_new = 0
    total_resolved = 0
    for first_seen, resolved in rows:
        if first_seen is not None:
            bi = _bucket_index(first_seen)
            if bi is not None:
                buckets[bi]["new"] += 1
                total_new += 1
        if resolved is not None:
            bi = _bucket_index(resolved)
            if bi is not None:
                buckets[bi]["resolved"] += 1
                total_resolved += 1
    for b in buckets:
        b["net"] = b["new"] - b["resolved"]

    return {
        "buckets": buckets,
        "bucket_days": bucket_days,
        "total_new": total_new,
        "total_resolved": total_resolved,
    }


@router.get(
    "/api/v1/projects/{project_id}/findings/roi",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)
async def findings_roi(
    project_id: uuid.UUID,
    _: CurrentUser,
    days: int = 0,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """ROI rollup — risk eliminated vs LLM spend (audit C6).

    ``project_llm_cost`` answered only the *cost* half of ROI. This
    pairs it with the *benefit* half: the risk-score points removed
    from the system by resolving findings. ``risk_per_usd`` is the
    headline — analysis spend converted into risk eliminated.

    ``precision`` is the analyzer-quality signal: of the findings an
    operator has triaged to a terminal state, how many were real
    (``resolved``) rather than noise (``false_positive``).

    ``days`` windows the flow metrics (risk eliminated, precision,
    spend) to the recent period — ``0`` means all-time. Without it a
    mature project's headline numbers freeze and stop reflecting
    current effort. ``open_risk_remaining`` is a live snapshot only of
    findings validated against the exact ready source+overlay head.
    """
    window_start: datetime | None = None
    if days > 0:
        window_start = datetime.now(tz=timezone.utc) - timedelta(
            days=min(days, 365)
        )

    rows = (
        await db.execute(
            select(
                Finding.status, Finding.risk_score, Finding.resolved_at
            ).where(Finding.project_id == project_id)
        )
    ).all()
    risk_eliminated = 0
    findings_resolved = 0
    false_positive_count = 0
    for status_val, risk, resolved_at in rows:
        risk = int(risk or 0)
        if status_val in {"open", "acknowledged"}:
            # Open risk is computed separately from the exact current-head
            # view. Historical/null-marker open rows must not leak into it.
            continue
        # Terminal states are windowed: a finding triaged outside the
        # window doesn't count toward recent ROI / precision.
        # PR-138d — coerce SQLite (naive) values to UTC so the
        # comparison against aware ``window_start`` works on both
        # backends.
        if resolved_at is not None and resolved_at.tzinfo is None:
            resolved_at = resolved_at.replace(tzinfo=timezone.utc)
        if window_start is not None and (
            resolved_at is None or resolved_at < window_start
        ):
            continue
        if status_val == "resolved":
            risk_eliminated += risk
            findings_resolved += 1
        elif status_val == "false_positive":
            false_positive_count += 1

    open_risk_stmt = (
        current_findings_select(project_id)
        .where(Finding.status.in_(("open", "acknowledged")))
        .with_only_columns(func.coalesce(func.sum(Finding.risk_score), 0))
    )
    open_risk_remaining = int(
        (await db.execute(open_risk_stmt)).scalar_one()
    )

    from app.extractor.cost import project_budget_exposure

    cost = await project_budget_exposure(
        db,
        project_id,
        since=(
            window_start
            if window_start is not None
            else datetime(1970, 1, 1, tzinfo=timezone.utc)
        ),
    )
    estimated_usd = cost.known_spend_usd
    cost_indeterminate = cost.unknown_provider_calls > 0

    triaged = findings_resolved + false_positive_count
    return {
        "window_days": days,
        "risk_eliminated": risk_eliminated,
        "findings_resolved": findings_resolved,
        "open_risk_remaining": open_risk_remaining,
        "false_positive_count": false_positive_count,
        "estimated_usd": estimated_usd,
        "known_spend_usd": estimated_usd,
        "unknown_provider_calls": cost.unknown_provider_calls,
        "unknown_provider_reservation_usd": (
            cost.unknown_provider_exposure_usd
        ),
        "authorized_reservation_usd": cost.authorized_reservation_usd,
        "unpriced_provider_calls": cost.unpriced_provider_calls,
        "cost_indeterminate": cost_indeterminate,
        "risk_per_usd": (
            round(risk_eliminated / estimated_usd, 1)
            if estimated_usd > 0 and not cost_indeterminate
            else None
        ),
        "precision": (
            round(findings_resolved / triaged, 3) if triaged else None
        ),
    }
