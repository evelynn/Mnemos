"""Data entity browsing API.

Week-4 slice only lists DataEntity nodes already in the graph (populated by
the MSSQL live_schema extractor). Sampling, PII masking, and query execution
are the Week-5 deliverables (spec §8, §11.4).
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.db import get_session
from app.models.graph import Node

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/data_entities", tags=["data"]
)


@router.get("")
async def list_data_entities(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "name": (r.data or {}).get("name"),
            "kind": (r.data or {}).get("kind"),
            "component_id": (r.data or {}).get("component_id"),
            "is_sensitive": (r.data or {}).get("is_sensitive", False),
            "certainty": r.certainty,
        }
        for r in rows
    ]


@router.get("/{entity_id:path}")
async def get_data_entity(
    project_id: uuid.UUID,
    entity_id: str,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    node = (
        await db.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {"id": node.id, "data": node.data, "certainty": node.certainty}
