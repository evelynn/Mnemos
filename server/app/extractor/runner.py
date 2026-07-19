"""Drive the extractor across L1-L3.

L1: per-Symbol node summary from the symbol + its 1-hop neighbours.
L2: per-source-file summary built from the file's L1 summaries.
L3: per-module/Component summary from its L2 summaries + cross-component edges.

L4/L5 are Phase-2 work (spec §15.4).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.extractor.agent import (
    FALLBACK_ANTHROPIC_CLIENT_INIT,
    FALLBACK_ANTHROPIC_IMPORT,
    FALLBACK_AGENT_SDK_INIT,
    FALLBACK_EVIDENCE_BUDGET,
    FALLBACK_NO_BACKEND,
    Extractor,
    ExtractorResult,
)
from app.extractor.agent_sdk import (
    SUMMARY_CANDIDATE_CONTRACT,
    SummaryCandidateContext,
    normalize_summary_candidate_payload,
    summary_operation_id,
    use_summary_candidate_context,
)
from app.extractor.cost import (
    LLMRunBudget,
    RunBudgetExceeded,
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
from app.llm.contracts import (
    AttemptStatus,
    LLMPurpose,
    ResultStatus,
    UsageSource,
    unavailable_usage,
)
from app.llm.lifecycle import (
    AttemptCallbacks,
    AttemptOutcome,
    AttemptReplayResult,
    AttemptReplayState,
    AttemptStartMetadata,
    AttemptTicket,
    BudgetScopeKind,
    LLMAttemptReplayBlocked,
    LLMLifecycleError,
    SemanticCandidate,
    SemanticCandidateReplay,
    SemanticCandidateReplayRequest,
    begin_attempt,
    classify_attempt_result,
    finalize_attempt,
    fingerprint_input,
    load_attempt_replay,
    load_terminal_semantic_candidate,
    lock_semantic_candidate_for_product,
    use_attempt_callbacks,
)
from app.summary_freshness import lock_ready_summary_generation

_LEGACY_HASH_CLAIM = "_evidence_hash"
FALLBACK_BUDGET_EXCEEDED = "budget_exceeded"
FALLBACK_RUN_DEADLINE = "run_deadline_exceeded"
FALLBACK_RUN_CALL_LIMIT = "run_call_limit_exceeded"
FALLBACK_RUN_INPUT_LIMIT = "run_input_token_limit_exceeded"
FALLBACK_UNGROUNDED_RESPONSE = "ungrounded_response"
FALLBACK_ATTEMPT_REPLAY_BLOCKED = "attempt_replay_blocked"
FALLBACK_ACCOUNTING_UNAVAILABLE = "durable_accounting_unavailable"
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


def _summary_result_from_replay(
    replay: AttemptReplayResult,
    *,
    level: int,
    target_id: str,
    evidence: list[dict[str, Any]],
) -> ExtractorResult:
    """Project one terminal attempt into the normal grounding pipeline."""

    if replay.attempt_id is None or replay.attempt_status is None:
        raise LLMLifecycleError("summary replay attempt projection is incomplete")
    tokens_used = replay.usage.total_tokens if replay.usage is not None else None
    if replay.state == AttemptReplayState.TERMINAL_WITH_CANDIDATE:
        if replay.candidate_status == "rejected":
            result = Extractor._stub(
                level,
                target_id,
                evidence,
                replay.failure_code or FALLBACK_ATTEMPT_REPLAY_BLOCKED,
            )
            result.tokens_used = tokens_used
            result._llm_call_id = replay.attempt_id
            return result
        if (
            replay.candidate_status not in {"candidate", "accepted"}
            or replay.result_status
            not in {ResultStatus.PENDING, ResultStatus.ACCEPTED}
        ):
            raise LLMLifecycleError(
                "summary replay candidate classification is inconsistent"
            )
        if replay.payload is None:
            raise LLMLifecycleError("summary replay candidate payload is unavailable")
        canonical = normalize_summary_candidate_payload(replay.payload)
        model = replay.resolved_model or replay.requested_model or "unknown"
        if replay.usage is not None and replay.usage.source == UsageSource.AGENT_SDK:
            model = f"{model}:agent_sdk"
        return ExtractorResult(
            summary=canonical["summary"],
            detailed=canonical["detailed"],
            claims=canonical["claims"],
            open_questions=canonical["open_questions"],
            model_used=model,
            tokens_used=tokens_used,
            _llm_call_id=replay.attempt_id,
        )
    if replay.state != AttemptReplayState.TERMINAL_WITHOUT_CANDIDATE:
        raise LLMLifecycleError("summary replay attempt is not terminal")
    result = Extractor._stub(
        level,
        target_id,
        evidence,
        replay.failure_code or FALLBACK_ATTEMPT_REPLAY_BLOCKED,
    )
    result.tokens_used = tokens_used
    result._llm_call_id = replay.attempt_id
    return result


async def _summarize_with_budget(
    session: AsyncSession,
    extractor: Extractor,
    project_id: uuid.UUID,
    level: int,
    target_id: str,
    evidence: list[dict[str, Any]],
    analysis_run_id: uuid.UUID | None = None,
    run_budget: LLMRunBudget | None = None,
    *,
    graph_generation: int = 0,
    overlay_generation: int = 0,
) -> ExtractorResult:
    """Run the extractor with the budget guard pre-check.

    A new paid provider call requires a positive rolling-window dollar
    authorization and enough remaining no-refund headroom. Missing/exhausted
    authority synthesises a clearly-labelled stub result. Operators see this
    on ``summaries.model_used`` and the Prometheus counter (PR-138).
    """
    if run_budget is None:
        # No durable budget scope means no callback can commit a STARTED row.
        # Trusted adapters already reject this state; returning here also
        # prevents custom/subclass adapters from dispatching outside the
        # lifecycle boundary.
        return Extractor._stub(
            level,
            target_id,
            evidence,
            FALLBACK_ACCOUNTING_UNAVAILABLE,
        )

    # Provider accounting/replay owns short-lived sessions below. Close the
    # graph-reading transaction first so SQLite does not retain a reader lock;
    # application sessions use expire_on_commit=False, matching the previous
    # begin_attempt-on-this-session behaviour without allowing a replay
    # rollback to expire preloaded L1/L2/L3 ORM worklists.
    in_transaction = getattr(session, "in_transaction", None)
    if callable(in_transaction) and in_transaction():
        await session.commit()

    candidate_context = SummaryCandidateContext(
        project_id=project_id,
        binding_fingerprint=fingerprint_input(
            "mnemos.summary.binding.v1",
            str(project_id),
            str(level),
            target_id,
            evidence_hash(evidence),
            str(graph_generation),
            str(overlay_generation),
        ),
    )

    def _new_accounting_session() -> AsyncSession:
        bind = session.bind
        if bind is None:
            raise LLMLifecycleError(
                "summary accounting session has no async bind"
            )
        return AsyncSession(bind=bind, expire_on_commit=False)

    # The semantic operation is provider-neutral.  Probe it before provider
    # selection, credential discovery, dollar policy reads, or local budget
    # mutation so a restart with different routing still reuses one paid
    # result.  Omitting the provider input fingerprint is intentional; the
    # exact project/target/source-generation binding remains mandatory.
    stable_operation_id = summary_operation_id(
        candidate_context,
        level=level,
        target_id=target_id,
    )
    try:
        async with _new_accounting_session() as accounting:
            replay = await load_attempt_replay(
                accounting,
                operation_id=stable_operation_id,
                attempt_no=1,
                project_id=project_id,
                purpose=LLMPurpose.SUMMARY,
                input_fingerprint=None,
                contract_name=SUMMARY_CANDIDATE_CONTRACT,
                binding_fingerprint=candidate_context.binding_fingerprint,
                normalizer=normalize_summary_candidate_payload,
            )
    except LLMLifecycleError:
        run_budget.stop(FALLBACK_ACCOUNTING_UNAVAILABLE)
        return Extractor._stub(
            level, target_id, evidence, FALLBACK_ACCOUNTING_UNAVAILABLE
        )
    if replay.state in {
        AttemptReplayState.TERMINAL_WITHOUT_CANDIDATE,
        AttemptReplayState.TERMINAL_WITH_CANDIDATE,
    }:
        return _summary_result_from_replay(
            replay,
            level=level,
            target_id=target_id,
            evidence=evidence,
        )
    if replay.state == AttemptReplayState.STARTED:
        return Extractor._stub(
            level, target_id, evidence, FALLBACK_ATTEMPT_REPLAY_BLOCKED
        )

    # Only an absent operation may consult the current request budget.  The
    # selected adapter supplies its honest input/output ceiling to
    # ``begin_attempt``, which performs the sole new-dispatch reservation.
    remaining = run_budget.remaining_seconds()
    if remaining <= 0:
        run_budget.stop(FALLBACK_RUN_DEADLINE)
        return Extractor._stub(level, target_id, evidence, FALLBACK_RUN_DEADLINE)

    started_ticket: AttemptTicket | None = None
    started_usage_source: UsageSource | None = None
    timeout_guard: asyncio.Timeout | None = None
    provider_dispatch_started = False

    async def _start_attempt(
        metadata: AttemptStartMetadata,
    ) -> AttemptTicket:
        nonlocal started_ticket, started_usage_source
        if run_budget is None:
            raise RuntimeError("durable LLM attempt requires a run budget")
        # Bind the semantic provider request to the project and exact input,
        # not to the ephemeral request budget.  A resumed worker may create a
        # fresh scope, but that must never authorize a second paid dispatch
        # for the same target/provider/model/prompt.
        async with _new_accounting_session() as accounting:
            started_ticket = await begin_attempt(
                accounting,
                project_id=project_id,
                analysis_run_id=analysis_run_id,
                purpose=LLMPurpose.SUMMARY,
                target_id=target_id,
                level=level,
                run_budget=run_budget,
                scope_kind=(
                    BudgetScopeKind.ANALYSIS_RUN
                    if analysis_run_id is not None
                    else BudgetScopeKind.REQUEST
                ),
                metadata=replace(metadata, operation_id=stable_operation_id),
                pre_reserved=None,
                require_atomic_dollar_reservation=True,
            )
        started_usage_source = metadata.usage_source
        # A restarted worker initially owns a fresh process-local clock.  Once
        # the durable scope is loaded, tighten (never extend) the active guard
        # to the original database deadline returned with the ticket.
        if timeout_guard is not None:
            local_deadline = timeout_guard.when()
            durable_deadline = (
                asyncio.get_running_loop().time()
                + started_ticket.remaining_seconds
            )
            if local_deadline is None or durable_deadline < local_deadline:
                timeout_guard.reschedule(durable_deadline)
        return started_ticket

    async def _finish_attempt(
        ticket: AttemptTicket, outcome: AttemptOutcome
    ) -> None:
        async with _new_accounting_session() as accounting:
            await finalize_attempt(
                accounting,
                ticket=ticket,
                outcome=outcome,
            )

    async def _finish_candidate(
        ticket: AttemptTicket,
        outcome: AttemptOutcome,
        candidate: SemanticCandidate,
    ) -> None:
        if (
            candidate.contract_name != SUMMARY_CANDIDATE_CONTRACT
            or candidate.binding_fingerprint
            != candidate_context.binding_fingerprint
        ):
            raise LLMLifecycleError(
                "summary semantic candidate binding changed"
            )
        async with _new_accounting_session() as accounting:
            await finalize_attempt(
                accounting,
                ticket=ticket,
                outcome=outcome,
                candidate=candidate,
            )

    async def _replay_candidate(
        request: SemanticCandidateReplayRequest,
    ) -> SemanticCandidateReplay:
        if (
            request.contract_name != SUMMARY_CANDIDATE_CONTRACT
            or request.binding_fingerprint
            != candidate_context.binding_fingerprint
        ):
            raise LLMLifecycleError(
                "summary semantic candidate replay binding changed"
            )
        async with _new_accounting_session() as accounting:
            return await load_terminal_semantic_candidate(
                accounting,
                operation_id=request.operation_id,
                attempt_no=request.attempt_no,
                project_id=project_id,
                purpose=LLMPurpose.SUMMARY,
                input_fingerprint=request.input_fingerprint,
                contract_name=request.contract_name,
                binding_fingerprint=request.binding_fingerprint,
                normalizer=normalize_summary_candidate_payload,
            )

    async def _replay_attempt(
        request: SemanticCandidateReplayRequest,
    ) -> AttemptReplayResult:
        if (
            request.operation_id != stable_operation_id
            or request.contract_name != SUMMARY_CANDIDATE_CONTRACT
            or request.binding_fingerprint
            != candidate_context.binding_fingerprint
        ):
            raise LLMLifecycleError(
                "summary semantic attempt replay binding changed"
            )
        async with _new_accounting_session() as accounting:
            return await load_attempt_replay(
                accounting,
                operation_id=request.operation_id,
                attempt_no=request.attempt_no,
                project_id=project_id,
                purpose=LLMPurpose.SUMMARY,
                # Provider/model routing can change across restarts while the
                # semantic product identity remains exact.
                input_fingerprint=None,
                contract_name=request.contract_name,
                binding_fingerprint=request.binding_fingerprint,
                normalizer=normalize_summary_candidate_payload,
            )

    callbacks = AttemptCallbacks(
        start=_start_attempt,
        finish=_finish_attempt,
        finish_candidate=_finish_candidate,
        replay_candidate=_replay_candidate,
        replay_attempt=_replay_attempt,
        mark_provider_dispatch=lambda: _mark_provider_dispatch(),
    )

    def _mark_provider_dispatch() -> None:
        nonlocal provider_dispatch_started
        provider_dispatch_started = True

    try:
        if run_budget is None:
            result = await extractor.summarize(level, target_id, evidence)
        else:
            async with use_attempt_callbacks(callbacks):
                if type(extractor) is not Extractor:
                    # Legacy/test adapters have no explicit dispatch marker;
                    # once invoked, conservatively treat timeout as possibly
                    # physical until they migrate to the callback boundary.
                    provider_dispatch_started = True
                # ``reserve`` only returns a positive remainder.  Do not round
                # a sub-100ms remainder up: the hard run deadline owns it.
                timeout_guard = asyncio.timeout(remaining)
                with use_summary_candidate_context(candidate_context):
                    async with timeout_guard:
                        result = await extractor.summarize(
                            level, target_id, evidence
                        )
    except RunBudgetExceeded as exc:
        return Extractor._stub(level, target_id, evidence, exc.reason)
    except LLMAttemptReplayBlocked:
        # Legacy/custom adapters that do not implement semantic replay remain
        # fail-closed and must never turn a duplicate into a second dispatch.
        return Extractor._stub(
            level, target_id, evidence, FALLBACK_ATTEMPT_REPLAY_BLOCKED
        )
    except LLMLifecycleError:
        # Accounting must succeed before dispatch.  Fail the optional AI
        # stage closed while leaving deterministic graph processing intact.
        if run_budget is not None:
            run_budget.stop(FALLBACK_ACCOUNTING_UNAVAILABLE)
        return Extractor._stub(
            level, target_id, evidence, FALLBACK_ACCOUNTING_UNAVAILABLE
        )
    except TimeoutError:
        if run_budget is not None:
            run_budget.stop(FALLBACK_RUN_DEADLINE)
        if not provider_dispatch_started:
            return Extractor._stub(
                level, target_id, evidence, FALLBACK_RUN_DEADLINE
            )
        # A provider may have consumed tokens before cancellation.  Record a
        # physical attempt with unknown usage instead of silently treating it
        # as free; the run-level call/input reservation already remains spent.
        if started_ticket is not None:
            await _finish_attempt(
                started_ticket,
                AttemptOutcome(
                    attempt_status=AttemptStatus.TIMEOUT,
                    result_status=ResultStatus.NOT_APPLICABLE,
                    usage=unavailable_usage(
                        lost_after_dispatch=True,
                        source=(started_usage_source or UsageSource.UNKNOWN),
                    ),
                    failure_code=FALLBACK_RUN_DEADLINE,
                ),
            )
        else:
            if run_budget is not None:
                run_budget.stop(FALLBACK_ACCOUNTING_UNAVAILABLE)
        result = Extractor._stub(
            level, target_id, evidence, FALLBACK_RUN_DEADLINE
        )
        if started_ticket is not None:
            setattr(result, "_llm_call_id", started_ticket.attempt_id)
        return result
    except asyncio.CancelledError:
        if started_ticket is not None:
            await _finish_attempt(
                started_ticket,
                AttemptOutcome(
                    attempt_status=AttemptStatus.CANCELLED,
                    result_status=ResultStatus.NOT_APPLICABLE,
                    usage=unavailable_usage(
                        lost_after_dispatch=True,
                        source=(started_usage_source or UsageSource.UNKNOWN),
                    ),
                    failure_code="summary_call_cancelled",
                ),
            )
        elif provider_dispatch_started and run_budget is not None:
            run_budget.stop(FALLBACK_ACCOUNTING_UNAVAILABLE)
        raise

    # A provider response with no claim cannot be presented as a grounded
    # success.  Downgrade before ledgering so the physical attempt remains
    # visible as a fallback while preserving its actual token count.
    physical_tokens_used = result.tokens_used
    if not _is_fallback(result) and not result.claims:
        llm_call_id = getattr(result, "_llm_call_id", None)
        result = Extractor._stub(
            level, target_id, evidence, FALLBACK_UNGROUNDED_RESPONSE
        )
        result.tokens_used = physical_tokens_used
        if llm_call_id is not None:
            setattr(result, "_llm_call_id", llm_call_id)
            contract_version = (
                await session.execute(
                    select(LLMCall.contract_version).where(
                        LLMCall.id == llm_call_id
                    )
                )
            ).scalar_one_or_none()
            if contract_version == 1:
                await classify_attempt_result(
                    session,
                    attempt_id=llm_call_id,
                    result_status=ResultStatus.GROUNDING_REJECTED,
                    failure_code=FALLBACK_UNGROUNDED_RESPONSE,
                )

    no_physical_call = result.fallback_reason in {
        FALLBACK_NO_BACKEND,
        FALLBACK_ANTHROPIC_IMPORT,
        FALLBACK_ANTHROPIC_CLIENT_INIT,
        FALLBACK_AGENT_SDK_INIT,
        FALLBACK_BUDGET_EXCEEDED,
        FALLBACK_EVIDENCE_BUDGET,
    }
    if no_physical_call and run_budget is not None:
        # Repeating the same unavailable backend/budget failure over hundreds
        # of targets creates useless rows and can make an opt-in run look hung.
        run_budget.stop(result.fallback_reason or FALLBACK_NO_BACKEND)
    existing_call_id = getattr(result, "_llm_call_id", None)
    if existing_call_id is not None:
        contract_version = (
            await session.execute(
                select(LLMCall.contract_version).where(
                    LLMCall.id == existing_call_id
                )
            )
        ).scalar_one_or_none()
        if contract_version != 1:
            run_budget.stop(FALLBACK_ACCOUNTING_UNAVAILABLE)
            return Extractor._stub(
                level,
                target_id,
                evidence,
                FALLBACK_ACCOUNTING_UNAVAILABLE,
            )
    if not no_physical_call and existing_call_id is None:
        # A provider-derived result without a committed v1 STARTED ticket is
        # an accounting bypass.  Never manufacture the old v0 row after the
        # fact: it cannot prove pre-dispatch authorization or at-most-once
        # operation identity.
        run_budget.stop(FALLBACK_ACCOUNTING_UNAVAILABLE)
        return Extractor._stub(
            level,
            target_id,
            evidence,
            FALLBACK_ACCOUNTING_UNAVAILABLE,
        )
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


def _summary_projection(result: ExtractorResult) -> dict[str, Any]:
    return {
        "summary": result.summary,
        "detailed": result.detailed,
        "claims": result.claims,
        "open_questions": result.open_questions,
    }


def _projection_sha256(projection: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


async def _persist_summary_result(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    target_id: str,
    level: int,
    analysis_run_id: uuid.UUID | None,
    graph_generation: int,
    overlay_generation: int,
    candidate_evidence: list[dict[str, Any]],
    product_evidence_hash: str,
    result: ExtractorResult,
) -> uuid.UUID:
    """Atomically publish one grounded Summary and its LLM classification."""

    await lock_ready_summary_generation(
        session,
        project_id=project_id,
        expected_generation=graph_generation,
        expected_overlay_generation=overlay_generation,
    )
    generated_at = datetime.now(tz=timezone.utc)
    attempt_id = getattr(result, "_llm_call_id", None)
    projection = _summary_projection(result)

    if not _is_fallback(result):
        if not isinstance(attempt_id, uuid.UUID):
            raise LLMLifecycleError(
                "grounded Summary has no durable physical attempt"
            )
        binding_fingerprint = fingerprint_input(
            "mnemos.summary.binding.v1",
            str(project_id),
            str(level),
            target_id,
            evidence_hash(candidate_evidence),
            str(graph_generation),
            str(overlay_generation),
        )
        receipt = await lock_semantic_candidate_for_product(
            session,
            attempt_id=attempt_id,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            contract_name=SUMMARY_CANDIDATE_CONTRACT,
            binding_fingerprint=binding_fingerprint,
            normalizer=normalize_summary_candidate_payload,
        )
        candidate = normalize_summary_candidate_payload(receipt.payload)
        # Re-run grounding while the graph head is locked. This makes
        # certainty a DB-owned projection at the final product boundary and
        # prevents mutable caller state from upgrading inferred/asserted facts
        # to verified merely because claim/reference identity still matches.
        grounded_projection, _rejected = await validate_claims(
            session,
            project_id=project_id,
            claims=candidate["claims"],
            provided_evidence=candidate_evidence,
        )
        if (
            receipt.candidate_status not in {"candidate", "accepted"}
            or receipt.result_status
            not in {ResultStatus.PENDING, ResultStatus.ACCEPTED}
            or candidate["summary"] != result.summary
            or candidate["detailed"] != result.detailed
            or candidate["open_questions"] != result.open_questions
            or grounded_projection != result.claims
        ):
            raise LLMLifecycleError(
                "grounded Summary does not match its semantic candidate"
            )

        row_id = uuid.uuid5(
            attempt_id,
            "mnemos.summary.grounded-product.v1",
        )
        projection_digest = _projection_sha256(projection)
        # Product provenance is derived only from the locked immutable receipt.
        # For map/reduce summaries this is the final candidate-producing call;
        # whole-run usage belongs to the physical LLMCall ledger, where every
        # contributing call remains independently attributable.
        product_model_used = receipt.resolved_model or receipt.requested_model
        product_tokens_used = receipt.total_tokens
        existing_rows = (
            await session.execute(
                select(Summary)
                .where(
                    or_(
                        Summary.id == row_id,
                        Summary.llm_attempt_id == attempt_id,
                    )
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        if len(existing_rows) > 1:
            raise LLMLifecycleError(
                "grounded Summary attempt owns multiple products"
            )
        await classify_attempt_result(
            session,
            attempt_id=attempt_id,
            result_status=ResultStatus.ACCEPTED,
            commit=False,
        )
        expected = (
            row_id,
            project_id,
            target_id,
            level,
            analysis_run_id,
            graph_generation,
            overlay_generation,
            projection["summary"],
            projection["detailed"],
            projection["claims"],
            projection["open_questions"],
            product_evidence_hash,
            product_model_used,
            product_tokens_used,
            None,
            attempt_id,
            binding_fingerprint,
            projection_digest,
            None,
        )
        if existing_rows:
            existing = existing_rows[0]
            actual = (
                existing.id,
                existing.project_id,
                existing.target_id,
                existing.level,
                existing.analysis_run_id,
                existing.validated_graph_generation,
                existing.validated_overlay_generation,
                existing.summary,
                existing.detailed,
                existing.claims,
                existing.open_questions,
                existing.evidence_hash,
                existing.model_used,
                existing.tokens_used,
                existing.fallback_reason,
                existing.llm_attempt_id,
                existing.semantic_binding_fingerprint,
                existing.projection_sha256,
                existing.superseded_by,
            )
            if actual != expected:
                raise LLMLifecycleError(
                    "grounded Summary replay projection changed"
                )
            await session.commit()
            return existing.id

        await session.execute(
            Summary.__table__.update()
            .where(
                Summary.project_id == project_id,
                Summary.target_id == target_id,
                Summary.level == level,
                Summary.superseded_by.is_(None),
                Summary.id != row_id,
            )
            .values(superseded_by=row_id)
        )
        session.add(
            Summary(
                id=row_id,
                project_id=project_id,
                target_id=target_id,
                level=level,
                summary=projection["summary"],
                detailed=projection["detailed"],
                claims=projection["claims"],
                open_questions=projection["open_questions"],
                evidence_hash=product_evidence_hash,
                model_used=product_model_used,
                tokens_used=product_tokens_used,
                analysis_run_id=analysis_run_id,
                validated_graph_generation=graph_generation,
                validated_overlay_generation=overlay_generation,
                validated_at=generated_at,
                fallback_reason=None,
                generated_at=generated_at,
                llm_attempt_id=attempt_id,
                semantic_binding_fingerprint=binding_fingerprint,
                projection_sha256=projection_digest,
            )
        )
        await session.commit()
        return row_id

    # Deterministic/no-provider fallbacks deliberately have no invented LLM
    # provenance.  They still publish under the same graph-head lock.
    await _supersede_current(session, project_id, target_id, level)
    row = Summary(
        project_id=project_id,
        target_id=target_id,
        level=level,
        summary=projection["summary"],
        detailed=projection["detailed"],
        claims=projection["claims"],
        open_questions=projection["open_questions"],
        evidence_hash=product_evidence_hash,
        model_used=result.model_used,
        tokens_used=result.tokens_used,
        analysis_run_id=analysis_run_id,
        validated_graph_generation=graph_generation,
        validated_overlay_generation=overlay_generation,
        validated_at=generated_at,
        fallback_reason=result.fallback_reason or None,
        generated_at=generated_at,
    )
    session.add(row)
    await session.commit()
    return row.id


async def _ground_result_or_fallback(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    level: int,
    target_id: str,
    evidence: list[dict[str, Any]],
    result: ExtractorResult,
    defer_accept: bool = False,
) -> ExtractorResult:
    """Validate exact provider scope or return an explicit ungrounded stub."""

    llm_call_id = getattr(result, "_llm_call_id", None)
    accepted, _rejected = await validate_claims(
        session,
        project_id=project_id,
        claims=result.claims,
        provided_evidence=evidence,
    )
    call_contract_version: int | None = None
    if llm_call_id is not None:
        call_contract_version = (
            await session.execute(
                select(LLMCall.contract_version).where(
                    LLMCall.id == llm_call_id
                )
            )
        ).scalar_one_or_none()
    if not _is_fallback(result) and not accepted:
        tokens_used = result.tokens_used
        result = Extractor._stub(
            level, target_id, evidence, FALLBACK_UNGROUNDED_RESPONSE
        )
        result.tokens_used = tokens_used
        if llm_call_id is not None and call_contract_version == 1:
            await classify_attempt_result(
                session,
                attempt_id=llm_call_id,
                result_status=ResultStatus.GROUNDING_REJECTED,
                failure_code=FALLBACK_UNGROUNDED_RESPONSE,
            )
        elif llm_call_id is not None:
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
    elif (
        not _is_fallback(result)
        and accepted
        and llm_call_id is not None
        and call_contract_version == 1
        and not defer_accept
    ):
        await classify_attempt_result(
            session,
            attempt_id=llm_call_id,
            result_status=ResultStatus.ACCEPTED,
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
            .order_by(Node.id.asc())
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
        .order_by(func.count().desc(), Edge.target_id.asc())
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
    wanted_rank = {node_id: index for index, node_id in enumerate(wanted)}
    high_deg_rows.sort(
        key=lambda node: wanted_rank.get(node.id, len(wanted_rank))
    )

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
        .order_by(Node.id.asc())
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
        .order_by(func.count().desc(), Edge.target_id.asc())
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
            .order_by(Node.id.asc())
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


_L1_SQLITE_BATCH_SIZE = 64


def _uses_sqlite(session: AsyncSession) -> bool:
    """Return true only for the local SQLite execution path."""

    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    return getattr(dialect, "name", None) == "sqlite"


async def _sqlite_l1_edges(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    nodes: list[Node],
    direction: str,
) -> list[Edge]:
    """Load each target's historical ordered top ten in one SQLite query.

    Every UNION branch is the former per-target SELECT, including its exact
    predicates, order, and limit.  The 64-target ceiling keeps the compound
    statement at no more than 256 SQLite binds, below the portable 999-bind
    ceiling.  The outer order is explicit because UNION branch order is not a
    result-order contract.
    """

    branches = []
    for index, node in enumerate(nodes):
        if direction == "out":
            target_filter = Edge.source_id == node.id
            edge_order = (
                Edge.kind.asc(),
                Edge.target_id.asc(),
                Edge.source_id.asc(),
                Edge.id.asc(),
            )
        else:
            target_filter = Edge.target_id == node.id
            edge_order = (
                Edge.kind.asc(),
                Edge.source_id.asc(),
                Edge.target_id.asc(),
                Edge.id.asc(),
            )
        target_edges = (
            select(Edge)
            .where(
                Edge.project_id == project_id,
                target_filter,
                Edge.valid_to.is_(None),
            )
            .order_by(*edge_order)
            .limit(10)
            .subquery(f"l1_{direction}_{index}")
        )
        branches.append(
            select(
                *(target_edges.c[column.key] for column in Edge.__table__.columns)
            )
        )

    combined = union_all(*branches).subquery(f"l1_{direction}_batch")
    edge = aliased(Edge, combined)
    if direction == "out":
        batch_order = (
            edge.source_id.asc(),
            edge.kind.asc(),
            edge.target_id.asc(),
            edge.source_id.asc(),
            edge.id.asc(),
        )
    else:
        batch_order = (
            edge.target_id.asc(),
            edge.kind.asc(),
            edge.source_id.asc(),
            edge.target_id.asc(),
            edge.id.asc(),
        )
    return (
        await session.execute(select(edge).order_by(*batch_order))
    ).scalars().all()


async def _sqlite_l1_batch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    nodes: list[Node],
) -> tuple[
    dict[str, list[Edge]],
    dict[str, list[Edge]],
    dict[str, Any],
    Any,
]:
    """Preload one bounded SQLite L1 block without changing evidence order."""

    outgoing = {node.id: [] for node in nodes}
    incoming = {node.id: [] for node in nodes}
    for edge in await _sqlite_l1_edges(
        session,
        project_id=project_id,
        nodes=nodes,
        direction="out",
    ):
        outgoing[edge.source_id].append(edge)
    for edge in await _sqlite_l1_edges(
        session,
        project_id=project_id,
        nodes=nodes,
        direction="in",
    ):
        incoming[edge.target_id].append(edge)

    node_overlays = await load_node_human_overlays(
        session,
        project_id=project_id,
        node_ids=(node.id for node in nodes),
    )
    neighbour_edges = [
        edge
        for node in nodes
        for edge in (*incoming[node.id], *outgoing[node.id])
    ]
    edge_overlays = await load_edge_overlays(
        session,
        project_id=project_id,
        identities=(edge_identity(edge) for edge in neighbour_edges),
    )
    return outgoing, incoming, node_overlays, edge_overlays


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
    sqlite_batching = _uses_sqlite(session)
    sqlite_batch = None
    for node_index, sym in enumerate(nodes):
        if run_budget is not None and run_budget.exhausted:
            break
        if sqlite_batching:
            if node_index % _L1_SQLITE_BATCH_SIZE == 0:
                sqlite_batch = await _sqlite_l1_batch(
                    session,
                    project_id=project_id,
                    nodes=nodes[node_index : node_index + _L1_SQLITE_BATCH_SIZE],
                )
            if sqlite_batch is None:
                raise AssertionError("SQLite L1 batch was not loaded")
            outgoing, incoming, node_overlays, edge_overlays = sqlite_batch
            neighbours_out = outgoing[sym.id]
            neighbours_in = incoming[sym.id]
            neighbour_edges = [*neighbours_in, *neighbours_out]
        else:
            neighbours_out = (
                await session.execute(
                    select(Edge)
                    .where(
                        Edge.project_id == project_id,
                        Edge.source_id == sym.id,
                        Edge.valid_to.is_(None),
                    )
                    .order_by(
                        Edge.kind.asc(),
                        Edge.target_id.asc(),
                        Edge.source_id.asc(),
                        Edge.id.asc(),
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
                    .order_by(
                        Edge.kind.asc(),
                        Edge.source_id.asc(),
                        Edge.target_id.asc(),
                        Edge.id.asc(),
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

        # Paid dispatch authorization is enforced atomically by
        # ``begin_attempt``. Missing/zero policy, exhausted headroom, or an
        # unsupported price contract all fall through to the explicit stub.
        result = await _summarize_with_budget(
            session,
            extractor,
            project_id,
            1,
            sym.id,
            bounded_evidence,
            analysis_run_id,
            run_budget,
            graph_generation=graph_generation,
            overlay_generation=overlay_generation,
        )
        result = await _ground_result_or_fallback(
            session,
            project_id=project_id,
            level=1,
            target_id=sym.id,
            evidence=bounded_evidence,
            result=result,
            defer_accept=True,
        )
        await _persist_summary_result(
            session,
            project_id=project_id,
            target_id=sym.id,
            level=1,
            analysis_run_id=analysis_run_id,
            graph_generation=graph_generation,
            overlay_generation=overlay_generation,
            candidate_evidence=bounded_evidence,
            product_evidence_hash=evidence_hash(bounded_evidence),
            result=result,
        )
        count += 1
        # Commit per item so the SQLite write lock (local mode runs the
        # API and this inline job in one process against one file) is
        # released between LLM calls — otherwise a single end-of-stage
        # commit holds the lock for the whole batch and the separate
        # session behind progress_cb / a concurrent request hits
        # "database is locked" (PR-141 pattern). Postgres is unaffected.
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

    # Selection is pushed into SQL so pre-call DB/RAM work is bounded by
    # ``limit`` instead of project size.  Grouping on the raw column is exact:
    # human overlays only add the human payload key to ``data`` and never
    # rewrite ``location.file`` (see graph_overlays.materialize_node_data).
    loc_file = Node.data["location"]["file"].astext
    l1_current = (
        Summary.project_id == project_id,
        Summary.level == 1,
        Summary.superseded_by.is_(None),
        Summary.validated_graph_generation == graph_generation,
        Summary.validated_overlay_generation == overlay_generation,
        Node.project_id == project_id,
        Node.valid_to.is_(None),
    )
    selected_files = (
        await session.execute(
            select(loc_file)
            .select_from(Summary)
            .join(Node, Node.id == Summary.target_id)
            .where(*l1_current, loc_file.isnot(None), loc_file != "")
            .distinct()
            .order_by(loc_file.asc())
            .limit(limit)
        )
    ).scalars().all()
    l1_rows = []
    if selected_files:
        l1_rows = (
            await session.execute(
                select(Summary, Node)
                .join(Node, Node.id == Summary.target_id)
                .where(*l1_current, loc_file.in_(selected_files))
                .order_by(Summary.target_id.asc(), Summary.id.asc(), Node.id.asc())
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
    for file_path, group in sorted(by_file.items(), key=lambda item: item[0])[:limit]:
        if run_budget is not None and run_budget.exhausted:
            break
        raw = []
        for summary, node in sorted(
            group, key=lambda item: (item[1].id, item[0].id)
        ):
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
                graph_generation=graph_generation,
                overlay_generation=overlay_generation,
            )
            partial = await _ground_result_or_fallback(
                session,
                project_id=project_id,
                level=2,
                target_id=target_label,
                evidence=chunk,
                result=partial,
                defer_accept=(len(chunks) == 1),
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
                    graph_generation=graph_generation,
                    overlay_generation=overlay_generation,
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
            defer_accept=True,
        )
        await _persist_summary_result(
            session,
            project_id=project_id,
            target_id=file_path,
            level=2,
            analysis_run_id=analysis_run_id,
            graph_generation=graph_generation,
            overlay_generation=overlay_generation,
            candidate_evidence=validation_evidence,
            product_evidence_hash=evidence_hash(flat_hash_input),
            result=result,
        )
        count += 1
        # Commit per item so the SQLite write lock (local mode runs the
        # API and this inline job in one process against one file) is
        # released between LLM calls — otherwise a single end-of-stage
        # commit holds the lock for the whole batch and the separate
        # session behind progress_cb / a concurrent request hits
        # "database is locked" (PR-141 pattern). Postgres is unaffected.
        if progress_cb is not None:
            await progress_cb()
    await session.commit()
    return count


# SQLite's default bound-parameter ceiling is far above this, but keeping IN
# lists small also keeps per-statement work bounded on huge modules.
_SUMMARY_TARGET_BATCH = 500


def _batched(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


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

    # Bounded selection: one narrow key scan (a single ``target_id`` string
    # per current L2 summary) picks the first ``limit`` modules; every
    # full-row load below is restricted to the files of those modules.  Batch
    # slices of the sorted file list keep global (target_id, id) order while
    # staying under SQLite's bound-parameter ceiling on very large modules.
    l2_current = (
        Summary.project_id == project_id,
        Summary.level == 2,
        Summary.superseded_by.is_(None),
        Summary.validated_graph_generation == graph_generation,
        Summary.validated_overlay_generation == overlay_generation,
    )
    l2_targets = (
        await session.execute(
            select(Summary.target_id)
            .where(*l2_current)
            .distinct()
            .order_by(Summary.target_id.asc())
        )
    ).scalars().all()
    files_by_module: dict[str, list[str]] = {}
    for target in l2_targets:
        parts = target.strip("/").split("/", 1)
        module_key = parts[0] if parts else "root"
        files_by_module.setdefault(module_key, []).append(target)
    selected_files = sorted(
        {
            target
            for module_key in sorted(files_by_module)[:limit]
            for target in files_by_module[module_key]
        }
    )

    l2_rows: list[Summary] = []
    for batch in _batched(selected_files, _SUMMARY_TARGET_BATCH):
        l2_rows.extend(
            (
                await session.execute(
                    select(Summary)
                    .where(*l2_current, Summary.target_id.in_(batch))
                    .order_by(Summary.target_id.asc(), Summary.id.asc())
                )
            ).scalars().all()
        )

    # L2 targets are presentation keys (source paths), not graph node IDs.
    # Rebuild their grounding from the same current L1-summary/current-Node
    # relation used by L2.  This keeps both single- and multi-chunk L3 input
    # citable without teaching the validator to trust path-shaped pseudo IDs.
    loc_file = Node.data["location"]["file"].astext
    l1_node_rows: list[Any] = []
    for batch in _batched(selected_files, _SUMMARY_TARGET_BATCH):
        l1_node_rows.extend(
            (
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
                        loc_file.in_(batch),
                    )
                    .order_by(
                        Summary.target_id.asc(), Summary.id.asc(), Node.id.asc()
                    )
                )
            ).all()
        )
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
    for module, group in sorted(by_module.items(), key=lambda item: item[0])[:limit]:
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
                graph_generation=graph_generation,
                overlay_generation=overlay_generation,
            )
        else:
            partial_results: list[ExtractorResult] = []
            for i, chunk in enumerate(chunks):
                r = await _summarize_with_budget(
                    session, extractor, project_id, 3,
                    f"{module}#chunk{i + 1}", chunk,
                    analysis_run_id,
                    run_budget,
                    graph_generation=graph_generation,
                    overlay_generation=overlay_generation,
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
                    graph_generation=graph_generation,
                    overlay_generation=overlay_generation,
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
            defer_accept=True,
        )
        await _persist_summary_result(
            session,
            project_id=project_id,
            target_id=module,
            level=3,
            analysis_run_id=analysis_run_id,
            graph_generation=graph_generation,
            overlay_generation=overlay_generation,
            candidate_evidence=validation_evidence,
            product_evidence_hash=evidence_hash(raw),
            result=result,
        )
        count += 1
        # Commit per item so the SQLite write lock (local mode runs the
        # API and this inline job in one process against one file) is
        # released between LLM calls — otherwise a single end-of-stage
        # commit holds the lock for the whole batch and the separate
        # session behind progress_cb / a concurrent request hits
        # "database is locked" (PR-141 pattern). Postgres is unaffected.
        if progress_cb is not None:
            await progress_cb()
    await session.commit()
    return count
