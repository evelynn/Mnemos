"""One fail-closed query boundary for current graph-derived findings."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph_publication import GraphPublicationError, lock_validated_graph_head
from app.models.findings import Finding
from app.models.graph import GraphHead

FINDING_NOT_CURRENT_CODE = "finding_not_current"


class FindingNotCurrent(RuntimeError):
    """The requested action is not pinned to the canonical ready graph."""


def current_findings_select(project_id: uuid.UUID):
    """Select findings validated against the exact ready source+overlay head.

    Legacy rows and results from a failed/partial postprocess have null or old
    markers and are intentionally absent. Historical and explicit-id readers
    must query ``Finding`` directly instead of using this current-state view.
    """

    return (
        select(Finding)
        .join(GraphHead, GraphHead.project_id == Finding.project_id)
        .where(
            Finding.project_id == project_id,
            GraphHead.state == "ready",
            Finding.validated_graph_generation == GraphHead.generation,
            Finding.validated_overlay_generation
            == GraphHead.overlay_generation,
            Finding.validated_at.is_not(None),
        )
    )


async def lock_current_finding_for_action(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
) -> Finding:
    """Lock and validate one finding at the graph-mutation boundary.

    The caller must resolve tenant ownership before invoking this helper.
    Graph writers serialize in ``GraphHead -> AnalysisRun -> derived row``
    order, so actions use the same order.  Holding the head lock through the
    caller's commit prevents a source promotion, overlay update, or findings
    rebuild from advancing the revision between this check and the mutation.

    Legacy/null-marker findings and findings from any older source or overlay
    revision intentionally fail closed.  The public API maps every failure to
    the same stable conflict response so receipt/marker details are not an
    externally observable side channel.
    """

    try:
        head, receipt = await lock_validated_graph_head(
            session,
            project_id=project_id,
        )
    except GraphPublicationError as exc:
        raise FindingNotCurrent("canonical graph publication unavailable") from exc

    finding = (
        await session.execute(
            select(Finding)
            .where(
                Finding.id == finding_id,
                Finding.project_id == project_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if finding is None:
        raise FindingNotCurrent("finding disappeared before action lock")

    if (
        finding.validated_at is None
        or finding.validated_graph_generation != receipt.generation
        or finding.validated_overlay_generation != head.overlay_generation
    ):
        raise FindingNotCurrent("finding does not match canonical graph revision")
    return finding
