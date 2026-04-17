"""Shared async query helpers backing both MCP tools and HTTP endpoints.

Keeping the logic in one place means the MCP surface and the GUI surface
cannot drift — both call the same helpers.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Edge, Node


async def search_symbols(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    query: str,
    kind: str | None = None,
    top_k: int = 20,
) -> list[dict[str, Any]]:
    top_k = max(1, min(top_k, 200))
    stmt = (
        select(Node)
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
        .limit(top_k)
    )
    if kind:
        stmt = stmt.where(Node.kind == kind)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(or_(Node.id.ilike(like), Node.data["name"].astext.ilike(like)))
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "symbol_id": r.id,
            "name": (r.data or {}).get("name"),
            "component_id": (r.data or {}).get("component_id"),
            "kind": r.kind,
            "certainty": r.certainty,
            "excerpt": (r.data or {}).get("signature"),
        }
        for r in rows
    ]


async def get_symbol(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    symbol_id: str,
) -> dict[str, Any] | None:
    node = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == symbol_id,
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        return None
    callers = (
        await session.execute(
            select(Edge.id)
            .where(
                Edge.project_id == project_id,
                Edge.target_id == symbol_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
            )
            .limit(1001)
        )
    ).all()
    callees = (
        await session.execute(
            select(Edge.id)
            .where(
                Edge.project_id == project_id,
                Edge.source_id == symbol_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
            )
            .limit(1001)
        )
    ).all()
    return {
        "symbol": {
            "id": node.id,
            "kind": node.kind,
            "data": node.data,
            "certainty": node.certainty,
        },
        "neighbors": {
            "callers_count": len(callers),
            "callees_count": len(callees),
        },
    }


async def find_callers(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    symbol_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    rows = (
        await session.execute(
            select(Edge)
            .where(
                and_(
                    Edge.project_id == project_id,
                    Edge.target_id == symbol_id,
                    Edge.kind == "CALLS",
                    Edge.valid_to.is_(None),
                )
            )
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "caller_id": e.source_id,
            "callee_id": e.target_id,
            "certainty": e.certainty,
            "site": (e.data or {}).get("invocation_site"),
        }
        for e in rows
    ]
