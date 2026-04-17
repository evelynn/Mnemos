"""Drive the extractor across L1-L3.

L1: per-Symbol node summary from the symbol + its 1-hop neighbours.
L2: per-source-file summary built from the file's L1 summaries.
L3: per-module/Component summary from its L2 summaries + cross-component edges.

L4/L5 are Phase-2 work (spec §15.4).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.agent import Extractor
from app.extractor.validator import validate_claims
from app.models.findings import Summary
from app.models.graph import Edge, Node


async def _supersede_current(
    session: AsyncSession, project_id: uuid.UUID, target_id: str, level: int
) -> None:
    await session.execute(
        Summary.__table__.update()
        .where(
            and_(
                Summary.project_id == project_id,
                Summary.target_id == target_id,
                Summary.level == level,
                Summary.superseded_by.is_(None),
            )
        )
        .values(superseded_by=uuid.uuid4())
    )


async def summarise_l1(
    session: AsyncSession,
    extractor: Extractor,
    *,
    project_id: uuid.UUID,
    limit: int = 25,
) -> int:
    symbols = (
        await session.execute(
            select(Node)
            .where(
                Node.project_id == project_id,
                Node.kind == "Symbol",
                Node.valid_to.is_(None),
            )
            .limit(limit)
        )
    ).scalars().all()

    count = 0
    for sym in symbols:
        neighbours_out = (
            await session.execute(
                select(Edge)
                .where(
                    Edge.project_id == project_id,
                    Edge.source_id == sym.id,
                    Edge.valid_to.is_(None),
                )
                .limit(10)
            )
        ).scalars().all()
        neighbours_in = (
            await session.execute(
                select(Edge)
                .where(
                    Edge.project_id == project_id,
                    Edge.target_id == sym.id,
                    Edge.valid_to.is_(None),
                )
                .limit(10)
            )
        ).scalars().all()
        evidence: list[dict[str, Any]] = [
            {
                "kind": "node",
                "node_id": sym.id,
                "data": sym.data,
                "certainty": sym.certainty,
            }
        ]
        for e in [*neighbours_in, *neighbours_out]:
            evidence.append(
                {
                    "kind": "edge",
                    "edge_id": str(e.id),
                    "edge_kind": e.kind,
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                    "certainty": e.certainty,
                }
            )

        result = await extractor.summarize(1, sym.id, evidence)
        accepted, _rejected = await validate_claims(
            session, project_id=project_id, claims=result.claims
        )

        await _supersede_current(session, project_id, sym.id, 1)
        session.add(
            Summary(
                project_id=project_id,
                target_id=sym.id,
                level=1,
                summary=result.summary,
                detailed=result.detailed,
                claims=accepted,
                open_questions=result.open_questions,
                model_used=result.model_used,
                tokens_used=result.tokens_used,
                generated_at=datetime.now(tz=timezone.utc),
            )
        )
        count += 1
    await session.commit()
    return count
