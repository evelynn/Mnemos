"""Crash-safe, revision-bound independent Gate-B second opinion.

The pass uses one direct Anthropic request and a dedicated structured output
contract.  Physical dispatch is authorized only by the durable LLM lifecycle.
The provider-normalized candidate commits atomically with the terminal attempt.
Grounding follows without a second dispatch; candidate classification and the
durable product then commit in one transaction.  If the worker stops before
that transaction, a restart decrypts and revalidates the exact revision-bound
candidate before rebuilding the product.  A terminal attempt without a
candidate always fails closed.

No prompt, raw provider response, diff text, or exception is persisted here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.cost import (
    BudgetExceeded,
    LLMRunBudget,
    RunBudgetExceeded,
    require_budget,
)
from app.graph_publication import (
    GraphPublicationError,
    lock_validated_graph_head,
)
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMPurpose,
    ResultStatus,
    UsageSource,
)
from app.llm.lifecycle import (
    AttemptCallbacks,
    AttemptOutcome,
    AttemptStartMetadata,
    AttemptTicket,
    BudgetScopeKind,
    LLMAttemptReplayBlocked,
    LLMLifecycleError,
    LLMSemanticCandidateCorrupt,
    LLMSemanticCandidateUnavailable,
    SemanticCandidate,
    SemanticCandidateReplay,
    SemanticCandidateReplayRequest,
    begin_attempt,
    classify_attempt_result,
    finalize_attempt,
    fingerprint_input,
    load_terminal_semantic_candidate,
    lock_semantic_candidate_for_product,
    use_attempt_callbacks,
)
from app.models.findings import LLMCall
from app.models.graph import AnalysisRun
from app.models.llm import LLMSemanticCandidate
from app.models.plans import Plan, SecondOpinionProduct
from app.review_revision import CanonicalReviewRevision
from app.safety.review.second_opinion_provider import (
    SECOND_OPINION_MODEL,
    SECOND_OPINION_SYSTEM_PROMPT,
    review_via_anthropic,
)
from app.safety.review.second_opinion_schema import (
    DiffEvidencePack,
    GroundedSecondOpinion,
    SECOND_OPINION_CONTRACT,
    SecondOpinionSchemaError,
    collect_diff_line_evidence,
    ground_second_opinion_payload,
    normalize_second_opinion_candidate_payload,
    normalize_second_opinion_payload,
)
from app.safety.review.types import Finding


log = logging.getLogger(__name__)

SECOND_OPINION_MAX_PROMPT_CHARS = 24_000
SECOND_OPINION_MAX_PRIOR_FINDINGS = 50
SECOND_OPINION_MAX_PRIOR_MESSAGE_CHARS = 400
SECOND_OPINION_MAX_INPUT_TOKENS = 32_000
SECOND_OPINION_OUTPUT_TOKENS = 1_024
SECOND_OPINION_WALL_TIME_SEC = 60

FAILURE_INPUT_BOUNDED = "second_opinion_input_bounded"
FAILURE_BUDGET = "second_opinion_budget_exceeded"
FAILURE_REPLAY = "second_opinion_replay_blocked"
FAILURE_IN_PROGRESS = "second_opinion_attempt_in_progress"
FAILURE_TERMINAL_WITHOUT_PRODUCT = (
    "second_opinion_terminal_without_product"
)
FAILURE_PRODUCT_INVALID = "second_opinion_product_invalid"
FAILURE_REVISION_UNBOUND = "second_opinion_revision_unbound"
FAILURE_ACCOUNTING = "second_opinion_accounting_failed"
FAILURE_PROVIDER = "second_opinion_provider_failed"
FAILURE_GROUNDING = "second_opinion_grounding_rejected"

_CANONICAL_GIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")

Coverage = Literal["complete", "incomplete"]


@dataclass(frozen=True, slots=True)
class SecondOpinionInput:
    prompt: str
    evidence: DiffEvidencePack
    complete: bool


@dataclass(frozen=True, slots=True)
class SecondOpinionReview:
    """Safe pipeline result reconstructed from one canonical product."""

    findings: tuple[Finding, ...] = ()
    coverage: Coverage = "incomplete"
    failure_code: str | None = None
    operation_id: uuid.UUID | None = None
    input_fingerprint: str | None = None
    attempt_id: uuid.UUID | None = None
    replayed: bool = False
    canonical_payload: dict[str, Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class _ProductIdentity:
    operation_id: uuid.UUID
    project_id: uuid.UUID
    plan_id: uuid.UUID
    input_fingerprint: str
    diff_sha256: str
    binding_fingerprint: str
    revision: CanonicalReviewRevision


@dataclass(frozen=True, slots=True)
class _ReplayLookup:
    review: SecondOpinionReview | None = None
    candidate: SemanticCandidateReplay | None = None
    failure_code: str | None = None


def _accounting_session_factory():  # noqa: ANN202
    """Resolve the reload-safe application session factory lazily."""

    from app.db import SessionLocal

    return SessionLocal()


def _canonical_prior_finding(finding: Finding) -> dict[str, Any]:
    return {
        "pass": finding.pass_name,
        "severity": finding.severity,
        "rule": finding.rule,
        "location": finding.location,
        "message": " ".join(finding.message.split())[
            :SECOND_OPINION_MAX_PRIOR_MESSAGE_CHARS
        ],
    }


def build_second_opinion_input(
    *,
    diff: str,
    prior_findings: list[Finding],
) -> SecondOpinionInput:
    """Build one deterministic complete-JSON prompt under a hard bound."""

    source_pack = collect_diff_line_evidence(diff)
    ordered_prior = sorted(
        (_canonical_prior_finding(finding) for finding in prior_findings),
        key=lambda item: (
            item["pass"],
            item["severity"],
            item["rule"],
            item["location"],
            item["message"],
        ),
    )
    complete = source_pack.complete
    if len(ordered_prior) > SECOND_OPINION_MAX_PRIOR_FINDINGS:
        complete = False
        ordered_prior = ordered_prior[:SECOND_OPINION_MAX_PRIOR_FINDINGS]

    envelope: dict[str, Any] = {
        "contract": SECOND_OPINION_CONTRACT,
        "prior_findings": [],
        "diff_lines": [],
    }

    def _serialized() -> str:
        return json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    selected_lines = []
    for line in source_pack.lines:
        envelope["diff_lines"].append(line.prompt_value())
        if len(_serialized()) > SECOND_OPINION_MAX_PROMPT_CHARS:
            envelope["diff_lines"].pop()
            complete = False
            break
        selected_lines.append(line)

    if len(selected_lines) != len(source_pack.lines):
        complete = False
    # Exact changed-line evidence owns the prompt budget. Prior findings are
    # only deduplication context and cannot crowd out the reviewed source.
    for prior in ordered_prior:
        envelope["prior_findings"].append(prior)
        if len(_serialized()) > SECOND_OPINION_MAX_PROMPT_CHARS:
            envelope["prior_findings"].pop()
            complete = False
            break
    selected_pack = DiffEvidencePack(
        lines=tuple(selected_lines),
        complete=complete,
    )
    return SecondOpinionInput(
        prompt=_serialized(),
        evidence=selected_pack,
        complete=complete,
    )


def _identity(
    *,
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    diff: str,
    input_fingerprint: str,
    revision: CanonicalReviewRevision,
) -> _ProductIdentity:
    if revision.project_id != project_id:
        raise LLMLifecycleError("second-opinion revision project mismatch")
    if (
        type(revision.source_generation) is not int
        or revision.source_generation < 1
        or type(revision.overlay_generation) is not int
        or revision.overlay_generation < 0
        or _CANONICAL_GIT_SHA.fullmatch(revision.git_sha) is None
    ):
        raise LLMLifecycleError("second-opinion revision is not canonical")
    diff_sha256 = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    operation_id = uuid.uuid5(
        plan_id,
        "\x00".join(
            (
                LLMPurpose.SECOND_OPINION.value,
                str(project_id),
                SECOND_OPINION_CONTRACT,
                SECOND_OPINION_MODEL,
                str(revision.run_id),
                str(revision.source_generation),
                str(revision.overlay_generation),
                revision.git_sha,
                diff_sha256,
                input_fingerprint,
            )
        ),
    )
    binding_fingerprint = fingerprint_input(
        "mnemos.second_opinion.candidate_binding.v1",
        str(project_id),
        str(plan_id),
        str(revision.run_id),
        str(revision.source_generation),
        str(revision.overlay_generation),
        revision.git_sha,
        diff_sha256,
        input_fingerprint,
    )
    return _ProductIdentity(
        operation_id=operation_id,
        project_id=project_id,
        plan_id=plan_id,
        input_fingerprint=input_fingerprint,
        diff_sha256=diff_sha256,
        binding_fingerprint=binding_fingerprint,
        revision=revision,
    )


def _product_shape(
    *,
    review_input: SecondOpinionInput,
    grounded: GroundedSecondOpinion,
) -> tuple[Coverage, str, str | None]:
    grounding_status = (
        "fully_grounded"
        if grounded.rejected_findings == 0
        else "rejected"
    )
    coverage: Coverage = (
        "complete"
        if review_input.complete and grounded.rejected_findings == 0
        else "incomplete"
    )
    failure_code = None
    if grounded.rejected_findings:
        failure_code = FAILURE_GROUNDING
    elif not review_input.complete:
        failure_code = FAILURE_INPUT_BOUNDED
    return coverage, grounding_status, failure_code


async def _validate_persistence_bindings(
    session: AsyncSession,
    identity: _ProductIdentity,
) -> None:
    plan_project_id = (
        await session.execute(
            select(Plan.project_id).where(Plan.id == identity.plan_id)
        )
    ).scalar_one_or_none()
    if plan_project_id != identity.project_id:
        raise LLMLifecycleError("second-opinion plan binding is invalid")
    try:
        head, receipt = await lock_validated_graph_head(
            session,
            project_id=identity.project_id,
        )
    except GraphPublicationError:
        raise LLMLifecycleError(
            "second-opinion publication receipt is invalid"
        ) from None
    if (
        head.current_run_id != identity.revision.run_id
        or receipt.generation != identity.revision.source_generation
        or head.overlay_generation
        != identity.revision.overlay_generation
    ):
        raise LLMLifecycleError(
            "second-opinion graph generation binding is invalid"
        )
    run_git_sha = (
        await session.execute(
            select(AnalysisRun.git_sha).where(
                AnalysisRun.id == identity.revision.run_id,
                AnalysisRun.project_id == identity.project_id,
            )
        )
    ).scalar_one_or_none()
    if run_git_sha != identity.revision.git_sha:
        raise LLMLifecycleError("second-opinion graph binding is invalid")


def _product_values(
    *,
    identity: _ProductIdentity,
    review_input: SecondOpinionInput,
    grounded: GroundedSecondOpinion,
    attempt_id: uuid.UUID | None,
) -> dict[str, Any]:
    canonical = normalize_second_opinion_payload(grounded.canonical_payload)
    if canonical != grounded.canonical_payload:
        raise LLMLifecycleError("second-opinion product is not canonical")
    coverage, grounding_status, failure_code = _product_shape(
        review_input=review_input,
        grounded=grounded,
    )
    return {
        "operation_id": identity.operation_id,
        "project_id": identity.project_id,
        "plan_id": identity.plan_id,
        "attempt_id": attempt_id,
        "contract_name": SECOND_OPINION_CONTRACT,
        "input_fingerprint": identity.input_fingerprint,
        "diff_sha256": identity.diff_sha256,
        "canonical_payload": canonical,
        "coverage": coverage,
        "input_coverage": (
            "complete" if review_input.complete else "incomplete"
        ),
        "grounding_status": grounding_status,
        "failure_code": failure_code,
        "finding_count": len(canonical["findings"]),
        "evidence_line_count": len(review_input.evidence.lines),
        "rejected_findings": grounded.rejected_findings,
        "review_run_id": identity.revision.run_id,
        "review_source_generation": identity.revision.source_generation,
        "review_overlay_generation": identity.revision.overlay_generation,
        "review_git_sha": identity.revision.git_sha,
    }


def _stored_values_match(
    row: SecondOpinionProduct,
    values: dict[str, Any],
) -> bool:
    return all(getattr(row, name) == value for name, value in values.items())


def _classification_for(
    grounded: GroundedSecondOpinion,
) -> tuple[ResultStatus, str | None]:
    if grounded.rejected_findings:
        return ResultStatus.GROUNDING_REJECTED, FAILURE_GROUNDING
    return ResultStatus.ACCEPTED, None


def _canonical_candidate_receipt(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    canonical = normalize_second_opinion_candidate_payload(payload)
    if canonical != payload:
        raise LLMLifecycleError(
            "second-opinion provider candidate is not privacy canonical"
        )
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return canonical, encoded


def _validate_locked_candidate_for_product(
    replay: SemanticCandidateReplay,
    *,
    identity: _ProductIdentity,
    attempt_id: uuid.UUID,
    payload: dict[str, Any],
    grounded: GroundedSecondOpinion,
) -> None:
    canonical, _ = _canonical_candidate_receipt(payload)
    result_status, _ = _classification_for(grounded)
    classified_candidate_status = (
        "accepted"
        if result_status == ResultStatus.ACCEPTED
        else "rejected"
    )
    expected_candidate_status = (
        "candidate"
        if replay.result_status == ResultStatus.PENDING
        else classified_candidate_status
    )
    if (
        replay.attempt_id != attempt_id
        or replay.operation_id != identity.operation_id
        or replay.attempt_no != 1
        or replay.project_id != identity.project_id
        or replay.purpose != LLMPurpose.SECOND_OPINION
        or replay.contract_name != SECOND_OPINION_CONTRACT
        or replay.input_fingerprint != identity.input_fingerprint
        or replay.binding_fingerprint != identity.binding_fingerprint
        or replay.payload != canonical
        or replay.requested_model != SECOND_OPINION_MODEL
        or replay.attempt_status != AttemptStatus.COMPLETED
        or replay.result_status
        not in {ResultStatus.PENDING, result_status}
        or replay.candidate_status != expected_candidate_status
    ):
        raise LLMLifecycleError(
            "second-opinion durable candidate receipt is invalid"
        )


def _validate_attempt_for_product(
    row: LLMCall,
    *,
    identity: _ProductIdentity,
    attempt_id: uuid.UUID,
    grounded: GroundedSecondOpinion,
) -> None:
    if (
        row.id != attempt_id
        or row.contract_version != 1
        or row.operation_id != identity.operation_id
        or row.project_id != identity.project_id
        or row.purpose != LLMPurpose.SECOND_OPINION.value
        or row.input_fingerprint != identity.input_fingerprint
        or row.attempt_no != 1
        or row.requested_model != SECOND_OPINION_MODEL
        or row.attempt_status != AttemptStatus.COMPLETED.value
    ):
        raise LLMLifecycleError(
            "second-opinion attempt/product identity mismatch"
        )
    result_status, failure_code = _classification_for(grounded)
    if row.result_status == ResultStatus.PENDING.value:
        raise LLMLifecycleError(
            "second-opinion product classification is pending"
        )
    if (
        row.result_status != result_status.value
        or row.failure_code != failure_code
    ):
        raise LLMLifecycleError(
            "second-opinion attempt was classified differently"
        )


async def _persist_product_and_classify(
    *,
    identity: _ProductIdentity,
    review_input: SecondOpinionInput,
    grounded: GroundedSecondOpinion,
    attempt_id: uuid.UUID | None,
    candidate_payload: dict[str, Any] | None,
) -> SecondOpinionReview:
    """Atomically classify one exact candidate and persist its product."""

    values = _product_values(
        identity=identity,
        review_input=review_input,
        grounded=grounded,
        attempt_id=attempt_id,
    )
    if attempt_id is None and review_input.evidence.lines:
        raise LLMLifecycleError(
            "second-opinion reviewed evidence has no physical attempt"
        )

    if attempt_id is not None:
        if candidate_payload is None:
            raise LLMLifecycleError(
                "second-opinion product has no candidate receipt"
            )

    async with _accounting_session_factory() as accounting:
        try:
            await _validate_persistence_bindings(accounting, identity)
            if attempt_id is not None:
                if candidate_payload is None:  # pragma: no cover - above
                    raise LLMLifecycleError(
                        "second-opinion product has no candidate receipt"
                    )
                candidate = await lock_semantic_candidate_for_product(
                    accounting,
                    attempt_id=attempt_id,
                    project_id=identity.project_id,
                    purpose=LLMPurpose.SECOND_OPINION,
                    contract_name=SECOND_OPINION_CONTRACT,
                    binding_fingerprint=identity.binding_fingerprint,
                    input_fingerprint=identity.input_fingerprint,
                    normalizer=normalize_second_opinion_candidate_payload,
                )
                _validate_locked_candidate_for_product(
                    candidate,
                    identity=identity,
                    attempt_id=attempt_id,
                    payload=candidate_payload,
                    grounded=grounded,
                )
                result_status, failure_code = _classification_for(grounded)
                await classify_attempt_result(
                    accounting,
                    attempt_id=attempt_id,
                    result_status=result_status,
                    failure_code=failure_code,
                    commit=False,
                )

            dialect = accounting.get_bind().dialect.name
            if dialect == "postgresql":
                statement = postgresql_insert(SecondOpinionProduct).values(
                    **values
                )
                statement = statement.on_conflict_do_nothing(
                    index_elements=[SecondOpinionProduct.operation_id]
                )
                await accounting.execute(statement)
            elif dialect == "sqlite":
                statement = sqlite_insert(SecondOpinionProduct).values(
                    **values
                )
                statement = statement.on_conflict_do_nothing(
                    index_elements=[SecondOpinionProduct.operation_id]
                )
                await accounting.execute(statement)
            else:  # pragma: no cover - supported stores are PG and SQLite
                raise LLMLifecycleError(
                    "second-opinion product store dialect is unsupported"
                )

            product = (
                await accounting.execute(
                    select(SecondOpinionProduct)
                    .where(
                        SecondOpinionProduct.operation_id
                        == identity.operation_id
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            if not _stored_values_match(product, values):
                raise LLMLifecycleError(
                    "second-opinion stable product replay changed"
                )
            await accounting.commit()
        except Exception:
            if accounting.in_transaction():
                await accounting.rollback()
            raise

    coverage, _grounding_status, failure_code = _product_shape(
        review_input=review_input,
        grounded=grounded,
    )
    return SecondOpinionReview(
        findings=grounded.findings,
        coverage=coverage,
        failure_code=failure_code,
        operation_id=identity.operation_id,
        input_fingerprint=identity.input_fingerprint,
        attempt_id=attempt_id,
        replayed=False,
        canonical_payload=grounded.canonical_payload,
    )


async def _review_from_product(
    session: AsyncSession,
    *,
    row: SecondOpinionProduct,
    identity: _ProductIdentity,
    review_input: SecondOpinionInput,
) -> SecondOpinionReview:
    await _validate_persistence_bindings(session, identity)
    if (
        row.operation_id != identity.operation_id
        or row.project_id != identity.project_id
        or row.plan_id != identity.plan_id
        or row.contract_name != SECOND_OPINION_CONTRACT
        or row.input_fingerprint != identity.input_fingerprint
        or row.diff_sha256 != identity.diff_sha256
        or row.review_run_id != identity.revision.run_id
        or row.review_source_generation
        != identity.revision.source_generation
        or row.review_overlay_generation
        != identity.revision.overlay_generation
        or row.review_git_sha != identity.revision.git_sha
        or row.evidence_line_count != len(review_input.evidence.lines)
        or row.input_coverage
        != ("complete" if review_input.complete else "incomplete")
    ):
        raise LLMLifecycleError("second-opinion stored product binding mismatch")

    try:
        canonical = normalize_second_opinion_payload(row.canonical_payload)
    except SecondOpinionSchemaError as exc:
        raise LLMLifecycleError(
            "second-opinion stored product schema is invalid"
        ) from exc
    if canonical != row.canonical_payload:
        raise LLMLifecycleError(
            "second-opinion stored product is not canonical"
        )
    grounded = ground_second_opinion_payload(
        canonical,
        evidence=review_input.evidence,
    )
    if (
        grounded.rejected_findings != 0
        or grounded.canonical_payload != canonical
        or row.finding_count != len(canonical["findings"])
        or row.rejected_findings < 0
    ):
        raise LLMLifecycleError(
            "second-opinion stored product grounding is invalid"
        )

    expected_grounding = (
        "fully_grounded" if row.rejected_findings == 0 else "rejected"
    )
    expected_coverage: Coverage = (
        "complete"
        if review_input.complete and row.rejected_findings == 0
        else "incomplete"
    )
    expected_failure = None
    if row.rejected_findings:
        expected_failure = FAILURE_GROUNDING
    elif not review_input.complete:
        expected_failure = FAILURE_INPUT_BOUNDED
    if (
        row.grounding_status != expected_grounding
        or row.coverage != expected_coverage
        or row.failure_code != expected_failure
    ):
        raise LLMLifecycleError(
            "second-opinion stored product status is inconsistent"
        )

    if row.attempt_id is None:
        if review_input.evidence.lines:
            raise LLMLifecycleError(
                "second-opinion stored product lost its attempt"
            )
    else:
        attempt = await session.get(LLMCall, row.attempt_id)
        if attempt is None:
            raise LLMLifecycleError(
                "second-opinion stored product attempt is missing"
            )
        _validate_attempt_for_product(
            attempt,
            identity=identity,
            attempt_id=row.attempt_id,
            grounded=GroundedSecondOpinion(
                findings=grounded.findings,
                rejected_findings=row.rejected_findings,
                canonical_payload=canonical,
            ),
        )

    return SecondOpinionReview(
        findings=grounded.findings,
        coverage=expected_coverage,
        failure_code=expected_failure,
        operation_id=identity.operation_id,
        input_fingerprint=identity.input_fingerprint,
        attempt_id=row.attempt_id,
        replayed=True,
        canonical_payload=canonical,
    )


async def _lookup_replay_or_attempt(
    *,
    identity: _ProductIdentity,
    review_input: SecondOpinionInput,
) -> _ReplayLookup:
    """Load a product, or recover its exact terminal semantic candidate."""

    terminal_attempt_id: uuid.UUID | None = None
    candidate_exists = False
    async with _accounting_session_factory() as accounting:
        try:
            product = (
                await accounting.execute(
                    select(SecondOpinionProduct).where(
                        SecondOpinionProduct.operation_id
                        == identity.operation_id
                    )
                )
            ).scalar_one_or_none()
            if product is not None:
                review = await _review_from_product(
                    accounting,
                    row=product,
                    identity=identity,
                    review_input=review_input,
                )
                return _ReplayLookup(review=review)

            await _validate_persistence_bindings(accounting, identity)
            attempt = (
                await accounting.execute(
                    select(LLMCall.id, LLMCall.attempt_status).where(
                        LLMCall.operation_id == identity.operation_id,
                        LLMCall.attempt_no == 1,
                    )
                )
            ).one_or_none()
            if attempt is None:
                return _ReplayLookup()
            if attempt.attempt_status == AttemptStatus.STARTED.value:
                return _ReplayLookup(failure_code=FAILURE_IN_PROGRESS)
            terminal_attempt_id = attempt.id
            candidate_exists = bool(
                await accounting.scalar(
                    select(LLMSemanticCandidate.attempt_id).where(
                        LLMSemanticCandidate.attempt_id == attempt.id
                    )
                )
            )
        except LLMLifecycleError:
            return _ReplayLookup(failure_code=FAILURE_PRODUCT_INVALID)
        except Exception as exc:  # noqa: BLE001 - diagnostics stay static
            log.warning(
                "second opinion replay lookup failed: %s",
                type(exc).__name__,
            )
            return _ReplayLookup(failure_code=FAILURE_ACCOUNTING)

    if terminal_attempt_id is None:
        return _ReplayLookup(failure_code=FAILURE_PRODUCT_INVALID)
    if not candidate_exists:
        return _ReplayLookup(
            failure_code=FAILURE_TERMINAL_WITHOUT_PRODUCT
        )
    try:
        async with _accounting_session_factory() as accounting:
            candidate = await load_terminal_semantic_candidate(
                accounting,
                operation_id=identity.operation_id,
                attempt_no=1,
                project_id=identity.project_id,
                purpose=LLMPurpose.SECOND_OPINION,
                input_fingerprint=identity.input_fingerprint,
                contract_name=SECOND_OPINION_CONTRACT,
                binding_fingerprint=identity.binding_fingerprint,
                normalizer=normalize_second_opinion_candidate_payload,
            )
        return _ReplayLookup(candidate=candidate)
    except (
        LLMSemanticCandidateCorrupt,
        LLMSemanticCandidateUnavailable,
        SecondOpinionSchemaError,
    ):
        return _ReplayLookup(failure_code=FAILURE_PRODUCT_INVALID)
    except Exception as exc:  # noqa: BLE001 - diagnostics stay static
        log.warning(
            "second opinion candidate replay lookup failed: %s",
            type(exc).__name__,
        )
        return _ReplayLookup(failure_code=FAILURE_ACCOUNTING)


async def _promote_replayed_candidate(
    *,
    identity: _ProductIdentity,
    review_input: SecondOpinionInput,
    candidate: SemanticCandidateReplay,
) -> SecondOpinionReview:
    canonical = normalize_second_opinion_candidate_payload(
        candidate.payload
    )
    if canonical != candidate.payload:
        raise LLMLifecycleError(
            "second-opinion replay candidate is not canonical"
        )
    grounded = ground_second_opinion_payload(
        canonical,
        evidence=review_input.evidence,
    )
    review = await _persist_product_and_classify(
        identity=identity,
        review_input=review_input,
        grounded=grounded,
        attempt_id=candidate.attempt_id,
        candidate_payload=canonical,
    )
    return replace(review, replayed=True)


def _incomplete(
    *,
    failure_code: str,
    identity: _ProductIdentity | None = None,
    attempt_id: uuid.UUID | None = None,
) -> SecondOpinionReview:
    return SecondOpinionReview(
        coverage="incomplete",
        failure_code=failure_code,
        operation_id=(identity.operation_id if identity else None),
        input_fingerprint=(identity.input_fingerprint if identity else None),
        attempt_id=attempt_id,
    )


async def run(
    session: AsyncSession,  # noqa: ARG001 - graph pass tx stays separate
    *,
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    diff: str,
    prior_findings: list[Finding],
    review_revision: CanonicalReviewRevision | None = None,
) -> SecondOpinionReview:
    """Run or replay one semantically grounded independent review product."""

    if review_revision is None:
        return _incomplete(failure_code=FAILURE_REVISION_UNBOUND)
    review_input = build_second_opinion_input(
        diff=diff,
        prior_findings=prior_findings,
    )
    input_fingerprint = fingerprint_input(
        SECOND_OPINION_SYSTEM_PROMPT,
        review_input.prompt,
    )
    try:
        identity = _identity(
            project_id=project_id,
            plan_id=plan_id,
            diff=diff,
            input_fingerprint=input_fingerprint,
            revision=review_revision,
        )
    except LLMLifecycleError:
        return _incomplete(failure_code=FAILURE_REVISION_UNBOUND)

    replay = await _lookup_replay_or_attempt(
        identity=identity,
        review_input=review_input,
    )
    if replay.review is not None:
        return replay.review
    if replay.candidate is not None:
        try:
            return await _promote_replayed_candidate(
                identity=identity,
                review_input=review_input,
                candidate=replay.candidate,
            )
        except (SecondOpinionSchemaError, LLMLifecycleError):
            return _incomplete(
                failure_code=FAILURE_PRODUCT_INVALID,
                identity=identity,
                attempt_id=replay.candidate.attempt_id,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics stay static
            log.warning(
                "second opinion candidate promotion failed: %s",
                type(exc).__name__,
            )
            return _incomplete(
                failure_code=FAILURE_ACCOUNTING,
                identity=identity,
                attempt_id=replay.candidate.attempt_id,
            )
    if replay.failure_code is not None:
        return _incomplete(
            failure_code=replay.failure_code,
            identity=identity,
        )

    # With no exact changed line, a provider cannot produce a grounded
    # finding. Empty diffs are complete; unsupported/bounded-away forms remain
    # explicit incomplete canonical products. No physical call is needed.
    if not review_input.evidence.lines:
        empty = GroundedSecondOpinion(
            findings=(),
            rejected_findings=0,
            canonical_payload={
                "contract": SECOND_OPINION_CONTRACT,
                "findings": [],
            },
        )
        try:
            return await _persist_product_and_classify(
                identity=identity,
                review_input=review_input,
                grounded=empty,
                attempt_id=None,
                candidate_payload=None,
            )
        except Exception as exc:  # noqa: BLE001 - static failure only
            log.warning(
                "second opinion empty product persistence failed: %s",
                type(exc).__name__,
            )
            return _incomplete(
                failure_code=FAILURE_ACCOUNTING,
                identity=identity,
            )

    scope_id = uuid.uuid5(
        identity.operation_id,
        "mnemos.second_opinion.request_budget.v1",
    )
    run_budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=SECOND_OPINION_MAX_INPUT_TOKENS,
        max_output_tokens=SECOND_OPINION_OUTPUT_TOKENS,
        wall_time_sec=SECOND_OPINION_WALL_TIME_SEC,
        scope_id=scope_id,
    )

    async def _start_attempt(
        metadata: AttemptStartMetadata,
    ) -> AttemptTicket:
        if (
            metadata.attempt_no != 1
            or metadata.attempt_kind != AttemptKind.PRIMARY
            or metadata.parent_attempt_id is not None
            or metadata.provider != "anthropic"
            or metadata.provider_mode != "api"
            or metadata.operation_id != identity.operation_id
            or metadata.requested_model != SECOND_OPINION_MODEL
            or metadata.billing_route != "anthropic_messages_api"
            or metadata.input_fingerprint != identity.input_fingerprint
            or metadata.requested_max_output_tokens
            != SECOND_OPINION_OUTPUT_TOKENS
            or metadata.usage_source != UsageSource.PROVIDER_API
        ):
            raise LLMLifecycleError(
                "second-opinion provider metadata contract mismatch"
            )
        async with _accounting_session_factory() as accounting:
            await require_budget(accounting, project_id)
            return await begin_attempt(
                accounting,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SECOND_OPINION,
                target_id=f"review:{identity.operation_id.hex}",
                level=0,
                run_budget=run_budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=metadata,
                require_atomic_dollar_reservation=True,
            )

    async def _finish_attempt(
        ticket: AttemptTicket,
        outcome: AttemptOutcome,
    ) -> None:
        async with _accounting_session_factory() as accounting:
            await finalize_attempt(accounting, ticket=ticket, outcome=outcome)

    async def _finish_candidate(
        ticket: AttemptTicket,
        outcome: AttemptOutcome,
        candidate: SemanticCandidate,
    ) -> None:
        canonical = normalize_second_opinion_candidate_payload(
            dict(candidate.payload)
        )
        if (
            candidate.contract_name != SECOND_OPINION_CONTRACT
            or candidate.binding_fingerprint
            != identity.binding_fingerprint
            or canonical != candidate.payload
        ):
            raise LLMLifecycleError(
                "second-opinion semantic candidate contract mismatch"
            )
        async with _accounting_session_factory() as accounting:
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
            request.operation_id != identity.operation_id
            or request.attempt_no != 1
            or request.input_fingerprint != identity.input_fingerprint
            or request.contract_name != SECOND_OPINION_CONTRACT
            or request.binding_fingerprint
            != identity.binding_fingerprint
        ):
            raise LLMLifecycleError(
                "second-opinion semantic replay binding mismatch"
            )
        async with _accounting_session_factory() as accounting:
            return await load_terminal_semantic_candidate(
                accounting,
                operation_id=request.operation_id,
                attempt_no=request.attempt_no,
                project_id=identity.project_id,
                purpose=LLMPurpose.SECOND_OPINION,
                input_fingerprint=request.input_fingerprint,
                contract_name=request.contract_name,
                binding_fingerprint=request.binding_fingerprint,
                normalizer=normalize_second_opinion_candidate_payload,
            )

    callbacks = AttemptCallbacks(
        start=_start_attempt,
        finish=_finish_attempt,
        finish_candidate=_finish_candidate,
        replay_candidate=_replay_candidate,
        mark_provider_dispatch=lambda: None,
    )
    try:
        async with use_attempt_callbacks(callbacks):
            provider_result = await review_via_anthropic(
                prompt=review_input.prompt,
                model=SECOND_OPINION_MODEL,
                operation_id=identity.operation_id,
                binding_fingerprint=identity.binding_fingerprint,
            )
    except LLMAttemptReplayBlocked:
        # A concurrent caller may have completed and persisted while this
        # caller waited for the begin-attempt lock. Recheck once; never fall
        # through to a second dispatch.
        replay = await _lookup_replay_or_attempt(
            identity=identity,
            review_input=review_input,
        )
        if replay.review is not None:
            return replay.review
        if replay.candidate is not None:
            try:
                return await _promote_replayed_candidate(
                    identity=identity,
                    review_input=review_input,
                    candidate=replay.candidate,
                )
            except (SecondOpinionSchemaError, LLMLifecycleError):
                return _incomplete(
                    failure_code=FAILURE_PRODUCT_INVALID,
                    identity=identity,
                    attempt_id=replay.candidate.attempt_id,
                )
            except Exception as exc:  # noqa: BLE001 - diagnostics stay static
                log.warning(
                    "second opinion blocked replay promotion failed: %s",
                    type(exc).__name__,
                )
                return _incomplete(
                    failure_code=FAILURE_ACCOUNTING,
                    identity=identity,
                    attempt_id=replay.candidate.attempt_id,
                )
        return _incomplete(
            failure_code=(replay.failure_code or FAILURE_REPLAY),
            identity=identity,
        )
    except (BudgetExceeded, RunBudgetExceeded):
        return _incomplete(failure_code=FAILURE_BUDGET, identity=identity)
    except LLMLifecycleError:
        return _incomplete(
            failure_code=FAILURE_ACCOUNTING,
            identity=identity,
        )
    except Exception as exc:  # noqa: BLE001 - never retain exception text
        log.warning("second opinion failed: %s", type(exc).__name__)
        return _incomplete(
            failure_code=FAILURE_PROVIDER,
            identity=identity,
        )

    if provider_result.payload is None:
        return _incomplete(
            failure_code=(provider_result.failure_code or FAILURE_PROVIDER),
            identity=identity,
            attempt_id=provider_result.attempt_id,
        )
    if provider_result.attempt_id is None:
        return _incomplete(
            failure_code=FAILURE_ACCOUNTING,
            identity=identity,
        )

    try:
        grounded = ground_second_opinion_payload(
            provider_result.payload,
            evidence=review_input.evidence,
        )
        # The locked exact candidate, its classification, and the grounded
        # product commit together; a pre-commit crash remains replayable.
        review = await _persist_product_and_classify(
            identity=identity,
            review_input=review_input,
            grounded=grounded,
            attempt_id=provider_result.attempt_id,
            candidate_payload=provider_result.payload,
        )
        return replace(review, replayed=provider_result.replayed)
    except (SecondOpinionSchemaError, LLMLifecycleError):
        return _incomplete(
            failure_code=FAILURE_ACCOUNTING,
            identity=identity,
            attempt_id=provider_result.attempt_id,
        )
    except Exception as exc:  # noqa: BLE001 - never retain exception text
        log.warning(
            "second opinion canonical product commit failed: %s",
            type(exc).__name__,
        )
        return _incomplete(
            failure_code=FAILURE_ACCOUNTING,
            identity=identity,
            attempt_id=provider_result.attempt_id,
        )


__all__ = [
    "SecondOpinionInput",
    "SecondOpinionReview",
    "build_second_opinion_input",
    "run",
]
