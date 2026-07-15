"""Drive the extractor across L1-L3.

L1: per-Symbol node summary from the symbol + its 1-hop neighbours.
L2: per-source-file summary built from the file's L1 summaries.
L3: per-module/Component summary from its L2 summaries + cross-component edges.

L4/L5 are Phase-2 work (spec §15.4).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.agent import (
    FALLBACK_ANTHROPIC_IMPORT,
    FALLBACK_EVIDENCE_BUDGET,
    FALLBACK_NO_BACKEND,
    Extractor,
    ExtractorResult,
)
from app.extractor.cost import (
    BudgetExceeded,
    LLMRunBudget,
    RunBudgetExceeded,
    require_budget,
)
from app.extractor.packing import _approx_tokens, evidence_hash, pack_by_budget
from app.extractor.validator import validate_claims
from app.graph_publication import read_graph_stamp
from app.graph_overlays import (
    edge_identity,
    edge_read_view,
    load_edge_overlays,
    load_node_human_overlays,
    node_read_view,
)
from app.models.findings import LLMCall, Summary
from app.models.graph import Edge, Node
from app.summary_freshness import lock_ready_summary_generation

_LEGACY_HASH_CLAIM = "_evidence_hash"
FALLBACK_BUDGET_EXCEEDED = "budget_exceeded"
FALLBACK_RUN_DEADLINE = "run_deadline_exceeded"
FALLBACK_RUN_CALL_LIMIT = "run_call_limit_exceeded"
FALLBACK_RUN_INPUT_LIMIT = "run_input_token_limit_exceeded"
FALLBACK_UNGROUNDED_RESPONSE = "ungrounded_response"
_RUN_BUDGET_REASONS = frozenset({
    FALLBACK_BUDGET_EXCEEDED,
    FALLBACK_RUN_DEADLINE,
    FALLBACK_RUN_CALL_LIMIT,
    FALLBACK_RUN_INPUT_LIMIT,
})


def _combined_tokens(results: list[ExtractorResult]) -> int | None:
    """Aggregate all map + rollup calls without pretending unknown is zero."""

    if any(result.tokens_used is None for result in results):
        return None
    return sum(int(result.tokens_used or 0) for result in results)


async def _summarize_with_budget(
    session: AsyncSession,
    extractor: Extractor,
    project_id: uuid.UUID,
    level: int,
    target_id: str,
    evidence: list[dict[str, Any]],
    analysis_run_id: uuid.UUID | None = None,
    run_budget: LLMRunBudget | None = None,
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
        if run_budget is not None:
            run_budget.stop(FALLBACK_BUDGET_EXCEEDED)
        return Extractor._stub(
            level, target_id, evidence, FALLBACK_BUDGET_EXCEEDED,
        )

    remaining: float | None = None
    if run_budget is not None:
        try:
            # Evidence is already the complete provider scope.  Include a
            # small fixed allowance for the system/target/schema wrapper.
            remaining = run_budget.reserve(_approx_tokens(evidence) + 256)
        except RunBudgetExceeded as exc:
            return Extractor._stub(level, target_id, evidence, exc.reason)

    try:
        if remaining is None:
            result = await extractor.summarize(level, target_id, evidence)
        else:
            # ``reserve`` only returns a positive remainder.  Do not round a
            # sub-100ms remainder up: that would make the advertised wall
            # deadline soft at exactly the point it is meant to cancel work.
            async with asyncio.timeout(remaining):
                result = await extractor.summarize(level, target_id, evidence)
    except TimeoutError:
        if run_budget is not None:
            run_budget.stop(FALLBACK_RUN_DEADLINE)
        # A provider may have consumed tokens before cancellation.  Record a
        # physical attempt with unknown usage instead of silently treating it
        # as free; the run-level call/input reservation already remains spent.
        session.add(
            LLMCall(
                project_id=project_id,
                analysis_run_id=analysis_run_id,
                target_id=target_id,
                level=level,
                model_used=extractor.model,
                tokens_used=None,
                status="timeout",
                fallback_reason=FALLBACK_RUN_DEADLINE,
                generated_at=datetime.now(tz=timezone.utc),
            )
        )
        await session.commit()
        return Extractor._stub(level, target_id, evidence, FALLBACK_RUN_DEADLINE)

    # A provider response with no claim cannot be presented as a grounded
    # success.  Downgrade before ledgering so the physical attempt remains
    # visible as a fallback while preserving its actual token count.
    physical_model_used = result.model_used
    physical_tokens_used = result.tokens_used
    if not _is_fallback(result) and not result.claims:
        result = Extractor._stub(
            level, target_id, evidence, FALLBACK_UNGROUNDED_RESPONSE
        )
        result.tokens_used = physical_tokens_used

    no_physical_call = result.fallback_reason in {
        FALLBACK_NO_BACKEND,
        FALLBACK_ANTHROPIC_IMPORT,
        FALLBACK_BUDGET_EXCEEDED,
        FALLBACK_EVIDENCE_BUDGET,
    }
    if no_physical_call and run_budget is not None:
        # Repeating the same unavailable backend/budget failure over hundreds
        # of targets creates useless rows and can make an opt-in run look hung.
        run_budget.stop(result.fallback_reason or FALLBACK_NO_BACKEND)
    if not no_physical_call:
        call_id = uuid.uuid4()
        session.add(
            LLMCall(
                id=call_id,
                project_id=project_id,
                analysis_run_id=analysis_run_id,
                target_id=target_id,
                level=level,
                model_used=physical_model_used,
                tokens_used=physical_tokens_used,
                status="fallback" if result.fallback_reason else "completed",
                fallback_reason=result.fallback_reason or None,
                generated_at=datetime.now(tz=timezone.utc),
            )
        )
        # Keep the accounting identity beside this in-memory result until its
        # graph references are validated.  This is deliberately private
        # runner metadata, not part of the provider/schema contract.
        setattr(result, "_llm_call_id", call_id)
        # The call ledger is an accounting fact, not part of the generated
        # Summary transaction.  Commit it immediately so a later validator,
        # packing, or summary-write failure cannot erase real token spend.
        # This also makes the next map/reduce budget check observe the call.
        await session.commit()
    return result


async def _current_summary(
    session: AsyncSession,
    project_id: uuid.UUID,
    target_id: str,
    level: int,
) -> Summary | None:
    return (
        await session.execute(
            select(Summary)
            .where(
                Summary.project_id == project_id,
                Summary.target_id == target_id,
                Summary.level == level,
                Summary.superseded_by.is_(None),
            )
            # Migration 0030 deterministically reconciles legacy duplicates,
            # and the partial unique index fences validated rows. The bounded
            # ordering remains a corruption/rolling-upgrade safety net so a
            # pre-existing duplicate cannot permanently brick regeneration.
            .order_by(Summary.generated_at.desc(), Summary.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _revalidate_cached_summary(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    expected_generation: int,
    expected_overlay_generation: int,
    summary: Summary,
) -> None:
    """Authorize an exact-evidence cache hit for the current generation.

    The shared GraphHead lock linearizes this marker update with source
    promotion. Content and ``analysis_run_id`` are deliberately untouched.
    """

    await lock_ready_summary_generation(
        session,
        project_id=project_id,
        expected_generation=expected_generation,
        expected_overlay_generation=expected_overlay_generation,
    )
    summary.validated_graph_generation = expected_generation
    summary.validated_overlay_generation = expected_overlay_generation
    summary.validated_at = datetime.now(tz=timezone.utc)
    await session.commit()


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
    # A no-backend/timeout/schema stub is a diagnostic, never a durable cache
    # hit.  When a provider is repaired the same evidence must be attempted
    # again instead of preserving a placeholder forever.
    if not _is_successful_summary(prev):
        return False
    prev_hash = prev.evidence_hash
    # Rolling upgrades may encounter summaries written by the old runner.
    # Read its synthetic control claim only as a cache migration fallback;
    # new rows never persist it and MCP filters it from responses.
    if prev_hash is None:
        for claim in prev.claims or []:
            if not isinstance(claim, dict):
                continue
            if claim.get("claim") != _LEGACY_HASH_CLAIM:
                continue
            legacy_evidence = claim.get("evidence")
            if not isinstance(legacy_evidence, list) or not legacy_evidence:
                continue
            first = legacy_evidence[0]
            if isinstance(first, dict):
                candidate = first.get("node_id")
                if isinstance(candidate, str):
                    prev_hash = candidate
            break
    if prev_hash is None:
        return False
    return prev_hash == evidence_hash(evidence)


def _is_fallback(result: ExtractorResult) -> bool:
    return bool(result.fallback_reason) or result.model_used.startswith("stub")


def _is_successful_summary(summary: Summary | None) -> bool:
    grounded_claim = bool(
        summary is not None
        and any(
            isinstance(claim, dict)
            and claim.get("claim") != _LEGACY_HASH_CLAIM
            and isinstance(claim.get("claim"), str)
            and bool(claim["claim"].strip())
            and isinstance(claim.get("evidence"), list)
            and bool(claim["evidence"])
            for claim in (summary.claims or [])
        )
    )
    return bool(
        summary is not None
        and not summary.fallback_reason
        and not summary.model_used.startswith("stub")
        and grounded_claim
    )


async def _ground_result_or_fallback(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    level: int,
    target_id: str,
    evidence: list[dict[str, Any]],
    result: ExtractorResult,
) -> ExtractorResult:
    """Validate exact provider scope or return an explicit ungrounded stub."""

    llm_call_id = getattr(result, "_llm_call_id", None)
    accepted, _rejected = await validate_claims(
        session,
        project_id=project_id,
        claims=result.claims,
        provided_evidence=evidence,
    )
    if not _is_fallback(result) and not accepted:
        tokens_used = result.tokens_used
        result = Extractor._stub(
            level, target_id, evidence, FALLBACK_UNGROUNDED_RESPONSE
        )
        result.tokens_used = tokens_used
        if llm_call_id is not None:
            # The physical call was already committed for durable accounting.
            # Reconcile its semantic outcome once DB-owned grounding rejects
            # every claim, using the exact call UUID to avoid cross-run races.
            await session.execute(
                LLMCall.__table__.update()
                .where(LLMCall.id == llm_call_id)
                .values(
                    status="fallback",
                    fallback_reason=FALLBACK_UNGROUNDED_RESPONSE,
                )
            )
            await session.commit()
        accepted, _rejected = await validate_claims(
            session,
            project_id=project_id,
            claims=result.claims,
            provided_evidence=evidence,
        )
    # Downstream persistence receives only DB-owned certainty and references
    # that were in the exact provider prompt.  Fallback rows may legitimately
    # have no claim (for example, L2/L3 presentation targets are not node IDs).
    result.claims = accepted
    return result


def _evidence_reference(row: Any) -> dict[str, Any] | None:
    """Copy one explicit graph reference from a model evidence row.

    Rollups must not turn presentation targets (a file path or module name)
    into graph identities.  Keep only the top-level node/edge discriminator,
    its ID, and graph certainty; arbitrary nested IDs are deliberately not
    interpreted.
    """

    if not isinstance(row, dict):
        return None
    certainty = row.get("certainty")
    if certainty not in {"verified", "asserted", "inferred"}:
        return None
    if row.get("kind") == "node":
        node_id = row.get("node_id")
        if isinstance(node_id, str) and node_id.strip() == node_id and node_id:
            return {
                "kind": "node",
                "node_id": node_id,
                "certainty": certainty,
            }
    elif row.get("kind") == "edge":
        edge_id = row.get("edge_id")
        try:
            normalized = str(uuid.UUID(str(edge_id)))
        except (ValueError, TypeError, AttributeError):
            return None
        return {
            "kind": "edge",
            "edge_id": normalized,
            "certainty": certainty,
        }
    return None


def _bounded_rollup_evidence(
    partial_results: list[ExtractorResult],
    source_chunks: list[list[dict[str, Any]]],
    *,
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Build one bounded reduce pack anchored only to supplied graph IDs.

    Every partial summary is attached to the first real graph reference from
    the chunk that produced it.  Remaining references are exposed as flat
    rows, so the model can cite them and ``validate_claims`` sees the exact
    same top-level scope.  Anchors are packed first to preserve broad partial
    coverage under the hard budget; very large reductions remain explicitly
    bounded instead of being blindly sliced by the provider prompt.
    """

    anchors: list[tuple[int, dict[str, Any]]] = []
    total_partials = len(partial_results)
    for partial_index, (partial, source_chunk) in enumerate(
        zip(partial_results, source_chunks, strict=True),
        start=1,
    ):
        references = [
            reference
            for row in source_chunk
            if (reference := _evidence_reference(row)) is not None
        ]
        if not references:
            continue
        anchors.append(
            (
                partial_index,
                {
                    **references[0],
                    "partial_index": partial_index,
                    "partial_count": total_partials,
                    "source_evidence_count": len(references),
                    "partial_summary": partial.summary,
                },
            )
        )

    selected: list[dict[str, Any]] = []

    def append_within_budget(candidate: dict[str, Any]) -> bool:
        # Bound one pathological row first, then test it against the complete
        # final pack.  Do not retain the packer's overflow chunks: a reduce
        # call has exactly one validation scope and one provider input.
        bounded = pack_by_budget([candidate], max_tokens=max_tokens)[0][0]
        if _evidence_reference(bounded) is None:
            return False
        if _approx_tokens([*selected, bounded]) > max_tokens:
            return False
        selected.append(bounded)
        return True

    included_partials: list[int] = []
    for partial_index, anchor in anchors:
        if not append_within_budget(anchor):
            break
        included_partials.append(partial_index)

    # Only after broad partial coverage do we add the remaining explicit
    # references.  Iteration stops as soon as the final pack is full, avoiding
    # an unbounded duplicate list for a file with hundreds of thousands of
    # symbols.
    for partial_index in included_partials:
        for row in source_chunks[partial_index - 1][1:]:
            reference = _evidence_reference(row)
            if reference is None:
                continue
            if not append_within_budget(
                {
                    **reference,
                    "partial_index": partial_index,
                    "partial_count": total_partials,
                }
            ):
                return selected
    return selected


