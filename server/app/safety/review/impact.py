"""Impact pass.

Reads the Plan's impact_report and warns if the diff unexpectedly affects
any opaque components or high-degree symbols that weren't declared. High
callers count (≥20) means lots of downstream callers depend on the
behaviour, so a change here is a `warning` at minimum.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Edge
from app.models.plans import Plan
from app.safety.review.types import Finding


async def run(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    diff: str,  # noqa: ARG001
) -> list[Finding]:
    plan = (
        await session.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()
    if plan is None:
        return []
    report = plan.impact_report or {}

    opaque = report.get("opaque_components_touched") or []
    directly = report.get("directly_affected") or []
    findings: list[Finding] = []

    for comp in opaque:
        findings.append(
            Finding(
                pass_name="impact",
                severity="warning",
                rule="touches_opaque_component",
                location=comp,
                message=(
                    f"Impact report flagged opaque component {comp}. Confirm "
                    f"we only call through published contracts."
                ),
                evidence=[{"kind": "node", "node_id": comp, "certainty": "asserted"}],
            )
        )

    for sym in directly[:50]:
        count = (
            await session.execute(
                select(func.count()).select_from(Edge).where(
                    Edge.project_id == project_id,
                    Edge.target_id == sym,
                    Edge.kind == "CALLS",
                    Edge.valid_to.is_(None),
                )
            )
        ).scalar_one()
        if count and count >= 20:
            findings.append(
                Finding(
                    pass_name="impact",
                    severity="warning",
                    rule="high_fan_in_target",
                    location=sym,
                    message=(
                        f"{sym} has {count} known callers. Behaviour changes "
                        f"propagate widely; call for extra test coverage."
                    ),
                    evidence=[{"kind": "node", "node_id": sym, "certainty": "asserted"}],
                )
            )
    return findings
