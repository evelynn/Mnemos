"""Canonical graph-revision binding for Gate-B review decisions.

Ultrareview reads several graph-backed passes under PostgreSQL READ COMMITTED.
The result is safe to persist or act on only when the ready graph head is
unchanged after the final pass.  This module provides the one read/lock/bind
contract shared by diff submission, break-glass reruns, and approval.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph_publication import (
    GraphGenerationChanged,
    GraphPublicationInvariantError,
    GraphReadStamp,
    lock_validated_graph_head,
    read_graph_stamp,
)
from app.models.graph import AnalysisRun


_CANONICAL_GIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")


class ReviewRevisionStale(RuntimeError):
    """A stored verdict or grant does not describe the current graph."""


@dataclass(frozen=True)
class CanonicalReviewRevision:
    """Exact source and durable-overlay revision used by one review."""

    project_id: uuid.UUID
    run_id: uuid.UUID
    source_generation: int
    overlay_generation: int
    git_sha: str


async def read_review_graph_stamp(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> GraphReadStamp:
    """Pin the canonical revision before a multi-pass review starts."""

    return await read_graph_stamp(session, project_id=project_id)


async def resolve_review_revision_from_stamp(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    stamp: GraphReadStamp,
) -> CanonicalReviewRevision:
    """Resolve immutable review provenance for a previously read graph stamp.

    This does not lock the graph.  It is the pre-provider binding used by the
    Gate-B product identity; callers still perform
    :func:`lock_review_graph_revision` after the review and keep that lock
    through the decision write.  A publication race therefore wastes at most
    one old-revision product and can never authorize it for the new revision.
    """

    if (
        stamp.project_id != project_id
        or type(stamp.generation) is not int
        or stamp.generation < 1
        or type(stamp.overlay_generation) is not int
        or stamp.overlay_generation < 0
    ):
        raise GraphPublicationInvariantError(
            "Gate-B graph stamp is not a canonical ready revision"
        )
    current_run_id = stamp.current_run_id
    if not isinstance(current_run_id, uuid.UUID):
        try:
            run_id = uuid.UUID(str(current_run_id))
        except (TypeError, ValueError) as exc:
            raise GraphPublicationInvariantError(
                "canonical graph revision has an invalid run id"
            ) from exc
    else:
        run_id = current_run_id
    git_sha = (
        await session.execute(
            select(AnalysisRun.git_sha).where(
                AnalysisRun.id == run_id,
                AnalysisRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if not isinstance(git_sha, str) or _CANONICAL_GIT_SHA.fullmatch(git_sha) is None:
        raise GraphPublicationInvariantError(
            "canonical graph revision has no full lowercase hexadecimal git_sha"
        )
    return CanonicalReviewRevision(
        project_id=project_id,
        run_id=run_id,
        source_generation=stamp.generation,
        overlay_generation=stamp.overlay_generation,
        git_sha=git_sha,
    )


async def lock_review_graph_revision(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    expected_stamp: GraphReadStamp | None = None,
) -> CanonicalReviewRevision:
    """Lock GraphHead -> AnalysisRun and return its validated revision.

    When ``expected_stamp`` is supplied, this is the post-compute recheck.
    The caller must keep the transaction open through the decision write (or
    external approval side effect) and commit; publication and overlay writers
    use the same GraphHead lock, so the revision cannot change in that window.
    """

    head, receipt = await lock_validated_graph_head(
        session,
        project_id=project_id,
    )
    current_stamp = GraphReadStamp(
        project_id=project_id,
        generation=receipt.generation,
        overlay_generation=head.overlay_generation,
        current_run_id=head.current_run_id,
        published_at=receipt.published_at,
    )
    if expected_stamp is not None and current_stamp != expected_stamp:
        raise GraphGenerationChanged(
            "graph revision changed while Gate-B review was running: "
            f"source {expected_stamp.generation}->{current_stamp.generation}, "
            "overlay "
            f"{expected_stamp.overlay_generation}->"
            f"{current_stamp.overlay_generation}"
        )

    return await resolve_review_revision_from_stamp(
        session,
        project_id=project_id,
        stamp=current_stamp,
    )


def bind_review_revision(
    target: Any,
    revision: CanonicalReviewRevision,
) -> None:
    """Persist one canonical revision on a submission or grant ORM row."""

    target.review_run_id = revision.run_id
    target.review_source_generation = revision.source_generation
    target.review_overlay_generation = revision.overlay_generation
    target.review_git_sha = revision.git_sha


def review_revision_matches(
    target: Any,
    revision: CanonicalReviewRevision,
) -> bool:
    """Return true only for an exact, fully populated provenance match."""

    return (
        target.review_run_id == revision.run_id
        and type(target.review_source_generation) is int
        and target.review_source_generation == revision.source_generation
        and type(target.review_overlay_generation) is int
        and target.review_overlay_generation == revision.overlay_generation
        and isinstance(target.review_git_sha, str)
        and target.review_git_sha == revision.git_sha
    )


def require_review_revision(
    target: Any,
    revision: CanonicalReviewRevision,
) -> None:
    """Fail closed when a verdict/grant predates the canonical revision."""

    if not review_revision_matches(target, revision):
        raise ReviewRevisionStale(
            "stored Gate-B decision does not match the canonical graph revision"
        )
