"""Fail-closed source revision binding for development plans.

A Plan authorises edits against one immutable source publication.  The
publication pointer is mutable, so every write boundary locks and validates
the canonical GraphHead + AnalysisRun receipt before it trusts a Plan.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph_publication import (
    GraphPublicationError,
    lock_validated_graph_head,
)
from app.models.graph import AnalysisRun
from app.models.plans import Plan

PLAN_SOURCE_REVISION_STALE_CODE = "plan_source_revision_stale"
PLAN_SOURCE_REVISION_UNAVAILABLE_CODE = "plan_source_revision_unavailable"
PLAN_BASE_REVISION_MISMATCH_CODE = "plan_base_revision_mismatch"

_CANONICAL_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")


class PlanSourceRevisionError(RuntimeError):
    """Base class for fail-closed Plan provenance failures."""


class PlanSourceRevisionUnavailable(PlanSourceRevisionError):
    """The current publication cannot authorise a source worktree."""


class PlanBaseRevisionMismatch(PlanSourceRevisionError):
    """A caller requested a revision other than the current publication."""


class PlanSourceRevisionStale(PlanSourceRevisionError):
    """A persisted Plan no longer matches the canonical publication."""


@dataclass(frozen=True)
class PlanSourceRevision:
    """Canonical source and graph identity persisted on every new Plan."""

    run_id: uuid.UUID
    git_sha: str
    source_generation: int
    overlay_generation: int


def _run_uuid(value: object) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PlanSourceRevisionUnavailable(
            "current publication run id is invalid"
        ) from exc


async def lock_current_plan_source_revision(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    requested_base_sha: str | None = None,
) -> PlanSourceRevision:
    """Lock and return the one source revision allowed for a new Plan.

    ``lock_validated_graph_head`` owns the receipt contract.  This boundary
    adds the source commit, which must be a full canonical Git object id;
    mutable/non-Git publications deliberately cannot authorise code edits.
    The GraphHead and AnalysisRun locks remain held until the caller commits
    or rolls back the transaction.
    """

    try:
        head, receipt = await lock_validated_graph_head(
            session,
            project_id=project_id,
        )
    except GraphPublicationError as exc:
        raise PlanSourceRevisionUnavailable(str(exc)) from exc

    run = (
        await session.execute(
            select(AnalysisRun).where(
                AnalysisRun.id == head.current_run_id,
                AnalysisRun.project_id == project_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        raise PlanSourceRevisionUnavailable(
            "current publication run disappeared while locked"
        )
    git_sha = run.git_sha
    if not isinstance(git_sha, str) or not _CANONICAL_COMMIT_RE.fullmatch(git_sha):
        raise PlanSourceRevisionUnavailable(
            "current publication has no canonical full Git commit"
        )
    if requested_base_sha is not None and requested_base_sha != git_sha:
        raise PlanBaseRevisionMismatch(
            "requested base_sha does not match the current published git_sha"
        )
    if type(head.overlay_generation) is not int or head.overlay_generation < 0:
        # The graph helper already enforces this. Keep the Plan boundary
        # self-contained if that helper's return contract changes later.
        raise PlanSourceRevisionUnavailable(
            "current publication overlay generation is invalid"
        )
    return PlanSourceRevision(
        run_id=_run_uuid(run.id),
        git_sha=git_sha,
        source_generation=receipt.generation,
        overlay_generation=head.overlay_generation,
    )


def bind_plan_source_revision(plan: Plan, revision: PlanSourceRevision) -> None:
    """Persist a validated canonical revision on a new Plan."""

    plan.source_run_id = revision.run_id
    plan.source_git_sha = revision.git_sha
    plan.source_graph_generation = revision.source_generation
    plan.source_overlay_generation = revision.overlay_generation


def require_plan_source_revision(
    plan: Plan,
    current: PlanSourceRevision,
) -> None:
    """Reject legacy, malformed, cross-project, or stale Plan provenance."""

    try:
        plan_run_id = _run_uuid(plan.source_run_id)
    except PlanSourceRevisionUnavailable as exc:
        raise PlanSourceRevisionStale(
            "plan has no valid source publication provenance"
        ) from exc
    if (
        not isinstance(plan.source_git_sha, str)
        or not _CANONICAL_COMMIT_RE.fullmatch(plan.source_git_sha)
        or type(plan.source_graph_generation) is not int
        or plan.source_graph_generation <= 0
        or type(plan.source_overlay_generation) is not int
        or plan.source_overlay_generation < 0
    ):
        raise PlanSourceRevisionStale(
            "plan has malformed source publication provenance"
        )
    actual = (
        plan_run_id,
        plan.source_git_sha,
        plan.source_graph_generation,
        plan.source_overlay_generation,
    )
    expected = (
        current.run_id,
        current.git_sha,
        current.source_generation,
        current.overlay_generation,
    )
    if actual != expected:
        raise PlanSourceRevisionStale(
            "plan source revision no longer matches the current publication"
        )


async def lock_current_plan_for_action(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    project_id: uuid.UUID,
) -> tuple[Plan, PlanSourceRevision]:
    """Lock GraphHead -> AnalysisRun -> Plan and verify exact provenance."""

    current = await lock_current_plan_source_revision(
        session,
        project_id=project_id,
    )
    plan = (
        await session.execute(
            select(Plan)
            .where(Plan.id == plan_id, Plan.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise PlanSourceRevisionStale("plan disappeared while acquiring its lock")
    require_plan_source_revision(plan, current)
    return plan, current


__all__ = [
    "PLAN_BASE_REVISION_MISMATCH_CODE",
    "PLAN_SOURCE_REVISION_STALE_CODE",
    "PLAN_SOURCE_REVISION_UNAVAILABLE_CODE",
    "PlanBaseRevisionMismatch",
    "PlanSourceRevision",
    "PlanSourceRevisionError",
    "PlanSourceRevisionStale",
    "PlanSourceRevisionUnavailable",
    "bind_plan_source_revision",
    "lock_current_plan_for_action",
    "lock_current_plan_source_revision",
    "require_plan_source_revision",
]