async def _priority_symbols(
    session: AsyncSession, project_id: uuid.UUID, limit: int
) -> list[Node]:
    """Rank candidates for L1: entry points first, then by caller degree.

    A large codebase has 100k+ symbols; the operator-visible first pass must
    cover the useful surface (HTTP contracts, controllers, background jobs)
    before grinding through private helpers.
    """
    if limit <= 0:
        return []

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
    analysis_run_id: uuid.UUID | None = None,
    run_budget: LLMRunBudget | None = None,
    retained_summary_ids: set[uuid.UUID] | None = None,
) -> int:
    # An explicit zero is the hard opt-out contract.  In particular, do not
    # let the historical ``max(1, limit // 5)`` entity top-up create one call.
    if limit <= 0:
        return 0
    graph_stamp = await read_graph_stamp(session, project_id=project_id)
    graph_generation = graph_stamp.generation
    overlay_generation = graph_stamp.overlay_generation
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
        if run_budget is not None and run_budget.exhausted:
            break
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
        node_overlays = await load_node_human_overlays(
            session, project_id=project_id, node_ids=[sym.id]
        )
        neighbour_edges = [*neighbours_in, *neighbours_out]
        edge_overlays = await load_edge_overlays(
            session,
            project_id=project_id,
            identities=(edge_identity(edge) for edge in neighbour_edges),
        )
        node_view = node_read_view(sym, node_overlays.get(sym.id))
        evidence: list[dict[str, Any]] = [
            {
                "kind": "node",
                "node_id": sym.id,
                **node_view,
            }
        ]
        for e in neighbour_edges:
            identity = edge_identity(e)
            edge_view = edge_read_view(
                e,
                edge_overlays.human.get(identity),
                edge_overlays.runtime.get(identity),
            )
            evidence.append(
                {
                    "kind": "edge",
                    "edge_id": str(e.id),
                    "edge_kind": e.kind,
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                    **edge_view,
                }
            )

        # L1 node payloads can contain large signatures/schema metadata.  The
        # exact first hard-bounded pack is the provider input, cache identity,
        # and validator scope; no prompt adapter is allowed to slice it later.
        evidence_chunks = pack_by_budget(evidence, max_tokens=3000)
        bounded_evidence = evidence_chunks[0] if evidence_chunks else []
        if len(evidence_chunks) > 1:
            # Reserve room for an explicit coverage marker.  A bounded prompt
            # must not silently look complete merely because the first node
            # payload consumed the whole pack and pushed 1-hop edges out.
            evidence_chunks = pack_by_budget(evidence, max_tokens=2800)
            first_chunk = evidence_chunks[0] if evidence_chunks else []
            bounded_evidence = [
                {
                    "scope": "prompt_evidence",
                    "truncated": True,
                    "included_rows": len(first_chunk),
                    "total_rows": len(evidence),
                    "included_chunks": 1,
                    "total_chunks": len(evidence_chunks),
                },
                *first_chunk,
            ]
            if _approx_tokens(bounded_evidence) > 3000:
                raise AssertionError("L1 evidence scope marker exceeded budget")

        prev = await _current_summary(session, project_id, sym.id, 1)
        if _unchanged(prev, bounded_evidence):
            if prev is None:
                raise AssertionError("unchanged summary cache hit has no row")
            await _revalidate_cached_summary(
                session,
                project_id=project_id,
                expected_generation=graph_generation,
                expected_overlay_generation=overlay_generation,
                summary=prev,
            )
            if retained_summary_ids is not None:
                # The existing row remains the canonical narrative because its
                # exact current-graph evidence hash was revalidated.  Record
                # that fact in bounded run-local state; do not rewrite the
                # row's original analysis_run_id provenance.
                retained_summary_ids.add(prev.id)
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
            session,
            extractor,
            project_id,
            1,
            sym.id,
            bounded_evidence,
            analysis_run_id,
            run_budget,
        )
        result = await _ground_result_or_fallback(
            session,
            project_id=project_id,
            level=1,
            target_id=sym.id,
            evidence=bounded_evidence,
            result=result,
        )
        accepted = result.claims

        await lock_ready_summary_generation(
            session,
            project_id=project_id,
            expected_generation=graph_generation,
            expected_overlay_generation=overlay_generation,
        )
        await _supersede_current(session, project_id, sym.id, 1)
        generated_at = datetime.now(tz=timezone.utc)
        session.add(
            Summary(
                project_id=project_id,
                target_id=sym.id,
                level=1,
                summary=result.summary,
                detailed=result.detailed,
                claims=accepted,
                open_questions=result.open_questions,
                evidence_hash=evidence_hash(bounded_evidence),
                model_used=result.model_used,
                tokens_used=result.tokens_used,
                analysis_run_id=analysis_run_id,
                validated_graph_generation=graph_generation,
                validated_overlay_generation=overlay_generation,
                validated_at=generated_at,
                # PR-138b — persist the structured fallback reason so
                # the dashboard / API can render "why this is a stub"
                # without parsing the model_used string. ``None`` on
                # the happy path (real LLM call succeeded).
                fallback_reason=(
                    result.fallback_reason or None
                ),
                generated_at=generated_at,
            )
        )
        count += 1
        # Commit per item so the SQLite write lock (local mode runs the
        # API and this inline job in one process against one file) is
        # released between LLM calls — otherwise a single end-of-stage
        # commit holds the lock for the whole batch and the separate
        # session behind progress_cb / a concurrent request hits
        # "database is locked" (PR-141 pattern). Postgres is unaffected.
        await session.commit()
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
    analysis_run_id: uuid.UUID | None = None,
    run_budget: LLMRunBudget | None = None,
    retained_summary_ids: set[uuid.UUID] | None = None,
) -> int:
    """File-level summary built purely from this file's L1 summaries.

    Groups L1 summaries by ``data.location.file`` of their target symbol so
    we never ask the LLM to read a file directly — only to condense
    already-condensed function-level summaries.
    """
    if limit <= 0:
        return 0

    from app.models.findings import Summary  # local import avoids cycle

    graph_stamp = await read_graph_stamp(session, project_id=project_id)
    graph_generation = graph_stamp.generation
    overlay_generation = graph_stamp.overlay_generation

    l1_rows = (
        await session.execute(
            select(Summary, Node)
            .join(Node, Node.id == Summary.target_id)
            .where(
                Summary.project_id == project_id,
                Summary.level == 1,
                Summary.superseded_by.is_(None),
                Summary.validated_graph_generation == graph_generation,
                Summary.validated_overlay_generation == overlay_generation,
                Node.project_id == project_id,
                Node.valid_to.is_(None),
            )
        )
    ).all()
    node_overlays = await load_node_human_overlays(
        session,
        project_id=project_id,
        node_ids=(node.id for _summary, node in l1_rows),
    )

    by_file: dict[str, list[tuple[Summary, Node]]] = {}
    for summary, node in l1_rows:
        data = node_read_view(node, node_overlays.get(node.id))["data"]
        loc = (data.get("location") or {}).get("file")
        if not loc:
            continue
        by_file.setdefault(loc, []).append((summary, node))

    count = 0
    for file_path, group in list(by_file.items())[:limit]:
        if run_budget is not None and run_budget.exhausted:
            break
        raw = []
        for summary, node in group:
            view = node_read_view(node, node_overlays.get(node.id))
            raw.append(
                {
                    "kind": "node",
                    "node_id": node.id,
                    "data": {"name": (view["data"] or {}).get("name")},
                    "l1_summary": summary.summary,
                    "certainty": view["certainty"],
                    "source_certainty": view["source_certainty"],
                    "effective_certainty": view["effective_certainty"],
                    "confirmed": view["confirmed"],
                }
            )
        # Token-budget chunking: a 500-method file produces several partial L2s
        # that we then fold into one rollup; no chunk exceeds ~3K tokens.
        chunks = pack_by_budget(raw, max_tokens=3000)

        # Hash check before spending any tokens.
        flat_hash_input = raw
        prev = await _current_summary(session, project_id, file_path, 2)
        if _unchanged(prev, flat_hash_input):
            if prev is None:
                raise AssertionError("unchanged summary cache hit has no row")
            await _revalidate_cached_summary(
                session,
                project_id=project_id,
                expected_generation=graph_generation,
                expected_overlay_generation=overlay_generation,
                summary=prev,
            )
            if retained_summary_ids is not None:
                retained_summary_ids.add(prev.id)
            if progress_cb is not None:
                await progress_cb()
            continue

        partial_results: list[ExtractorResult] = []
        for i, chunk in enumerate(chunks):
            target_label = file_path if len(chunks) == 1 else f"{file_path}#chunk{i + 1}"
            # PR-138 — every L2 LLM call passes through the budget guard.
            partial = await _summarize_with_budget(
                session,
                extractor,
                project_id,
                2,
                target_label,
                chunk,
                analysis_run_id,
                run_budget,
            )
            partial = await _ground_result_or_fallback(
                session,
                project_id=project_id,
                level=2,
                target_id=target_label,
                evidence=chunk,
                result=partial,
            )
            partial_results.append(partial)
            if _is_fallback(partial):
                break

        if len(chunks) == 1:
            # The single chunk is the complete file input.  Reusing that
            # structured result avoids the old duplicate call with identical
            # evidence (roughly half of normal L2 token/process overhead).
            result = partial_results[0]
            validation_evidence = chunks[0]
        else:
            failed_partial = next(
                (partial for partial in partial_results if _is_fallback(partial)),
                None,
            )
            if failed_partial is not None:
                # Do not reduce incomplete/failed maps into authoritative
                # prose. No extra provider call is made for the doomed rollup.
                reason = failed_partial.fallback_reason or FALLBACK_NO_BACKEND
                result = Extractor._stub(
                    2, file_path, flat_hash_input, reason
                )
                result.tokens_used = _combined_tokens(partial_results)
                validation_evidence: list[dict[str, Any]] = []
            else:
                rollup_input = _bounded_rollup_evidence(
                    partial_results,
                    chunks,
                    max_tokens=3000,
                )
                result = await _summarize_with_budget(
                    session,
                    extractor,
                    project_id,
                    2,
                    file_path,
                    rollup_input,
                    analysis_run_id,
                    run_budget,
                )
                result.tokens_used = _combined_tokens(
                    [*partial_results, result]
                )
                validation_evidence = rollup_input

        result = await _ground_result_or_fallback(
            session,
            project_id=project_id,
            level=2,
            target_id=file_path,
            evidence=validation_evidence,
            result=result,
        )
        accepted = result.claims
        await lock_ready_summary_generation(
            session,
            project_id=project_id,
            expected_generation=graph_generation,
            expected_overlay_generation=overlay_generation,
        )
        await _supersede_current(session, project_id, file_path, 2)
        generated_at = datetime.now(tz=timezone.utc)
        session.add(
            Summary(
                project_id=project_id,
                target_id=file_path,
                level=2,
                summary=result.summary,
                detailed=result.detailed,
                claims=accepted,
                open_questions=result.open_questions,
                evidence_hash=evidence_hash(flat_hash_input),
                model_used=result.model_used,
                tokens_used=result.tokens_used,
                analysis_run_id=analysis_run_id,
                validated_graph_generation=graph_generation,
                validated_overlay_generation=overlay_generation,
                validated_at=generated_at,
                # PR-138b — persist the structured fallback reason so
                # the dashboard / API can render "why this is a stub"
                # without parsing the model_used string. ``None`` on
                # the happy path (real LLM call succeeded).
                fallback_reason=(
                    result.fallback_reason or None
                ),
                generated_at=generated_at,
            )
        )
        count += 1
        # Commit per item so the SQLite write lock (local mode runs the
        # API and this inline job in one process against one file) is
        # released between LLM calls — otherwise a single end-of-stage
        # commit holds the lock for the whole batch and the separate
        # session behind progress_cb / a concurrent request hits
        # "database is locked" (PR-141 pattern). Postgres is unaffected.
        await session.commit()
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
    analysis_run_id: uuid.UUID | None = None,
    run_budget: LLMRunBudget | None = None,
    retained_summary_ids: set[uuid.UUID] | None = None,
) -> int:
    """Module-level summary from L2 file summaries.

    Module boundary ≔ first path segment of the file (directory or package).
    Phase-2 replaces this with user-confirmed module definitions (spec §10.2).
    """
    if limit <= 0:
        return 0

    from app.models.findings import Summary

    graph_stamp = await read_graph_stamp(session, project_id=project_id)
    graph_generation = graph_stamp.generation
    overlay_generation = graph_stamp.overlay_generation

    l2_rows = (
        await session.execute(
            select(Summary).where(
                Summary.project_id == project_id,
                Summary.level == 2,
                Summary.superseded_by.is_(None),
                Summary.validated_graph_generation == graph_generation,
                Summary.validated_overlay_generation == overlay_generation,
            )
        )
    ).scalars().all()

    # L2 targets are presentation keys (source paths), not graph node IDs.
    # Rebuild their grounding from the same current L1-summary/current-Node
    # relation used by L2.  This keeps both single- and multi-chunk L3 input
    # citable without teaching the validator to trust path-shaped pseudo IDs.
    l1_node_rows = (
        await session.execute(
            select(Summary, Node)
            .join(Node, Node.id == Summary.target_id)
            .where(
                Summary.project_id == project_id,
                Summary.level == 1,
                Summary.superseded_by.is_(None),
                Summary.validated_graph_generation == graph_generation,
                Summary.validated_overlay_generation == overlay_generation,
                Node.project_id == project_id,
                Node.valid_to.is_(None),
            )
        )
    ).all()
    node_overlays = await load_node_human_overlays(
        session,
        project_id=project_id,
        node_ids=(node.id for _summary, node in l1_node_rows),
    )
    current_nodes_by_file: dict[str, list[Node]] = {}
    for _summary, node in l1_node_rows:
        node_data = node_read_view(node, node_overlays.get(node.id))["data"]
        location = ((node_data or {}).get("location") or {}).get("file")
        if isinstance(location, str) and location:
            current_nodes_by_file.setdefault(location, []).append(node)
    for nodes in current_nodes_by_file.values():
        nodes.sort(key=lambda node: node.id)

    by_module: dict[str, list[Summary]] = {}
    for s in l2_rows:
        parts = s.target_id.strip("/").split("/", 1)
        module = parts[0] if parts else "root"
        by_module.setdefault(module, []).append(s)

    count = 0
    for module, group in list(by_module.items())[:limit]:
        if run_budget is not None and run_budget.exhausted:
            break
        raw: list[dict[str, Any]] = []
        for l2_summary in sorted(group, key=lambda summary: summary.target_id):
            nodes = current_nodes_by_file.get(l2_summary.target_id, [])
            for node_index, node in enumerate(nodes):
                view = node_read_view(node, node_overlays.get(node.id))
                evidence_row: dict[str, Any] = {
                    "kind": "node",
                    "node_id": node.id,
                    "certainty": view["certainty"],
                    "source_certainty": view["source_certainty"],
                    "effective_certainty": view["effective_certainty"],
                    "confirmed": view["confirmed"],
                    "source_file": l2_summary.target_id,
                }
                if node_index == 0:
                    evidence_row["l2_summary"] = l2_summary.summary
                raw.append(evidence_row)
        if not raw:
            if progress_cb is not None:
                await progress_cb()
            continue
        prev = await _current_summary(session, project_id, module, 3)
        if _unchanged(prev, raw):
            if prev is None:
                raise AssertionError("unchanged summary cache hit has no row")
            await _revalidate_cached_summary(
                session,
                project_id=project_id,
                expected_generation=graph_generation,
                expected_overlay_generation=overlay_generation,
                summary=prev,
            )
            if retained_summary_ids is not None:
                retained_summary_ids.add(prev.id)
            if progress_cb is not None:
                await progress_cb()
            continue

        chunks = pack_by_budget(raw, max_tokens=4000)
        if len(chunks) == 1:
            # PR-138 — budget guard wraps every L3 call.
            validation_evidence = chunks[0]
            result = await _summarize_with_budget(
                session,
                extractor,
                project_id,
                3,
                module,
                validation_evidence,
                analysis_run_id,
                run_budget,
            )
        else:
            partial_results: list[ExtractorResult] = []
            for i, chunk in enumerate(chunks):
                r = await _summarize_with_budget(
                    session, extractor, project_id, 3,
                    f"{module}#chunk{i + 1}", chunk,
                    analysis_run_id,
                    run_budget,
                )
                r = await _ground_result_or_fallback(
                    session,
                    project_id=project_id,
                    level=3,
                    target_id=f"{module}#chunk{i + 1}",
                    evidence=chunk,
                    result=r,
                )
                partial_results.append(r)
                if _is_fallback(r):
                    break
            failed_partial = next(
                (partial for partial in partial_results if _is_fallback(partial)),
                None,
            )
            if failed_partial is not None:
                reason = failed_partial.fallback_reason or FALLBACK_NO_BACKEND
                result = Extractor._stub(3, module, raw, reason)
                result.tokens_used = _combined_tokens(partial_results)
                validation_evidence = []
            else:
                rollup = _bounded_rollup_evidence(
                    partial_results,
                    chunks,
                    max_tokens=4000,
                )
                result = await _summarize_with_budget(
                    session,
                    extractor,
                    project_id,
                    3,
                    module,
                    rollup,
                    analysis_run_id,
                    run_budget,
                )
                result.tokens_used = _combined_tokens(
                    [*partial_results, result]
                )
                validation_evidence = rollup

        result = await _ground_result_or_fallback(
            session,
            project_id=project_id,
            level=3,
            target_id=module,
            evidence=validation_evidence,
            result=result,
        )
        accepted = result.claims
        await lock_ready_summary_generation(
            session,
            project_id=project_id,
            expected_generation=graph_generation,
            expected_overlay_generation=overlay_generation,
        )
        await _supersede_current(session, project_id, module, 3)
        generated_at = datetime.now(tz=timezone.utc)
        session.add(
            Summary(
                project_id=project_id,
                target_id=module,
                level=3,
                summary=result.summary,
                detailed=result.detailed,
                claims=accepted,
                open_questions=result.open_questions,
                evidence_hash=evidence_hash(raw),
                model_used=result.model_used,
                tokens_used=result.tokens_used,
                analysis_run_id=analysis_run_id,
                validated_graph_generation=graph_generation,
                validated_overlay_generation=overlay_generation,
                validated_at=generated_at,
                # PR-138b — persist the structured fallback reason so
                # the dashboard / API can render "why this is a stub"
                # without parsing the model_used string. ``None`` on
                # the happy path (real LLM call succeeded).
                fallback_reason=(
                    result.fallback_reason or None
                ),
                generated_at=generated_at,
            )
        )
        count += 1
        # Commit per item so the SQLite write lock (local mode runs the
        # API and this inline job in one process against one file) is
        # released between LLM calls — otherwise a single end-of-stage
        # commit holds the lock for the whole batch and the separate
        # session behind progress_cb / a concurrent request hits
        # "database is locked" (PR-141 pattern). Postgres is unaffected.
        await session.commit()
        if progress_cb is not None:
            await progress_cb()
    await session.commit()
    return count
