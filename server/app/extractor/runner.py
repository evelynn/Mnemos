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

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.agent import (
    Extractor,
    ExtractorResult,
)
from app.extractor.cost import BudgetExceeded, require_budget
from app.extractor.packing import evidence_hash, pack_by_budget
from app.extractor.validator import validate_claims
from app.models.findings import Summary
from app.models.graph import Edge, Node

_HASH_KEY = "_evidence_hash"
FALLBACK_BUDGET_EXCEEDED = "budget_exceeded"


async def _summarize_with_budget(
    session: AsyncSession,
    extractor: Extractor,
    project_id: uuid.UUID,
    level: int,
    target_id: str,
    evidence: list[dict[str, Any]],
) -> ExtractorResult:
    """Run the extractor with the budget guard pre-check.

    When the project has exhausted its rolling-window cap (cap > 0,
    spend ≥ cap), we skip the LLM call and synthesise a stub result
    with a clearly-labelled reason. Operators see this on
    ``summaries.model_used`` and the Prometheus counter (PR-138).

    Default deployments leave ``MNEMOS_LLM_BUDGET_USD_PER_PROJECT``
    at 0 (disabled), so this guard is a no-op until an operator
    explicitly opts in.
    """
    try:
        await require_budget(session, project_id)
    except BudgetExceeded:
        # Fall through to the stub path; tick the same counter the
        # silent-fallback path uses so the operator sees a unified
        # "this run did NOT hit the LLM" metric.
        try:
            from app.obs.metrics import llm_fallback_total
            llm_fallback_total.labels(
                **{"from": "extractor", "reason": FALLBACK_BUDGET_EXCEEDED}
            ).inc()
        except Exception:  # noqa: BLE001
            pass
        return Extractor._stub(
            level, target_id, evidence, FALLBACK_BUDGET_EXCEEDED,
        )
    return await extractor.summarize(level, target_id, evidence)


async def _current_summary(
    session: AsyncSession,
    project_id: uuid.UUID,
    target_id: str,
    level: int,
) -> Summary | None:
    return (
        await session.execute(
            select(Summary).where(
                Summary.project_id == project_id,
                Summary.target_id == target_id,
                Summary.level == level,
                Summary.superseded_by.is_(None),
            )
        )
    ).scalar_one_or_none()


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


def _unchanged(prev: Summary | None, evidence: list[dict[str, Any]]) -> bool:
    """True if the previous summary's evidence hash matches the new evidence."""
    if prev is None or not prev.claims:
        return False
    prev_hash = None
    for c in prev.claims:
        if isinstance(c, dict) and c.get("claim") == _HASH_KEY:
            prev_hash = c.get("evidence", [{}])[0].get("node_id")
            break
    if prev_hash is None:
        return False
    return prev_hash == evidence_hash(evidence)


