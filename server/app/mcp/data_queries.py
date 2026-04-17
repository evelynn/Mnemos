"""Data-tool queries shared by MCP and HTTP surfaces."""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Node
from app.models.samples import DataSample


async def get_data_entity(
    session: AsyncSession, *, project_id: uuid.UUID, entity_id: str
) -> dict[str, Any] | None:
    node = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        return None
    latest = (
        await session.execute(
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
        "entity": node.data,
        "certainty": node.certainty,
        "sample_available": latest is not None,
        "is_sensitive": (node.data or {}).get("is_sensitive", False),
    }


async def get_sample_data(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    entity_id: str,
    limit: int = 10,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    node = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        return {"error": "entity_not_found"}
    if (node.data or {}).get("is_sensitive"):
        return {"error": "sensitive_entity_sample_disallowed"}
    sample = (
        await session.execute(
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
        return {"error": "no_sample_yet"}
    rows = sample.sample_rows[:limit] if isinstance(sample.sample_rows, list) else []
    return {
        "columns": list(rows[0].keys()) if rows else [],
        "rows": rows,
        "row_count_estimate": sample.row_count_estimate,
        "sampled_at": sample.sampled_at,
        "masking_applied": sample.masking_applied,
    }


async def get_column_stats(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    entity_id: str,
    column: str,
) -> dict[str, Any]:
    sample = (
        await session.execute(
            select(DataSample)
            .where(
                DataSample.project_id == project_id,
                DataSample.data_entity_id == entity_id,
            )
            .order_by(DataSample.sampled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sample is None or sample.column_stats is None:
        return {"error": "no_stats"}
    return sample.column_stats.get(column, {"error": "column_not_in_stats"})


async def search_data(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    value_pattern: str,
    max_hits: int = 50,
) -> list[dict[str, Any]]:
    pattern = re.compile(value_pattern)
    samples = (
        await session.execute(
            select(DataSample).where(DataSample.project_id == project_id).limit(200)
        )
    ).scalars().all()
    hits: list[dict[str, Any]] = []
    for sample in samples:
        rows = sample.sample_rows or []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for col, val in row.items():
                if val is None:
                    continue
                if pattern.search(str(val)):
                    hits.append(
                        {
                            "entity_id": sample.data_entity_id,
                            "column": col,
                            "sample_row_index": idx,
                            "masked_snippet": str(val)[:80],
                        }
                    )
                    if len(hits) >= max_hits:
                        return hits
    return hits
