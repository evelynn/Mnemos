"""Generation authority for persisted optional narratives.

Summary prose is a cache over graph evidence, not source truth. A writer takes
an exclusive lock on the ready GraphHead immediately before validating or
inserting a marker. This both linearizes the marker with source/overlay
publication and serializes the short final write section so concurrent writers
cannot leave two unsuperseded summaries for one logical target.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.graph_publication import (
    GraphGenerationChanged,
    lock_validated_graph_head,
)


@dataclass(frozen=True)
class SummaryGraphRevision:
    source_generation: int
    overlay_generation: int


async def lock_ready_summary_generation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    expected_generation: int | None = None,
    expected_overlay_generation: int | None = None,
) -> SummaryGraphRevision:
    """Lock and return the source generation authorized for a Summary write.

    PostgreSQL renders this as ``FOR UPDATE``. Provider work happens before
    this boundary, so only the short validation/supersede/insert transaction is
    serialized per project. SQLite ignores the lock clause, but its serialized
    write transaction, the generation equality check, and the partial unique
    index preserve local mode's fail-closed behavior.
    """

    head, publication = await lock_validated_graph_head(
        session, project_id=project_id
    )
    generation = publication.generation
    overlay_generation = head.overlay_generation
    if expected_generation is not None and generation != expected_generation:
        raise GraphGenerationChanged(
            "graph source generation changed before summary validation: "
            f"{expected_generation}->{generation}"
        )
    if (
        expected_overlay_generation is not None
        and overlay_generation != expected_overlay_generation
    ):
        raise GraphGenerationChanged(
            "graph overlay generation changed before summary validation: "
            f"{expected_overlay_generation}->{overlay_generation}"
        )
    return SummaryGraphRevision(
        source_generation=generation,
        overlay_generation=overlay_generation,
    )
