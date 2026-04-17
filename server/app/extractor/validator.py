"""Claim validator (spec §10.4).

Every evidence reference (node_id / edge_id) must exist in the current
graph. Failing claims are dropped; if *all* claims fail, the caller should
retry the extractor.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Edge, Node


async def validate_claims(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    claims: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for claim in claims:
        evidence = claim.get("evidence") or []
        ok = True
        for ev in evidence:
            kind = ev.get("kind")
            if kind == "node":
                node_id = ev.get("node_id")
                if not node_id:
                    ok = False
                    break
                exists = (
                    await session.execute(
                        select(Node.id).where(
                            Node.project_id == project_id,
                            Node.id == node_id,
                            Node.valid_to.is_(None),
                        )
                    )
                ).first()
                if exists is None:
                    ok = False
                    break
            elif kind == "edge":
                raw = ev.get("edge_id")
                try:
                    edge_id = uuid.UUID(str(raw))
                except (ValueError, TypeError):
                    ok = False
                    break
                exists = (
                    await session.execute(
                        select(Edge.id).where(
                            Edge.id == edge_id, Edge.valid_to.is_(None)
                        )
                    )
                ).first()
                if exists is None:
                    ok = False
                    break
            else:
                ok = False
                break
        (accepted if ok else rejected).append(claim)
    return accepted, rejected
