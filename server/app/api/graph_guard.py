"""Pin and revalidate the atomic graph head around HTTP graph consumers."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.graph_publication import (
    GraphGenerationChanged,
    GraphHeadMissing,
    GraphHeadNeedsRebuild,
    GraphPublicationInvariantError,
    read_graph_stamp,
    revalidate_graph_stamp,
)

GRAPH_SNAPSHOT_UNAVAILABLE_CODE = "graph_snapshot_unavailable"
GRAPH_SNAPSHOT_CHANGED_CODE = "graph_snapshot_changed_retry"


async def require_readable_current_graph(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> AsyncIterator[None]:
    """Reject an unavailable head or a response that crosses publication.

    PostgreSQL ``READ COMMITTED`` gives each statement its own snapshot.  A
    one-shot preflight check can therefore observe generation N while later
    queries observe N+1.  Routes install this dependency with
    ``scope="function"`` so the second head read runs after the handler has
    fully materialized its result but before FastAPI sends the response.  If a
    source promotion or human/runtime overlay committed between those reads,
    the mixed result is discarded and the caller receives a retryable 409.

    A failed *staged* refresh does not change ``GraphHead`` and consequently
    does not make the last published graph unavailable.
    """

    try:
        stamp = await read_graph_stamp(db, project_id=project_id)
    except (GraphHeadMissing, GraphHeadNeedsRebuild, GraphPublicationInvariantError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "schema": "mnemos.error.v1",
                "error": GRAPH_SNAPSHOT_UNAVAILABLE_CODE,
                "reason": GRAPH_SNAPSHOT_UNAVAILABLE_CODE,
                "repair": {
                    "scope": "full",
                    "reason": "no atomically published graph is ready",
                },
            },
        ) from exc

    try:
        yield
    except BaseException:
        # Preserve the handler's own error.  No graph result will be returned,
        # so replacing it with a generation diagnostic would hide the cause.
        raise
    else:
        try:
            await revalidate_graph_stamp(db, stamp=stamp)
        except GraphGenerationChanged as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "schema": "mnemos.error.v1",
                    "error": GRAPH_SNAPSHOT_CHANGED_CODE,
                    "reason": GRAPH_SNAPSHOT_CHANGED_CODE,
                    "retryable": True,
                    "snapshot": {
                        "generation": stamp.generation,
                        "overlay_generation": stamp.overlay_generation,
                        "run_id": str(stamp.current_run_id),
                        "published_at": stamp.published_at.isoformat(),
                    },
                },
            ) from exc
