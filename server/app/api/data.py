"""Data entity + sample browsing API (spec §8, §11.4)."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.data_sampler import MaskingEngine, compute_column_stats, mask_rows
from app.db import get_session
from app.models.graph import Node
from app.models.samples import DataQueryLog, DataSample

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

    latest_sample = (
        await db.execute(
            select(DataSample)
            .where(
                DataSample.project_id == project_id,
                DataSample.data_entity_id == entity_id,
            )
            .order_by(DataSample.sampled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return {
        "id": node.id,
        "data": node.data,
        "certainty": node.certainty,
        "sample_available": latest_sample is not None,
        "is_sensitive": (node.data or {}).get("is_sensitive", False),
    }


class SampleIngest(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count_estimate: int | None = None


@router.get("/{entity_id:path}/sample")
async def get_sample(
    project_id: uuid.UUID,
    entity_id: str,
    user: CurrentUser,
    limit: int = 10,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
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
        raise HTTPException(status_code=404, detail="entity_not_found")
    if (node.data or {}).get("is_sensitive"):
        raise HTTPException(status_code=403, detail="sensitive_entity_sample_disallowed")

    sample = (
        await db.execute(
            select(DataSample)
            .where(
                DataSample.project_id == project_id,
                DataSample.data_entity_id == entity_id,
            )
            .order_by(DataSample.sampled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sample is None:
        raise HTTPException(status_code=404, detail="no_sample_yet")

    rows = sample.sample_rows[:limit] if isinstance(sample.sample_rows, list) else []
    await audit_record(
        actor=f"user:{user.id}",
        action="data.sample_view",
        target=entity_id,
        project_id=project_id,
        details={"limit": limit},
    )
    return {
        "entity_id": entity_id,
        "rows": rows,
        "row_count_estimate": sample.row_count_estimate,
        "column_stats": sample.column_stats,
        "sampled_at": sample.sampled_at,
        "masking_applied": sample.masking_applied,
    }


@router.post("/{entity_id:path}/refresh_sample")
async def refresh_sample(
    project_id: uuid.UUID,
    entity_id: str,
    body: SampleIngest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Ingest a freshly-captured raw sample.

    The caller (analyzer runner / manual trigger) supplies pre-fetched rows;
    the platform performs masking and stat computation server-side so raw
    values never linger in storage.
    """
    entity = (
        await db.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=404, detail="entity_not_found")
    if (entity.data or {}).get("is_sensitive"):
        raise HTTPException(
            status_code=403, detail="sensitive_entity_sample_disallowed"
        )

    masked_rows, _col_flags, any_masked = mask_rows(body.columns, body.rows)
    stats = compute_column_stats(body.columns, masked_rows)

    sample = DataSample(
        project_id=project_id,
        data_entity_id=entity_id,
        sample_rows=[
            {col: row[i] for i, col in enumerate(body.columns)} for row in masked_rows
        ],
        row_count_estimate=body.row_count_estimate,
        column_stats=stats,
        masking_applied=any_masked,
    )
    db.add(sample)
    await db.commit()
    await db.refresh(sample)
    await audit_record(
        actor=f"user:{user.id}",
        action="data.sample_refresh",
        target=entity_id,
        project_id=project_id,
        details={"rows": len(masked_rows), "masking_applied": any_masked},
    )
    return {
        "id": str(sample.id),
        "rows": len(masked_rows),
        "masking_applied": any_masked,
        "sampled_at": sample.sampled_at,
    }


query_router = APIRouter(prefix="/api/v1/projects/{project_id}/data", tags=["data"])


class QueryRequest(BaseModel):
    db_component_id: str
    columns: list[str] = Field(description="Pre-validated column names")
    rows: list[list[Any]] = Field(description="Pre-executed result rows")
    sql: str
    purpose: str = Field(min_length=3)
    execution_ms: int | None = None


@query_router.post("/query")
async def query_data(
    project_id: uuid.UUID,
    body: QueryRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Accept a pre-executed read-only query result, mask it, and log it.

    The actual SELECT is run by the analyzer container (ggoss-sql-mssql /
    ggoss-sql-oracle ``query``) so the platform's own process stays isolated
    from production DBs; this endpoint is the guarded landing zone.
    """
    if not body.sql.strip().lower().startswith("select"):
        raise HTTPException(status_code=400, detail="only_select_allowed")

    masked_rows, _flags, any_masked = mask_rows(body.columns, body.rows)

    log = DataQueryLog(
        project_id=project_id,
        db_component_id=body.db_component_id,
        sql=body.sql,
        purpose=body.purpose,
        requester=f"user:{user.id}",
        row_count=len(masked_rows),
        execution_ms=body.execution_ms,
    )
    db.add(log)
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="data.query",
        target=body.db_component_id,
        project_id=project_id,
        details={"purpose": body.purpose, "rows": len(masked_rows)},
    )
    return {
        "columns": body.columns,
        "rows": masked_rows,
        "row_count": len(masked_rows),
        "execution_ms": body.execution_ms,
        "masking_applied": any_masked,
        "executed_at": datetime.utcnow(),
    }