def _stamp_hash(
    claims: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Embed the evidence hash as a synthetic claim so later runs can skip."""
    stamp = {
        "claim": _HASH_KEY,
        "evidence": [
            {"kind": "node", "node_id": evidence_hash(evidence), "certainty": "asserted"}
        ],
    }
    return [*claims, stamp]


async def _priority_symbols(
    session: AsyncSession, project_id: uuid.UUID, limit: int
) -> list[Node]:
    """Rank candidates for L1: entry points first, then by caller degree.

    A large codebase has 100k+ symbols; the operator-visible first pass must
    cover the useful surface (HTTP contracts, controllers, background jobs)
    before grinding through private helpers.
    """
    # Prefer symbols with data.is_entry_point=true or targeted by EXPOSES.
    entry_rows = (
        await session.execute(
            select(Node)
            .where(
                Node.project_id == project_id,
                Node.kind == "Symbol",
                Node.valid_to.is_(None),
                Node.data["is_entry_point"].astext == "true",
            )
            .limit(limit)
        )
    ).scalars().all()

    if len(entry_rows) >= limit:
        return entry_rows[:limit]

    # Top up by in-degree (count of CALLS edges whose target is this symbol).
    deg_stmt = (
        select(Edge.target_id, func.count().label("deg"))
        .where(
            Edge.project_id == project_id,
            Edge.kind == "CALLS",
            Edge.valid_to.is_(None),
        )
        .group_by(Edge.target_id)
        .order_by(func.count().desc())
        .limit(limit * 3)
    )
    top_ids = [r[0] for r in (await session.execute(deg_stmt)).all()]
    seen = {n.id for n in entry_rows}
    wanted = [i for i in top_ids if i not in seen][: limit - len(entry_rows)]

    high_deg_rows = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.kind == "Symbol",
                Node.valid_to.is_(None),
                Node.id.in_(wanted),
            )
        )
    ).scalars().all() if wanted else []

    combined = [*entry_rows, *high_deg_rows]
    if len(combined) >= limit:
        return combined[:limit]

    # Final top-up: plain lexical scan. Still cheap because of the current-row index.
    filler_stmt = (
        select(Node)
        .where(
            Node.project_id == project_id,
            Node.kind == "Symbol",
            Node.valid_to.is_(None),
        )
        .limit(limit)
    )
    filler = (await session.execute(filler_stmt)).scalars().all()
    seen = {n.id for n in combined}
    for n in filler:
        if n.id in seen:
            continue
        combined.append(n)
        if len(combined) >= limit:
            break
    return combined[:limit]


async def _priority_data_entities(
    session: AsyncSession, project_id: uuid.UUID, limit: int
) -> list[Node]:
    """Rank DataEntity (table/view) nodes for L1 summarisation by how many
    things touch them (incoming READS / WRITES / REFERENCES edges) — the
    busiest tables matter most — then top up with any remaining entities."""
    if limit <= 0:
        return []
    deg_stmt = (
        select(Edge.target_id)
        .where(
            Edge.project_id == project_id,
            Edge.kind.in_(("READS", "WRITES", "REFERENCES")),
            Edge.valid_to.is_(None),
        )
        .group_by(Edge.target_id)
        .order_by(func.count().desc())
        .limit(limit * 3)
    )
    top_ids = [r[0] for r in (await session.execute(deg_stmt)).all()]
    fetched = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
                Node.id.in_(top_ids),
            )
        )
    ).scalars().all() if top_ids else []
    # ``IN`` does not preserve the degree ordering — re-sort by the rank
    # position in ``top_ids`` so the busiest table comes first.
    order = {nid: i for i, nid in enumerate(top_ids)}
    ranked = sorted(fetched, key=lambda n: order.get(n.id, len(order)))

    if len(ranked) >= limit:
        return ranked[:limit]

    filler = (
        await session.execute(
            select(Node)
            .where(
                Node.project_id == project_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
            .limit(limit)
        )
    ).scalars().all()
    seen = {n.id for n in ranked}
    out = [*ranked]
    for n in filler:
        if n.id in seen:
            continue
        out.append(n)
        if len(out) >= limit:
            break
    return out[:limit]


async def summarise_l1(
    session: AsyncSession,
    extractor: Extractor,
    *,
    project_id: uuid.UUID,
    limit: int = 25,
    progress_cb=None,
) -> int:
    symbols = await _priority_symbols(session, project_id, limit)
    # PR-152 — also summarise the most-referenced data entities (tables) so
    # "what does table X hold / who touches it?" gets an LLM narrative, not
    # just the raw column list. Bounded to a fraction of the symbol budget.
    entities = await _priority_data_entities(
        session, project_id, max(1, limit // 5)
    )
    nodes = [*symbols, *entities]

    count = 0
    for sym in nodes:
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

        prev = await _current_summary(session, project_id, sym.id, 1)
        if _unchanged(prev, evidence):
            if progress_cb is not None:
                await progress_cb()
            continue

        # PR-138 — budget guard. If the project crossed the configured
        # cap in the rolling window, skip the LLM call and let the
        # extractor fall through to the stub path; the operator sees
        # ``model_used="stub:budget_exceeded"`` and the runaway-spend
        # event is recorded on ``mnemos_llm_fallback_total``. When the
        # cap is unset (``MNEMOS_LLM_BUDGET_USD_PER_PROJECT=0``,
        # default) ``check_budget`` returns ``enabled=False`` so the
        # call is free.
        result = await _summarize_with_budget(
            session, extractor, project_id, 1, sym.id, evidence
        )
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
                claims=_stamp_hash(accepted, evidence),
                open_questions=result.open_questions,
                model_used=result.model_used,
                tokens_used=result.tokens_used,
                # PR-138b — persist the structured fallback reason so
                # the dashboard / API can render "why this is a stub"
                # without parsing the model_used string. ``None`` on
                # the happy path (real LLM call succeeded).
                fallback_reason=(
                    result.fallback_reason or None
                ),
                generated_at=datetime.now(tz=timezone.utc),
            )
        )
        count += 1
        if progress_cb is not None:
            await progress_cb()
    await session.commit()
    return count


async def summarise_l2(
    session: AsyncSession,
    extractor: Extractor,
    *,
    project_id: uuid.UUID,
    limit: int = 25,
    progress_cb=None,
) -> int:
    """File-level summary built purely from this file's L1 summaries.

    Groups L1 summaries by ``data.location.file`` of their target symbol so
    we never ask the LLM to read a file directly — only to condense
    already-condensed function-level summaries.
    """
    from app.models.findings import Summary  # local import avoids cycle

    l1_rows = (
        await session.execute(
            select(Summary, Node)
            .join(Node, Node.id == Summary.target_id)
            .where(
                Summary.project_id == project_id,
                Summary.level == 1,
                Summary.superseded_by.is_(None),
                Node.project_id == project_id,
                Node.valid_to.is_(None),
            )
        )
    ).all()

    by_file: dict[str, list[tuple[Summary, Node]]] = {}
    for summary, node in l1_rows:
        data = node.data or {}
        loc = (data.get("location") or {}).get("file")
        if not loc:
            continue
        by_file.setdefault(loc, []).append((summary, node))

    count = 0
    for file_path, group in list(by_file.items())[:limit]:
        raw = [
            {
                "kind": "node",
                "node_id": n.id,
                "data": {"name": (n.data or {}).get("name")},
                "l1_summary": s.summary,
                "certainty": n.certainty,
            }
            for s, n in group
        ]
        # Token-budget chunking: a 500-method file produces several partial L2s
        # that we then fold into one rollup; no chunk exceeds ~3K tokens.
        chunks = pack_by_budget(raw, max_tokens=3000)

        # Hash check before spending any tokens.
        flat_hash_input = raw
        prev = await _current_summary(session, project_id, file_path, 2)
        if _unchanged(prev, flat_hash_input):
            if progress_cb is not None:
                await progress_cb()
            continue

        partials: list[str] = []
        for i, chunk in enumerate(chunks):
            target_label = file_path if len(chunks) == 1 else f"{file_path}#chunk{i + 1}"
            # PR-138 — every L2 LLM call passes through the budget guard.
            partial = await _summarize_with_budget(
                session, extractor, project_id, 2, target_label, chunk
            )
            partials.append(partial.summary)

        if len(partials) == 1:
            result = await _summarize_with_budget(
                session, extractor, project_id, 2, file_path, raw
            )
        else:
            rollup_input = [{"kind": "node", "node_id": file_path, "partial_summary": p} for p in partials]
            result = await _summarize_with_budget(
                session, extractor, project_id, 2, file_path, rollup_input
            )

        accepted, _ = await validate_claims(
            session, project_id=project_id, claims=result.claims
        )
        await _supersede_current(session, project_id, file_path, 2)
        session.add(
            Summary(
                project_id=project_id,
                target_id=file_path,
                level=2,
                summary=result.summary,
                detailed=result.detailed,
                claims=_stamp_hash(accepted, flat_hash_input),
                open_questions=result.open_questions,
                model_used=result.model_used,
                tokens_used=result.tokens_used,
                # PR-138b — persist the structured fallback reason so
                # the dashboard / API can render "why this is a stub"
                # without parsing the model_used string. ``None`` on
                # the happy path (real LLM call succeeded).
                fallback_reason=(
                    result.fallback_reason or None
                ),
                generated_at=datetime.now(tz=timezone.utc),
            )
        )
        count += 1
        if progress_cb is not None:
            await progress_cb()
    await session.commit()
    return count


async def summarise_l3(
    session: AsyncSession,
    extractor: Extractor,
    *,
    project_id: uuid.UUID,
    limit: int = 25,
    progress_cb=None,
) -> int:
    """Module-level summary from L2 file summaries.

    Module boundary ≔ first path segment of the file (directory or package).
    Phase-2 replaces this with user-confirmed module definitions (spec §10.2).
    """
    from app.models.findings import Summary

    l2_rows = (
        await session.execute(
            select(Summary).where(
                Summary.project_id == project_id,
                Summary.level == 2,
                Summary.superseded_by.is_(None),
            )
        )
    ).scalars().all()

    by_module: dict[str, list[Summary]] = {}
    for s in l2_rows:
        parts = s.target_id.strip("/").split("/", 1)
        module = parts[0] if parts else "root"
        by_module.setdefault(module, []).append(s)

    count = 0
    for module, group in list(by_module.items())[:limit]:
        raw = [
            {
                "kind": "node",
                "node_id": s.target_id,
                "l2_summary": s.summary,
            }
            for s in group
        ]
        prev = await _current_summary(session, project_id, module, 3)
        if _unchanged(prev, raw):
            if progress_cb is not None:
                await progress_cb()
            continue

        chunks = pack_by_budget(raw, max_tokens=4000)
        if len(chunks) == 1:
            # PR-138 — budget guard wraps every L3 call.
            result = await _summarize_with_budget(
                session, extractor, project_id, 3, module, raw
            )
        else:
            partials: list[str] = []
            for i, chunk in enumerate(chunks):
                r = await _summarize_with_budget(
                    session, extractor, project_id, 3,
                    f"{module}#chunk{i + 1}", chunk,
                )
                partials.append(r.summary)
            rollup = [{"kind": "node", "node_id": module, "partial_summary": p} for p in partials]
            result = await _summarize_with_budget(
                session, extractor, project_id, 3, module, rollup
            )

        accepted, _ = await validate_claims(
            session, project_id=project_id, claims=result.claims
        )
        await _supersede_current(session, project_id, module, 3)
        session.add(
            Summary(
                project_id=project_id,
                target_id=module,
                level=3,
                summary=result.summary,
                detailed=result.detailed,
                claims=_stamp_hash(accepted, raw),
                open_questions=result.open_questions,
                model_used=result.model_used,
                tokens_used=result.tokens_used,
                # PR-138b — persist the structured fallback reason so
                # the dashboard / API can render "why this is a stub"
                # without parsing the model_used string. ``None`` on
                # the happy path (real LLM call succeeded).
                fallback_reason=(
                    result.fallback_reason or None
                ),
                generated_at=datetime.now(tz=timezone.utc),
            )
        )
        count += 1
        if progress_cb is not None:
            await progress_cb()
    await session.commit()
    return count
