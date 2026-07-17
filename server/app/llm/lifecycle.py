"""Crash-durable lifecycle for one physical LLM provider attempt.

Prompts, source text, raw provider envelopes, and exceptions never enter the
database through this module.  A provider adapter passes bounded routing and
usage facts.  It may also attach one contract-normalized semantic candidate;
that canonical JSON is encrypted before it is committed atomically with the
terminal attempt.  Consumer-specific grounding remains a later boundary.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.cost import (
    LLMRunBudget,
    ProjectDollarBudgetConfig,
    RunBudgetExceeded,
    canonical_price_attestation_failure_predicate,
    configured_project_dollar_budget,
)
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMPhysicalAttemptV1,
    LLMPurpose,
    LLMUsageV1,
    LLMUsageContractError,
    ResultStatus,
    UsageStatus,
    UsageSource,
    unavailable_usage,
)
from app.llm.pricing import (
    PriceContractError,
    PriceReservation,
    resolve_price_contract,
    resolve_price_contract_by_id,
)
from app.models.findings import (
    LLMCall,
    LLMBudgetScope,
    LLMProjectBudgetAccount,
)
from app.models.llm import (
    LLMSemanticCandidate,
    MAX_SEMANTIC_CANDIDATE_BYTES,
    MAX_SEMANTIC_CANDIDATE_CIPHERTEXT_BYTES,
)
from app.safety.crypto import decrypt, encrypt


BUDGET_POLICY_VERSION = "mnemos.llm_budget.v1"
PRICE_ATTESTATION_MODEL_MISMATCH = "price_attestation_model_mismatch"
PRICE_ATTESTATION_INPUT_BREACH = "price_attestation_input_ceiling_breach"
PRICE_ATTESTATION_OUTPUT_BREACH = "price_attestation_output_ceiling_breach"
PRICE_ATTESTATION_COST_BREACH = "price_attestation_cost_ceiling_breach"
PRICE_ATTESTATION_TOOL_BREACH = "price_attestation_tool_input_breach"

_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CANDIDATE_CONTRACT = re.compile(r"[a-z0-9][a-z0-9._-]{2,95}\Z")
_CANDIDATE_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_CANDIDATE_JSON_DEPTH = 32
_MAX_CANDIDATE_JSON_VALUES = 100_000


class BudgetScopeKind(StrEnum):
    ANALYSIS_RUN = "analysis_run"
    REQUEST = "request"


class LLMLifecycleError(RuntimeError):
    """A durable attempt cannot be started or reconciled safely."""


class LLMAttemptConflict(LLMLifecycleError):
    """A duplicate dispatch or conflicting terminal replay was requested."""


class LLMAttemptReplayBlocked(LLMAttemptConflict):
    """This stable operation already owns an attempt and cannot redispatch."""


class LLMPriceContractBreach(LLMLifecycleError):
    """A provider response exceeded or drifted from its reserved contract."""


class LLMSemanticCandidateError(LLMLifecycleError):
    """A semantic candidate cannot be stored or replayed safely."""


class LLMSemanticCandidateUnavailable(LLMSemanticCandidateError):
    """The stable terminal attempt has no matching replay candidate."""


class LLMSemanticCandidateCorrupt(LLMSemanticCandidateError):
    """Encrypted candidate integrity or contract validation failed."""


def _validate_candidate_json_tree(value: Any) -> None:
    """Reject non-JSON, non-finite, excessively deep, or huge object trees.

    Diagnostics intentionally describe only the violated invariant.  Object
    keys and values are model-controlled and may contain source or secrets.
    """

    seen_values = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        seen_values += 1
        if seen_values > _MAX_CANDIDATE_JSON_VALUES:
            raise LLMSemanticCandidateError(
                "semantic candidate has too many JSON values"
            )
        if depth > _MAX_CANDIDATE_JSON_DEPTH:
            raise LLMSemanticCandidateError(
                "semantic candidate exceeds the JSON depth limit"
            )
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise LLMSemanticCandidateError(
                    "semantic candidate contains a non-finite number"
                )
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, Mapping):
            if any(not isinstance(key, str) for key in current):
                raise LLMSemanticCandidateError(
                    "semantic candidate object keys must be strings"
                )
            stack.extend((item, depth + 1) for item in current.values())
            continue
        raise LLMSemanticCandidateError(
            "semantic candidate contains a non-JSON value"
        )


def _canonical_candidate_bytes(
    payload: Mapping[str, Any],
    *,
    contract_name: str,
) -> bytes:
    if not isinstance(payload, Mapping):
        raise LLMSemanticCandidateError(
            "semantic candidate payload must be a JSON object"
        )
    if payload.get("contract") != contract_name:
        raise LLMSemanticCandidateError(
            "semantic candidate payload contract does not match"
        )
    _validate_candidate_json_tree(payload)
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise LLMSemanticCandidateError(
            "semantic candidate cannot be canonicalized"
        ) from None
    if not 2 <= len(encoded) <= MAX_SEMANTIC_CANDIDATE_BYTES:
        raise LLMSemanticCandidateError(
            "semantic candidate exceeds the canonical byte limit"
        )
    return encoded


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    """Bounded canonical candidate supplied only after strict normalization.

    ``binding_fingerprint`` hashes consumer-owned revision/evidence identity;
    it is independent from the exact provider-input fingerprint on LLMCall.
    The payload is excluded from ``repr`` and snapshotted to canonical bytes
    during construction so later mutation of a caller-owned dict cannot alter
    what the terminal transaction persists.
    """

    contract_name: str
    binding_fingerprint: str
    payload: Mapping[str, Any] = field(repr=False)
    _canonical_payload: bytes = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.contract_name, str)
            or _CANDIDATE_CONTRACT.fullmatch(self.contract_name) is None
        ):
            raise LLMSemanticCandidateError(
                "semantic candidate contract name is invalid"
            )
        if (
            not isinstance(self.binding_fingerprint, str)
            or _LOWER_SHA256.fullmatch(self.binding_fingerprint) is None
        ):
            raise LLMSemanticCandidateError(
                "semantic candidate binding fingerprint is invalid"
            )
        canonical = _canonical_candidate_bytes(
            self.payload,
            contract_name=self.contract_name,
        )
        # A JSON round-trip strips Mapping subclasses and snapshots a private
        # tree without retaining the caller's potentially mutable object.
        snapshot = json.loads(canonical)
        object.__setattr__(self, "payload", snapshot)
        object.__setattr__(self, "_canonical_payload", canonical)


CandidateNormalizer = Callable[
    [Mapping[str, Any]], Mapping[str, Any]
]


@dataclass(frozen=True, slots=True)
class SemanticCandidateReplay:
    """A decrypted terminal candidate after identity and contract checks."""

    attempt_id: uuid.UUID
    operation_id: uuid.UUID
    attempt_no: int
    project_id: uuid.UUID
    purpose: LLMPurpose
    input_fingerprint: str
    contract_name: str
    binding_fingerprint: str
    candidate_status: str
    attempt_status: AttemptStatus
    result_status: ResultStatus
    requested_model: str
    resolved_model: str | None
    total_tokens: int | None
    usage_status: UsageStatus
    payload: dict[str, Any] = field(repr=False)


class AttemptReplayState(StrEnum):
    """Database state for one stable physical-attempt identity."""

    ABSENT = "absent"
    STARTED = "started"
    TERMINAL_WITHOUT_CANDIDATE = "terminal_without_candidate"
    TERMINAL_WITH_CANDIDATE = "terminal_with_candidate"


@dataclass(frozen=True, slots=True)
class AttemptReplayResult:
    """Safe static attempt facts plus an optional validated candidate.

    Provider input/output, request ids, targets, and raw failure text are
    intentionally absent.  ``payload`` is present only after every stored
    identity, digest, encryption, and consumer-contract check succeeds.
    """

    state: AttemptReplayState
    operation_id: uuid.UUID
    attempt_no: int
    project_id: uuid.UUID
    purpose: LLMPurpose
    attempt_id: uuid.UUID | None = None
    input_fingerprint: str | None = None
    attempt_status: AttemptStatus | None = None
    result_status: ResultStatus | None = None
    failure_code: str | None = None
    provider: str | None = None
    provider_mode: str | None = None
    requested_model: str | None = None
    resolved_model: str | None = None
    requested_max_output_tokens: int | None = None
    usage: LLMUsageV1 | None = None
    contract_name: str | None = None
    binding_fingerprint: str | None = None
    candidate_status: str | None = None
    payload: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def total_tokens(self) -> int | None:
        """Compatibility projection without treating unknown usage as zero."""

        return self.usage.total_tokens if self.usage is not None else None

    @property
    def usage_status(self) -> UsageStatus | None:
        return self.usage.status if self.usage is not None else None


@dataclass(frozen=True, slots=True)
class SemanticCandidateReplayRequest:
    """Consumer-independent exact identity for one candidate replay.

    Project and purpose stay owned by the callback closure so an adapter
    cannot widen its authorization to another product surface.
    """

    operation_id: uuid.UUID
    attempt_no: int
    input_fingerprint: str
    contract_name: str
    binding_fingerprint: str
    normalizer: CandidateNormalizer = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_id, uuid.UUID)
            or isinstance(self.attempt_no, bool)
            or not isinstance(self.attempt_no, int)
            or self.attempt_no <= 0
            or not isinstance(self.input_fingerprint, str)
            or _LOWER_SHA256.fullmatch(self.input_fingerprint) is None
            or not isinstance(self.contract_name, str)
            or _CANDIDATE_CONTRACT.fullmatch(self.contract_name) is None
            or not isinstance(self.binding_fingerprint, str)
            or _LOWER_SHA256.fullmatch(self.binding_fingerprint) is None
            or not callable(self.normalizer)
        ):
            raise LLMSemanticCandidateError(
                "semantic candidate replay request is invalid"
            )



@dataclass(frozen=True, slots=True)
class AttemptStartMetadata:
    """Provider-side facts known immediately before physical dispatch."""

    operation_id: uuid.UUID
    attempt_no: int
    attempt_kind: AttemptKind
    provider: str
    provider_mode: str
    requested_model: str
    input_fingerprint: str
    estimated_input_tokens: int
    input_estimate_method: str
    requested_max_output_tokens: int
    usage_source: UsageSource
    billing_route: str | None = None
    parent_attempt_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class AttemptTicket:
    """Safe identity returned only after the start transaction commits."""

    attempt_id: uuid.UUID
    operation_id: uuid.UUID
    budget_scope_id: uuid.UUID
    started_at: datetime
    remaining_seconds: float


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """Canonical transport and schema outcome for one started attempt."""

    attempt_status: AttemptStatus
    result_status: ResultStatus
    usage: LLMUsageV1
    resolved_model: str | None = None
    provider_request_id: str | None = None
    failure_code: str | None = None
    finish_reason: str | None = None
    provider_mode: str | None = None


@dataclass(frozen=True, slots=True)
class PreReservedAttempt:
    """One caller-level reservation already present in ``LLMRunBudget``."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class _BudgetConflictSnapshot:
    max_calls: int
    max_input_tokens: int
    max_output_tokens: int
    wall_time_sec: int
    started_monotonic: float
    calls_started: int
    input_tokens_reserved: int
    output_tokens_reserved: int
    exhausted_reason: str | None
    durable_scope_id: uuid.UUID | None
    durable_calls_synced: int
    durable_input_tokens_synced: int
    durable_output_tokens_synced: int


AttemptStarter = Callable[[AttemptStartMetadata], Awaitable[AttemptTicket]]
AttemptFinalizer = Callable[
    [AttemptTicket, AttemptOutcome], Awaitable[None]
]
SemanticCandidateFinalizer = Callable[
    [AttemptTicket, AttemptOutcome, SemanticCandidate], Awaitable[None]
]
SemanticCandidateReplayer = Callable[
    [SemanticCandidateReplayRequest], Awaitable[SemanticCandidateReplay]
]
AttemptStateReplayer = Callable[
    [SemanticCandidateReplayRequest], Awaitable[AttemptReplayResult]
]
OpaqueBudgetReserver = Callable[[], Awaitable[float]]
ProviderDispatchMarker = Callable[[], None]


@dataclass(frozen=True, slots=True)
class AttemptCallbacks:
    """Task-local bridge from a provider adapter to its owning consumer."""

    start: AttemptStarter
    finish: AttemptFinalizer
    finish_candidate: SemanticCandidateFinalizer | None = None
    replay_candidate: SemanticCandidateReplayer | None = None
    replay_attempt: AttemptStateReplayer | None = None
    reserve_opaque: OpaqueBudgetReserver | None = None
    mark_provider_dispatch: ProviderDispatchMarker | None = None


_ATTEMPT_CALLBACKS: contextvars.ContextVar[AttemptCallbacks | None] = (
    contextvars.ContextVar("mnemos_llm_attempt_callbacks", default=None)
)


def current_attempt_callbacks() -> AttemptCallbacks | None:
    return _ATTEMPT_CALLBACKS.get()


def require_attempt_callbacks(consumer: str) -> AttemptCallbacks:
    """Return the task-owned durable accounting capability or fail closed.

    Provider adapters are intentionally callable from more than one product
    surface.  A default of ``None`` would therefore be an accounting bypass:
    a new caller could reach the network without first committing a physical
    attempt.  Consumers install :class:`AttemptCallbacks` only for the
    duration of an owned request/run; absence never authorizes dispatch.
    """

    callbacks = current_attempt_callbacks()
    if callbacks is None:
        label = consumer.strip()[:80] or "LLM provider"
        raise LLMLifecycleError(
            f"{label} requires a durable attempt accounting capability"
        )
    return callbacks


@asynccontextmanager
async def use_attempt_callbacks(
    callbacks: AttemptCallbacks,
) -> AsyncIterator[None]:
    """Install callbacks for one task without mutating a shared Extractor."""

    token = _ATTEMPT_CALLBACKS.set(callbacks)
    try:
        yield
    finally:
        _ATTEMPT_CALLBACKS.reset(token)


def fingerprint_input(*parts: str) -> str:
    """Hash exact input parts with length framing; retain none of their text."""

    digest = hashlib.sha256()
    for part in parts:
        if not isinstance(part, str):
            raise TypeError("LLM input fingerprint parts must be strings")
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _encrypted_candidate_values(
    row: LLMCall,
    candidate: SemanticCandidate,
) -> dict[str, Any]:
    """Encrypt one snapshotted candidate and derive safe ledger metadata."""

    operation_id = row.operation_id
    attempt_no = row.attempt_no
    purpose = row.purpose
    input_fingerprint = row.input_fingerprint
    if (
        operation_id is None
        or attempt_no is None
        or attempt_no <= 0
        or purpose is None
        or input_fingerprint is None
        or _LOWER_SHA256.fullmatch(input_fingerprint) is None
    ):
        raise LLMSemanticCandidateError(
            "semantic candidate attempt binding is invalid"
        )
    try:
        plaintext = candidate._canonical_payload.decode("utf-8")
        ciphertext, iv = encrypt(plaintext)
    except Exception:
        # KMS exceptions can include backend paths or provider configuration.
        raise LLMSemanticCandidateError(
            "semantic candidate encryption failed"
        ) from None
    if (
        not isinstance(ciphertext, bytes)
        or not 1 <= len(ciphertext)
        <= MAX_SEMANTIC_CANDIDATE_CIPHERTEXT_BYTES
        or not isinstance(iv, bytes)
        or len(iv) != 16
    ):
        raise LLMSemanticCandidateError(
            "semantic candidate encryption envelope is invalid"
        )
    canonical = candidate._canonical_payload
    return {
        "attempt_id": row.id,
        "operation_id": operation_id,
        "attempt_no": attempt_no,
        "project_id": row.project_id,
        "purpose": purpose,
        "contract_name": candidate.contract_name,
        "input_fingerprint": input_fingerprint,
        "binding_fingerprint": candidate.binding_fingerprint,
        "payload_sha256": hashlib.sha256(canonical).hexdigest(),
        "payload_bytes": len(canonical),
        "payload_ciphertext": ciphertext,
        "payload_iv": iv,
        "candidate_status": "candidate",
        "failure_code": None,
    }


def _candidate_metadata_matches(
    stored: LLMSemanticCandidate,
    *,
    row: LLMCall,
    candidate: SemanticCandidate,
) -> bool:
    canonical = candidate._canonical_payload
    return bool(
        stored.attempt_id == row.id
        and stored.operation_id == row.operation_id
        and stored.attempt_no == row.attempt_no
        and stored.project_id == row.project_id
        and stored.purpose == row.purpose
        and stored.contract_name == candidate.contract_name
        and stored.input_fingerprint == row.input_fingerprint
        and stored.binding_fingerprint == candidate.binding_fingerprint
        and stored.payload_sha256 == hashlib.sha256(canonical).hexdigest()
        and stored.payload_bytes == len(canonical)
        and stored.candidate_status == "candidate"
        and stored.failure_code is None
    )


def _validate_stored_candidate_identity(
    stored: LLMSemanticCandidate,
    row: LLMCall,
) -> None:
    if (
        stored.attempt_id != row.id
        or stored.operation_id != row.operation_id
        or stored.attempt_no != row.attempt_no
        or stored.project_id != row.project_id
        or stored.purpose != row.purpose
        or stored.input_fingerprint != row.input_fingerprint
        or stored.candidate_status not in {"candidate", "accepted", "rejected"}
        or (
            stored.candidate_status in {"candidate", "accepted"}
            and stored.failure_code is not None
        )
        or (
            stored.candidate_status == "rejected"
            and not stored.failure_code
        )
    ):
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate identity binding is corrupt"
        )


def _assert_candidate_classification_consistent(
    stored: LLMSemanticCandidate,
    row: LLMCall,
) -> None:
    """Verify the candidate projection matches its owning attempt exactly."""

    if row.attempt_status != AttemptStatus.COMPLETED.value:
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate transport status is inconsistent"
        )
    try:
        result_status = ResultStatus(cast(str, row.result_status))
    except (TypeError, ValueError):
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate result status is inconsistent"
        ) from None

    if result_status == ResultStatus.PENDING:
        expected_status = "candidate"
        expected_failure = None
        if row.failure_code is not None:
            raise LLMSemanticCandidateCorrupt(
                "semantic candidate pending result is inconsistent"
            )
    elif result_status == ResultStatus.ACCEPTED:
        expected_status = "accepted"
        expected_failure = None
        if row.failure_code is not None:
            raise LLMSemanticCandidateCorrupt(
                "semantic candidate accepted result is inconsistent"
            )
    elif result_status == ResultStatus.NOT_APPLICABLE:
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate cannot have a non-semantic result"
        )
    else:
        expected_status = "rejected"
        expected_failure = row.failure_code
        if (
            not isinstance(expected_failure, str)
            or _CANDIDATE_FAILURE_CODE.fullmatch(expected_failure) is None
        ):
            raise LLMSemanticCandidateCorrupt(
                "semantic candidate rejection code is inconsistent"
            )

    if (
        stored.candidate_status != expected_status
        or stored.failure_code != expected_failure
    ):
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate classification is inconsistent"
        )


def _candidate_classification_target(
    *,
    result_status: ResultStatus,
    failure_code: str | None,
) -> tuple[str, str | None]:
    if result_status == ResultStatus.ACCEPTED:
        if failure_code is not None:
            raise LLMAttemptConflict(
                "accepted semantic candidate cannot have a failure code"
            )
        return "accepted", None
    if (
        not isinstance(failure_code, str)
        or _CANDIDATE_FAILURE_CODE.fullmatch(failure_code) is None
    ):
        raise LLMAttemptConflict(
            "semantic candidate rejection requires a static failure code"
        )
    return "rejected", failure_code


def _decrypt_and_validate_candidate(
    stored: LLMSemanticCandidate,
    *,
    normalizer: CandidateNormalizer,
) -> dict[str, Any]:
    if (
        not isinstance(stored.contract_name, str)
        or _CANDIDATE_CONTRACT.fullmatch(stored.contract_name) is None
        or not isinstance(stored.binding_fingerprint, str)
        or _LOWER_SHA256.fullmatch(stored.binding_fingerprint) is None
        or not isinstance(stored.payload_sha256, str)
        or _LOWER_SHA256.fullmatch(stored.payload_sha256) is None
        or isinstance(stored.payload_bytes, bool)
        or not isinstance(stored.payload_bytes, int)
        or not 2 <= stored.payload_bytes <= MAX_SEMANTIC_CANDIDATE_BYTES
        or not isinstance(stored.payload_ciphertext, bytes)
        or not 1 <= len(stored.payload_ciphertext)
        <= MAX_SEMANTIC_CANDIDATE_CIPHERTEXT_BYTES
        or not isinstance(stored.payload_iv, bytes)
        or len(stored.payload_iv) != 16
    ):
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate storage envelope is corrupt"
        )
    try:
        plaintext = decrypt(stored.payload_ciphertext, stored.payload_iv)
        encoded = plaintext.encode("utf-8")
    except Exception:
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate decryption failed"
        ) from None
    if (
        len(encoded) != stored.payload_bytes
        or hashlib.sha256(encoded).hexdigest() != stored.payload_sha256
    ):
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate digest verification failed"
        )
    try:
        decoded = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate canonical JSON is invalid"
        ) from None
    if not isinstance(decoded, dict):
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate payload is not an object"
        )
    try:
        canonical = _canonical_candidate_bytes(
            decoded,
            contract_name=stored.contract_name,
        )
    except LLMSemanticCandidateError:
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate canonical JSON is invalid"
        ) from None
    if canonical != encoded:
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate JSON is not canonical"
        )
    try:
        normalized = normalizer(decoded)
        normalized_bytes = _canonical_candidate_bytes(
            normalized,
            contract_name=stored.contract_name,
        )
    except Exception:
        # A contract validator may include model text in its exception.
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate contract validation failed"
        ) from None
    if normalized_bytes != encoded:
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate contract is not canonical"
        )
    # Return a fresh JSON tree; neither the validator nor the ORM row owns it.
    replay = json.loads(encoded)
    if not isinstance(replay, dict):  # pragma: no cover - checked above
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate payload is not an object"
        )
    return replay


def _usage_from_replay_row(row: LLMCall) -> LLMUsageV1:
    """Rebuild and validate the provider-neutral usage projection."""

    try:
        return LLMUsageV1(
            status=UsageStatus(cast(str, row.usage_status)),
            source=UsageSource(cast(str, row.usage_source)),
            input_tokens=row.input_tokens,
            uncached_input_tokens=row.uncached_input_tokens,
            output_tokens=row.output_tokens,
            visible_output_tokens=row.visible_output_tokens,
            total_tokens=row.total_tokens,
            cache_read_input_tokens=row.cache_read_input_tokens,
            cache_write_input_tokens=row.cache_write_input_tokens,
            reasoning_output_tokens=row.reasoning_output_tokens,
            tool_input_tokens=row.tool_input_tokens,
            provider_total_tokens=row.provider_total_tokens,
            provider_turn_count=row.provider_turn_count,
            reported_cost_usd=row.reported_cost_usd,
            estimated_cost_usd=row.estimated_cost_usd,
            pricing_version=row.pricing_version,
        )
    except (LLMUsageContractError, TypeError, ValueError):
        raise LLMSemanticCandidateCorrupt(
            "attempt replay usage metadata is corrupt"
        ) from None


def _canonical_replay_attempt(row: LLMCall) -> LLMPhysicalAttemptV1:
    """Validate a v1 ledger row without exposing its content-bearing fields."""

    try:
        started_at = _aware_utc(cast(datetime, row.started_at))
        completed_at = (
            _aware_utc(row.completed_at)
            if row.completed_at is not None
            else None
        )
        attempt = LLMPhysicalAttemptV1(
            attempt_id=row.id,
            operation_id=cast(uuid.UUID, row.operation_id),
            budget_scope_id=cast(uuid.UUID, row.budget_scope_id),
            project_id=row.project_id,
            analysis_run_id=row.analysis_run_id,
            purpose=LLMPurpose(cast(str, row.purpose)),
            target_id=row.target_id,
            attempt_no=cast(int, row.attempt_no),
            parent_attempt_id=row.parent_attempt_id,
            attempt_kind=AttemptKind(cast(str, row.attempt_kind)),
            provider=cast(str, row.provider),
            provider_mode=cast(str, row.provider_mode),
            requested_model=cast(str, row.requested_model),
            resolved_model=row.resolved_model,
            provider_request_id=row.provider_request_id,
            input_fingerprint=cast(str, row.input_fingerprint),
            estimated_input_tokens=cast(int, row.estimated_input_tokens),
            input_estimate_method=cast(str, row.input_estimate_method),
            requested_max_output_tokens=cast(
                int, row.requested_max_output_tokens
            ),
            attempt_status=AttemptStatus(cast(str, row.attempt_status)),
            result_status=ResultStatus(cast(str, row.result_status)),
            failure_code=row.failure_code,
            finish_reason=row.finish_reason,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=row.duration_ms,
            usage=_usage_from_replay_row(row),
        )
    except LLMSemanticCandidateCorrupt:
        raise
    except (LLMUsageContractError, AttributeError, TypeError, ValueError):
        raise LLMSemanticCandidateCorrupt(
            "attempt replay static metadata is corrupt"
        ) from None
    if attempt.failure_code is not None and (
        _CANDIDATE_FAILURE_CODE.fullmatch(attempt.failure_code) is None
    ):
        raise LLMSemanticCandidateCorrupt(
            "attempt replay failure code is corrupt"
        )
    if row.tokens_used != attempt.usage.total_tokens or row.model_used != (
        attempt.resolved_model or attempt.requested_model
    ):
        raise LLMSemanticCandidateCorrupt(
            "attempt replay metadata is inconsistent"
        )
    return attempt


def _validate_attempt_replay_binding(
    *,
    operation_id: uuid.UUID,
    attempt_no: int,
    project_id: uuid.UUID,
    purpose: LLMPurpose,
    input_fingerprint: str | None,
    contract_name: str,
    binding_fingerprint: str,
    normalizer: CandidateNormalizer,
) -> None:
    if (
        not isinstance(operation_id, uuid.UUID)
        or not isinstance(project_id, uuid.UUID)
        or isinstance(attempt_no, bool)
        or not isinstance(attempt_no, int)
        or attempt_no <= 0
        or not isinstance(purpose, LLMPurpose)
        or (
            input_fingerprint is not None
            and (
                not isinstance(input_fingerprint, str)
                or _LOWER_SHA256.fullmatch(input_fingerprint) is None
            )
        )
        or not isinstance(contract_name, str)
        or _CANDIDATE_CONTRACT.fullmatch(contract_name) is None
        or not isinstance(binding_fingerprint, str)
        or _LOWER_SHA256.fullmatch(binding_fingerprint) is None
        or not callable(normalizer)
    ):
        raise LLMSemanticCandidateError(
            "attempt replay binding is invalid"
        )


def _attempt_replay_result(
    *,
    state: AttemptReplayState,
    attempt: LLMPhysicalAttemptV1,
    contract_name: str | None = None,
    binding_fingerprint: str | None = None,
    candidate_status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AttemptReplayResult:
    return AttemptReplayResult(
        state=state,
        operation_id=attempt.operation_id,
        attempt_no=attempt.attempt_no,
        project_id=attempt.project_id,
        purpose=attempt.purpose,
        attempt_id=attempt.attempt_id,
        input_fingerprint=attempt.input_fingerprint,
        attempt_status=attempt.attempt_status,
        result_status=attempt.result_status,
        failure_code=attempt.failure_code,
        provider=attempt.provider,
        provider_mode=attempt.provider_mode,
        requested_model=attempt.requested_model,
        resolved_model=attempt.resolved_model,
        requested_max_output_tokens=attempt.requested_max_output_tokens,
        usage=attempt.usage,
        contract_name=contract_name,
        binding_fingerprint=binding_fingerprint,
        candidate_status=candidate_status,
        payload=payload,
    )


async def load_attempt_replay(
    session: AsyncSession,
    *,
    operation_id: uuid.UUID,
    attempt_no: int,
    project_id: uuid.UUID,
    purpose: LLMPurpose,
    input_fingerprint: str | None = None,
    contract_name: str,
    binding_fingerprint: str,
    normalizer: CandidateNormalizer,
) -> AttemptReplayResult:
    """Load one stable attempt and optional candidate in one DB snapshot.

    A provider-neutral consumer may omit ``input_fingerprint``.  Project,
    purpose, operation, candidate contract, and candidate binding remain
    mandatory and fail closed.  The one OUTER JOIN statement prevents a
    terminal row and candidate from being observed at different snapshots.
    """

    _require_clean_accounting_session(session)
    if session.in_transaction():
        raise LLMSemanticCandidateError(
            "attempt replay requires a fresh session"
        )
    _validate_attempt_replay_binding(
        operation_id=operation_id,
        attempt_no=attempt_no,
        project_id=project_id,
        purpose=purpose,
        input_fingerprint=input_fingerprint,
        contract_name=contract_name,
        binding_fingerprint=binding_fingerprint,
        normalizer=normalizer,
    )
    try:
        pair = (
            await session.execute(
                select(LLMCall, LLMSemanticCandidate)
                .select_from(LLMCall)
                .outerjoin(
                    LLMSemanticCandidate,
                    LLMSemanticCandidate.attempt_id == LLMCall.id,
                )
                .where(
                    LLMCall.operation_id == operation_id,
                    LLMCall.attempt_no == attempt_no,
                )
            )
        ).one_or_none()
        if pair is None:
            return AttemptReplayResult(
                state=AttemptReplayState.ABSENT,
                operation_id=operation_id,
                attempt_no=attempt_no,
                project_id=project_id,
                purpose=purpose,
            )
        row, stored = pair
        if row.contract_version != 1:
            raise LLMSemanticCandidateUnavailable(
                "attempt replay row is unavailable"
            )
        if (
            row.project_id != project_id
            or row.purpose != purpose.value
            or (
                input_fingerprint is not None
                and row.input_fingerprint != input_fingerprint
            )
        ):
            raise LLMSemanticCandidateUnavailable(
                "attempt replay binding does not match"
            )
        attempt = _canonical_replay_attempt(row)
        if (
            attempt.operation_id != operation_id
            or attempt.attempt_no != attempt_no
        ):
            raise LLMSemanticCandidateCorrupt(
                "attempt replay identity metadata is corrupt"
            )
        if attempt.attempt_status == AttemptStatus.STARTED:
            if stored is not None:
                raise LLMSemanticCandidateCorrupt(
                    "started attempt unexpectedly owns a semantic candidate"
                )
            return _attempt_replay_result(
                state=AttemptReplayState.STARTED,
                attempt=attempt,
            )
        if stored is None:
            return _attempt_replay_result(
                state=AttemptReplayState.TERMINAL_WITHOUT_CANDIDATE,
                attempt=attempt,
            )
        _validate_stored_candidate_identity(stored, row)
        _assert_candidate_classification_consistent(stored, row)
        if (
            stored.contract_name != contract_name
            or stored.binding_fingerprint != binding_fingerprint
        ):
            raise LLMSemanticCandidateUnavailable(
                "semantic candidate contract binding does not match"
            )
        payload = _decrypt_and_validate_candidate(
            stored,
            normalizer=normalizer,
        )
        return _attempt_replay_result(
            state=AttemptReplayState.TERMINAL_WITH_CANDIDATE,
            attempt=attempt,
            contract_name=stored.contract_name,
            binding_fingerprint=stored.binding_fingerprint,
            candidate_status=stored.candidate_status,
            payload=payload,
        )
    finally:
        if session.in_transaction():
            await session.rollback()


async def load_terminal_semantic_candidate(
    session: AsyncSession,
    *,
    operation_id: uuid.UUID,
    attempt_no: int,
    project_id: uuid.UUID,
    purpose: LLMPurpose,
    input_fingerprint: str,
    contract_name: str,
    binding_fingerprint: str,
    normalizer: CandidateNormalizer,
) -> SemanticCandidateReplay:
    """Strict compatibility API requiring one exact terminal candidate."""

    replay = await load_attempt_replay(
        session,
        operation_id=operation_id,
        attempt_no=attempt_no,
        project_id=project_id,
        purpose=purpose,
        input_fingerprint=input_fingerprint,
        contract_name=contract_name,
        binding_fingerprint=binding_fingerprint,
        normalizer=normalizer,
    )
    if replay.state == AttemptReplayState.ABSENT:
        raise LLMSemanticCandidateUnavailable(
            "semantic candidate terminal attempt is unavailable"
        )
    if replay.state == AttemptReplayState.STARTED:
        raise LLMSemanticCandidateUnavailable(
            "semantic candidate attempt is not terminal"
        )
    if replay.state == AttemptReplayState.TERMINAL_WITHOUT_CANDIDATE:
        raise LLMSemanticCandidateUnavailable(
            "semantic candidate is unavailable for terminal replay"
        )
    if (
        replay.attempt_id is None
        or replay.input_fingerprint is None
        or replay.attempt_status is None
        or replay.result_status is None
        or replay.usage is None
        or replay.contract_name is None
        or replay.binding_fingerprint is None
        or replay.candidate_status is None
        or replay.payload is None
    ):
        raise LLMSemanticCandidateCorrupt(
            "semantic candidate replay projection is corrupt"
        )
    return SemanticCandidateReplay(
        attempt_id=replay.attempt_id,
        operation_id=replay.operation_id,
        attempt_no=replay.attempt_no,
        project_id=replay.project_id,
        purpose=replay.purpose,
        input_fingerprint=replay.input_fingerprint,
        contract_name=replay.contract_name,
        binding_fingerprint=replay.binding_fingerprint,
        candidate_status=replay.candidate_status,
        attempt_status=replay.attempt_status,
        result_status=replay.result_status,
        requested_model=replay.requested_model,
        resolved_model=replay.resolved_model,
        total_tokens=replay.usage.total_tokens,
        usage_status=replay.usage.status,
        payload=replay.payload,
    )


async def lock_semantic_candidate_for_product(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    project_id: uuid.UUID,
    purpose: LLMPurpose,
    contract_name: str,
    binding_fingerprint: str,
    normalizer: CandidateNormalizer,
    input_fingerprint: str | None = None,
) -> SemanticCandidateReplay:
    """Lock and validate one candidate inside a caller-owned transaction.

    Product writers use this after locking their publication head and before
    calling ``classify_attempt_result(commit=False)``.  Unlike the read-only
    replay API, this function never commits or rolls back: the grounded
    product, attempt classification, and candidate classification therefore
    succeed or fail together.
    """

    _require_clean_accounting_session(session)
    if not isinstance(attempt_id, uuid.UUID):
        raise LLMSemanticCandidateError(
            "semantic product attempt identity is invalid"
        )
    # Reuse every consumer-bound invariant; operation/attempt values are
    # validated again from the joined canonical rows below.
    _validate_attempt_replay_binding(
        operation_id=uuid.UUID(int=0),
        attempt_no=1,
        project_id=project_id,
        purpose=purpose,
        input_fingerprint=input_fingerprint,
        contract_name=contract_name,
        binding_fingerprint=binding_fingerprint,
        normalizer=normalizer,
    )
    pair = (
        await session.execute(
            select(LLMCall, LLMSemanticCandidate)
            .join(
                LLMSemanticCandidate,
                LLMSemanticCandidate.attempt_id == LLMCall.id,
            )
            .where(LLMCall.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if pair is None:
        raise LLMSemanticCandidateUnavailable(
            "semantic product candidate is unavailable"
        )
    row, stored = pair
    if row.contract_version != 1:
        raise LLMSemanticCandidateUnavailable(
            "semantic product attempt is unavailable"
        )
    if (
        row.project_id != project_id
        or row.purpose != purpose.value
        or (
            input_fingerprint is not None
            and row.input_fingerprint != input_fingerprint
        )
        or stored.contract_name != contract_name
        or stored.binding_fingerprint != binding_fingerprint
    ):
        raise LLMSemanticCandidateUnavailable(
            "semantic product candidate binding does not match"
        )
    _validate_stored_candidate_identity(stored, row)
    _assert_candidate_classification_consistent(stored, row)
    attempt = _canonical_replay_attempt(row)
    payload = _decrypt_and_validate_candidate(stored, normalizer=normalizer)
    return SemanticCandidateReplay(
        attempt_id=attempt.attempt_id,
        operation_id=attempt.operation_id,
        attempt_no=attempt.attempt_no,
        project_id=attempt.project_id,
        purpose=attempt.purpose,
        input_fingerprint=attempt.input_fingerprint,
        contract_name=stored.contract_name,
        binding_fingerprint=stored.binding_fingerprint,
        candidate_status=stored.candidate_status,
        attempt_status=attempt.attempt_status,
        result_status=attempt.result_status,
        requested_model=attempt.requested_model,
        resolved_model=attempt.resolved_model,
        total_tokens=attempt.usage.total_tokens,
        usage_status=attempt.usage.status,
        payload=payload,
    )


def _aware_utc(value: datetime) -> datetime:
    # SQLite drops timezone metadata in tests; production PostgreSQL does not.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _database_utc_now(session: AsyncSession) -> datetime:
    """Use PostgreSQL's clock for cross-worker budget deadlines."""

    if session.get_bind().dialect.name == "postgresql":
        value = (
            await session.execute(select(func.clock_timestamp()))
        ).scalar_one()
        return _aware_utc(value)
    # SQLite contract tests do not provide clock_timestamp().
    return datetime.now(tz=timezone.utc)


def _dollar_reservation_for(
    metadata: AttemptStartMetadata,
) -> tuple[ProjectDollarBudgetConfig | None, PriceReservation | None]:
    """Resolve the immutable hard-cost contract before any DB/provider work."""

    policy = configured_project_dollar_budget()
    if policy is None:
        return None, None
    try:
        contract = resolve_price_contract(
            provider=metadata.provider,
            billing_route=metadata.billing_route,
            model=metadata.requested_model,
        )
        reservation = contract.reserve(metadata.requested_max_output_tokens)
    except PriceContractError:
        # Opaque Agent SDK, Atlas/custom proxies, aliases, and unknown models
        # cannot prove a hard invoice upper bound.  No ticket means no dispatch.
        raise RunBudgetExceeded(
            "project_dollar_price_contract_unavailable"
        ) from None
    if metadata.estimated_input_tokens > reservation.reserved_input_tokens:
        raise RunBudgetExceeded("project_dollar_input_ceiling_exceeded")
    return policy, reservation


async def _lock_or_create_project_budget_account(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    policy: ProjectDollarBudgetConfig,
    run_budget: LLMRunBudget,
) -> LLMProjectBudgetAccount:
    """Create the first project lock row safely, then hold it to commit."""

    values = {
        "project_id": project_id,
        "policy_version": policy.policy_version,
        "cap_usd": policy.cap_usd,
        "window_sec": policy.window_sec,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(LLMProjectBudgetAccount).values(**values)
        statement = statement.on_conflict_do_nothing(
            index_elements=[LLMProjectBudgetAccount.project_id]
        )
        await session.execute(statement)
    elif dialect == "sqlite":
        statement = sqlite_insert(LLMProjectBudgetAccount).values(**values)
        statement = statement.on_conflict_do_nothing(
            index_elements=[LLMProjectBudgetAccount.project_id]
        )
        await session.execute(statement)
    else:  # pragma: no cover - supported deployments are PostgreSQL/SQLite
        account = await session.get(LLMProjectBudgetAccount, project_id)
        if account is None:
            session.add(LLMProjectBudgetAccount(**values))
            await session.flush()

    account = (
        await session.execute(
            select(LLMProjectBudgetAccount)
            .where(LLMProjectBudgetAccount.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if (
        account.policy_version != policy.policy_version
        or account.cap_usd != policy.cap_usd
        or int(account.window_sec) != policy.window_sec
    ):
        # Environment drift across workers must not let the looser process win.
        reason = "project_dollar_policy_mismatch"
        run_budget.stop(reason)
        raise RunBudgetExceeded(reason)
    return account


async def _advisory_project_budget_lock(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Serialize enabled and disabled workers before inspecting policy state.

    A row lock cannot protect the initially absent account row.  Taking one
    stable PostgreSQL transaction lock for *every* physical attempt prevents
    a cap-disabled worker from racing the first cap-enabled account creation.
    SQLite is retained only for sequential contract tests.
    """

    if session.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(
        b"mnemos.project-dollar-budget.v1\x00" + project_id.bytes
    ).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def _reject_disabled_worker_policy_bypass(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_budget: LLMRunBudget,
) -> None:
    """Fail closed when this worker disabled an already-persisted policy."""

    account = (
        await session.execute(
            select(LLMProjectBudgetAccount.project_id)
            .where(LLMProjectBudgetAccount.project_id == project_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if account is not None:
        reason = "project_dollar_policy_mismatch"
        run_budget.stop(reason)
        raise RunBudgetExceeded(reason)


async def _authorize_project_dollar_reservation(
    session: AsyncSession,
    *,
    account: LLMProjectBudgetAccount,
    reservation: PriceReservation,
    now: datetime,
    run_budget: LLMRunBudget,
) -> None:
    """Authorize one no-refund amount while holding the project account row."""

    since = now - timedelta(seconds=int(account.window_sec))
    reserved, unpriced = (
        await session.execute(
            select(
                func.coalesce(func.sum(LLMCall.reserved_cost_usd), 0),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (
                                    LLMCall.reserved_cost_usd.is_(None)
                                    | canonical_price_attestation_failure_predicate()
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(
                LLMCall.project_id == account.project_id,
                LLMCall.generated_at >= since,
            )
        )
    ).one()
    if int(unpriced):
        reason = "project_dollar_history_unpriced"
        run_budget.stop(reason)
        raise RunBudgetExceeded(reason)
    exposure = reserved + reservation.reserved_cost_usd
    if exposure > account.cap_usd:
        reason = "project_dollar_limit_exceeded"
        run_budget.stop(reason)
        raise RunBudgetExceeded(reason)


def _require_clean_accounting_session(session: AsyncSession) -> None:
    if session.new or session.dirty or session.deleted:
        raise LLMLifecycleError(
            "attempt accounting requires a session with no pending mutations"
        )


async def assert_attempt_accounting_isolation(session: AsyncSession) -> None:
    """Fail closed unless PostgreSQL accounting sees fresh statements.

    The project-dollar lock is acquired before the rolling reservation SUM.
    Under REPEATABLE READ, however, a transaction that established its
    snapshot before waiting for that lock can miss the previous writer's
    newly committed ``llm_calls`` row and authorize a second reservation.
    Application engines pin READ COMMITTED, while this runtime check also
    protects custom/test sessions and connections with per-transaction
    overrides. SQLite's writer serialization follows a separate local-mode
    contract and must not receive PostgreSQL-only isolation commands.
    """

    if session.get_bind().dialect.name != "postgresql":
        return
    connection = await session.connection()
    isolation_level = await connection.get_isolation_level()
    normalized = " ".join(
        str(isolation_level).upper().replace("_", " ").split()
    )
    if normalized != "READ COMMITTED":
        raise LLMLifecycleError(
            "postgresql_attempt_accounting_requires_read_committed"
        )


async def _advisory_scope_lock(
    session: AsyncSession, scope_id: uuid.UUID
) -> None:
    """Serialize first-create and reservation on PostgreSQL.

    The conditional counters remain the data invariant.  This lock also makes
    operation/attempt inspection deterministic before a row for a new scope
    exists.  SQLite deliberately skips it and is used only as E1 test evidence.
    """

    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    key = int.from_bytes(scope_id.bytes[:8], "big", signed=True)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


def _legacy_status(
    attempt_status: AttemptStatus, result_status: ResultStatus
) -> str:
    if attempt_status == AttemptStatus.COMPLETED:
        return (
            "completed"
            if result_status in {ResultStatus.ACCEPTED, ResultStatus.PENDING}
            else "fallback"
        )
    return attempt_status.value


def _usage_values(usage: LLMUsageV1) -> dict[str, object]:
    return {
        "usage_status": usage.status.value,
        "usage_source": usage.source.value,
        "input_tokens": usage.input_tokens,
        "uncached_input_tokens": usage.uncached_input_tokens,
        "output_tokens": usage.output_tokens,
        "visible_output_tokens": usage.visible_output_tokens,
        "total_tokens": usage.total_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "tool_input_tokens": usage.tool_input_tokens,
        "provider_total_tokens": usage.provider_total_tokens,
        "provider_turn_count": usage.provider_turn_count,
        "reported_cost_usd": usage.reported_cost_usd,
        "estimated_cost_usd": usage.estimated_cost_usd,
        "pricing_version": usage.pricing_version,
    }


def _sync_in_memory_budget(
    run_budget: LLMRunBudget,
    scope: LLMBudgetScope,
    *,
    now: datetime,
) -> float:
    """Make process-local stats reflect the durable policy and counters."""

    deadline = _aware_utc(scope.deadline_at)
    remaining = max(0.0, (deadline - now).total_seconds())
    run_budget.max_calls = int(scope.max_calls)
    run_budget.max_input_tokens = int(scope.max_input_tokens)
    run_budget.max_output_tokens = int(scope.max_output_tokens)
    run_budget.wall_time_sec = int(scope.wall_time_sec)
    run_budget.calls_started = int(scope.calls_reserved)
    run_budget.input_tokens_reserved = int(scope.input_tokens_reserved)
    run_budget.output_tokens_reserved = int(scope.output_tokens_reserved)
    run_budget._durable_scope_id = scope.id
    run_budget._durable_calls_synced = int(scope.calls_reserved)
    run_budget._durable_input_tokens_synced = int(
        scope.input_tokens_reserved
    )
    run_budget._durable_output_tokens_synced = int(
        scope.output_tokens_reserved
    )
    elapsed = max(0.0, run_budget.wall_time_sec - remaining)
    run_budget.started_monotonic = time.monotonic() - elapsed
    run_budget.exhausted_reason = scope.exhausted_reason
    return remaining


def _capture_budget_for_conflict(
    run_budget: LLMRunBudget,
) -> _BudgetConflictSnapshot:
    return _BudgetConflictSnapshot(
        max_calls=run_budget.max_calls,
        max_input_tokens=run_budget.max_input_tokens,
        max_output_tokens=run_budget.max_output_tokens,
        wall_time_sec=run_budget.wall_time_sec,
        started_monotonic=run_budget.started_monotonic,
        calls_started=run_budget.calls_started,
        input_tokens_reserved=run_budget.input_tokens_reserved,
        output_tokens_reserved=run_budget.output_tokens_reserved,
        exhausted_reason=run_budget.exhausted_reason,
        durable_scope_id=run_budget._durable_scope_id,
        durable_calls_synced=run_budget._durable_calls_synced,
        durable_input_tokens_synced=(
            run_budget._durable_input_tokens_synced
        ),
        durable_output_tokens_synced=(
            run_budget._durable_output_tokens_synced
        ),
    )


def _restore_conflicting_pre_reservation(
    run_budget: LLMRunBudget,
    pre_reserved: PreReservedAttempt | None,
    snapshot: _BudgetConflictSnapshot | None,
) -> None:
    """Undo only the current caller reservation after a no-dispatch conflict."""

    if pre_reserved is None or snapshot is None:
        return
    run_budget.max_calls = snapshot.max_calls
    run_budget.max_input_tokens = snapshot.max_input_tokens
    run_budget.max_output_tokens = snapshot.max_output_tokens
    run_budget.wall_time_sec = snapshot.wall_time_sec
    run_budget.started_monotonic = snapshot.started_monotonic
    run_budget.calls_started = snapshot.calls_started - 1
    run_budget.input_tokens_reserved = (
        snapshot.input_tokens_reserved - pre_reserved.input_tokens
    )
    run_budget.output_tokens_reserved = (
        snapshot.output_tokens_reserved - pre_reserved.output_tokens
    )
    run_budget.exhausted_reason = snapshot.exhausted_reason
    run_budget._durable_scope_id = snapshot.durable_scope_id
    run_budget._durable_calls_synced = snapshot.durable_calls_synced
    run_budget._durable_input_tokens_synced = (
        snapshot.durable_input_tokens_synced
    )
    run_budget._durable_output_tokens_synced = (
        snapshot.durable_output_tokens_synced
    )


def _validate_pre_reserved_attempt(
    run_budget: LLMRunBudget,
    pre_reserved: PreReservedAttempt | None,
) -> None:
    if pre_reserved is None:
        return
    for name, value, minimum in (
        ("input_tokens", pre_reserved.input_tokens, 1),
        ("output_tokens", pre_reserved.output_tokens, 0),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
        ):
            raise ValueError(
                f"pre_reserved {name} must be an integer >= {minimum}"
            )
    if (
        run_budget.calls_started < 1
        or run_budget.input_tokens_reserved < pre_reserved.input_tokens
        or run_budget.output_tokens_reserved < pre_reserved.output_tokens
    ):
        raise LLMLifecycleError(
            "pre-reserved attempt is absent from the in-memory budget"
        )


def _validate_scope_identity(
    *,
    run_budget: LLMRunBudget,
    scope_kind: BudgetScopeKind,
    analysis_run_id: uuid.UUID | None,
) -> uuid.UUID:
    scope_id = run_budget.scope_id
    if scope_kind == BudgetScopeKind.ANALYSIS_RUN and (
        analysis_run_id is None or scope_id != analysis_run_id
    ):
        raise ValueError(
            "analysis-run budget scope id must equal analysis_run_id"
        )
    return scope_id


async def _lock_or_create_budget_scope(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID | None,
    scope_kind: BudgetScopeKind,
    run_budget: LLMRunBudget,
) -> tuple[LLMBudgetScope, datetime, float]:
    scope_id = run_budget.scope_id
    await _advisory_scope_lock(session, scope_id)
    scope = (
        await session.execute(
            select(LLMBudgetScope)
            .where(LLMBudgetScope.id == scope_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    # Lock acquisition can consume the last available wall time.
    now = await _database_utc_now(session)
    sampled_monotonic = time.monotonic()
    if scope is None:
        local_remaining = run_budget.remaining_seconds()
        elapsed = max(0.0, run_budget.wall_time_sec - local_remaining)
        started_at = now - timedelta(seconds=elapsed)
        scope = LLMBudgetScope(
            id=scope_id,
            project_id=project_id,
            analysis_run_id=analysis_run_id,
            scope_kind=scope_kind.value,
            policy_version=BUDGET_POLICY_VERSION,
            max_calls=run_budget.max_calls,
            max_input_tokens=run_budget.max_input_tokens,
            max_output_tokens=run_budget.max_output_tokens,
            wall_time_sec=run_budget.wall_time_sec,
            calls_reserved=0,
            input_tokens_reserved=0,
            output_tokens_reserved=0,
            started_at=started_at,
            deadline_at=started_at + timedelta(seconds=run_budget.wall_time_sec),
        )
        session.add(scope)
        await session.flush()
    elif (
        scope.project_id != project_id
        or scope.analysis_run_id != analysis_run_id
        or scope.scope_kind != scope_kind.value
        or scope.policy_version != BUDGET_POLICY_VERSION
    ):
        raise LLMAttemptConflict("budget scope ownership mismatch")
    return scope, now, sampled_monotonic


async def _reserve_locked_budget_scope(
    session: AsyncSession,
    *,
    scope: LLMBudgetScope,
    run_budget: LLMRunBudget,
    estimate: int,
    output_reservation: int,
    pre_reserved: PreReservedAttempt | None,
    now: datetime,
) -> None:
    """Apply one call plus any unpersisted opaque local delta to a lock."""

    scope_id = run_budget.scope_id
    if pre_reserved is not None:
        if run_budget._durable_scope_id == scope_id:
            calls_to_add = (
                run_budget.calls_started - run_budget._durable_calls_synced
            )
            input_to_add = (
                run_budget.input_tokens_reserved
                - run_budget._durable_input_tokens_synced
            )
            output_to_add = (
                run_budget.output_tokens_reserved
                - run_budget._durable_output_tokens_synced
            )
        else:
            calls_to_add = run_budget.calls_started
            input_to_add = run_budget.input_tokens_reserved
            output_to_add = run_budget.output_tokens_reserved
        if (
            calls_to_add < 1
            or input_to_add < pre_reserved.input_tokens
            or output_to_add < pre_reserved.output_tokens
        ):
            raise LLMLifecycleError(
                "pre-reserved attempt is absent from the in-memory budget"
            )
        input_to_add += max(0, estimate - pre_reserved.input_tokens)
        output_to_add += max(0, output_reservation - pre_reserved.output_tokens)
    else:
        calls_to_add = 1
        input_to_add = estimate
        output_to_add = output_reservation

    # A persisted policy always wins over changed environment values.
    _sync_in_memory_budget(run_budget, scope, now=now)
    if pre_reserved is None:
        try:
            run_budget.reserve(estimate, output_reservation)
        except RunBudgetExceeded as exc:
            if scope.exhausted_reason is None:
                scope.exhausted_reason = exc.reason
            await session.commit()
            raise

    reason: str | None = scope.exhausted_reason
    if reason is None and now >= _aware_utc(scope.deadline_at):
        reason = "run_deadline_exceeded"
    if (
        reason is None
        and scope.calls_reserved + calls_to_add > scope.max_calls
    ):
        reason = "run_call_limit_exceeded"
    if (
        reason is None
        and scope.input_tokens_reserved + input_to_add
        > scope.max_input_tokens
    ):
        reason = "run_input_token_limit_exceeded"
    if (
        reason is None
        and scope.output_tokens_reserved + output_to_add
        > scope.max_output_tokens
    ):
        reason = "run_output_token_limit_exceeded"
    if reason is not None:
        scope.exhausted_reason = reason
        run_budget.stop(reason)
        await session.commit()
        raise RunBudgetExceeded(reason)

    scope.calls_reserved += calls_to_add
    scope.input_tokens_reserved += input_to_add
    scope.output_tokens_reserved += output_to_add


async def persist_pre_reserved_budget(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID | None,
    run_budget: LLMRunBudget,
    scope_kind: BudgetScopeKind,
    pre_reserved: PreReservedAttempt,
) -> float:
    """Commit an opaque consumer's budget before its provider dispatch.

    Agent SDK paths do not yet have an honest provider output-token cap or a
    complete v1 usage envelope.  They still must not reset calls/input/time on
    worker restart.  This boundary persists only conservative budget facts;
    it does not fabricate a canonical physical-attempt row.
    """

    _require_clean_accounting_session(session)
    _validate_pre_reserved_attempt(run_budget, pre_reserved)
    _validate_scope_identity(
        run_budget=run_budget,
        scope_kind=scope_kind,
        analysis_run_id=analysis_run_id,
    )
    snapshot = _capture_budget_for_conflict(run_budget)
    try:
        scope, now, sampled_monotonic = await _lock_or_create_budget_scope(
            session,
            project_id=project_id,
            analysis_run_id=analysis_run_id,
            scope_kind=scope_kind,
            run_budget=run_budget,
        )
        await _reserve_locked_budget_scope(
            session,
            scope=scope,
            run_budget=run_budget,
            estimate=pre_reserved.input_tokens,
            output_reservation=pre_reserved.output_tokens,
            pre_reserved=pre_reserved,
            now=now,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        _restore_conflicting_pre_reservation(
            run_budget, pre_reserved, snapshot
        )
        raise LLMAttemptConflict(
            "durable budget reservation collided with another writer"
        ) from exc
    except LLMAttemptConflict:
        if session.in_transaction():
            await session.rollback()
        _restore_conflicting_pre_reservation(
            run_budget, pre_reserved, snapshot
        )
        raise
    except RunBudgetExceeded:
        if session.in_transaction():
            await session.rollback()
        raise
    except asyncio.CancelledError:
        if session.in_transaction():
            try:
                await asyncio.shield(session.rollback())
            except Exception:  # noqa: BLE001
                pass
        _restore_conflicting_pre_reservation(
            run_budget, pre_reserved, snapshot
        )
        raise
    except Exception:
        if session.in_transaction():
            await session.rollback()
        _restore_conflicting_pre_reservation(
            run_budget, pre_reserved, snapshot
        )
        raise LLMLifecycleError(
            "durable opaque budget reservation failed before dispatch"
        )

    return _sync_in_memory_budget(
        run_budget,
        scope,
        now=now
        + timedelta(
            seconds=max(0.0, time.monotonic() - sampled_monotonic)
        ),
    )


async def _assert_operation_dispatch_available(
    session: AsyncSession,
    *,
    operation_id: uuid.UUID,
    attempt_no: int,
) -> None:
    """Read the complete duplicate/open-attempt state in one statement."""

    duplicate_count, open_count = (
        await session.execute(
            select(
                func.count(
                    case((LLMCall.attempt_no == attempt_no, 1))
                ),
                func.count(
                    case(
                        (
                            LLMCall.attempt_status
                            == AttemptStatus.STARTED.value,
                            1,
                        )
                    )
                ),
            ).where(LLMCall.operation_id == operation_id)
        )
    ).one()
    if int(duplicate_count):
        raise LLMAttemptReplayBlocked(
            "physical attempt already exists; refusing redispatch"
        )
    if int(open_count):
        raise LLMAttemptReplayBlocked(
            "operation has an unresolved started attempt"
        )


async def begin_attempt(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    analysis_run_id: uuid.UUID | None,
    purpose: LLMPurpose,
    target_id: str,
    level: int,
    run_budget: LLMRunBudget,
    scope_kind: BudgetScopeKind,
    metadata: AttemptStartMetadata,
    pre_reserved: PreReservedAttempt | None = None,
    require_atomic_dollar_reservation: bool = False,
) -> AttemptTicket:
    """Atomically reserve durable budget and commit one v1 STARTED row.

    No provider may be invoked unless this function returns a ticket.  The
    caller may already have applied the legacy in-process reservation.  In
    that case the durable scope absorbs every local reservation since its last
    sync and conservatively keeps the larger of the current caller estimate
    and the adapter estimate. Production network callsites must set
    ``require_atomic_dollar_reservation=True``; the default exists only for
    ledger contract tests and non-dispatch maintenance helpers.
    """

    _require_clean_accounting_session(session)
    validation_now = datetime.now(tz=timezone.utc)
    if (
        isinstance(metadata.estimated_input_tokens, bool)
        or not isinstance(metadata.estimated_input_tokens, int)
        or metadata.estimated_input_tokens < 1
    ):
        raise ValueError("estimated_input_tokens must be a positive integer")
    if (
        isinstance(metadata.requested_max_output_tokens, bool)
        or not isinstance(metadata.requested_max_output_tokens, int)
        or metadata.requested_max_output_tokens < 1
    ):
        raise ValueError(
            "requested_max_output_tokens must be a positive integer"
        )
    estimate = metadata.estimated_input_tokens
    output_reservation = metadata.requested_max_output_tokens
    dollar_policy: ProjectDollarBudgetConfig | None = None
    dollar_reservation: PriceReservation | None = None
    _validate_pre_reserved_attempt(run_budget, pre_reserved)
    conflict_snapshot = (
        _capture_budget_for_conflict(run_budget)
        if pre_reserved is not None
        else None
    )
    attempt_id = uuid.uuid4()
    scope_id = _validate_scope_identity(
        run_budget=run_budget,
        scope_kind=scope_kind,
        analysis_run_id=analysis_run_id,
    )
    if metadata.attempt_no == 1:
        if (
            metadata.attempt_kind != AttemptKind.PRIMARY
            or metadata.parent_attempt_id is not None
        ):
            raise ValueError("attempt 1 must be a parentless primary")
    elif (
        metadata.attempt_kind == AttemptKind.PRIMARY
        or metadata.parent_attempt_id is None
    ):
        raise ValueError("later attempts require retry/fallback lineage")

    # Validate every bounded/string/enum invariant before touching the DB.
    canonical = LLMPhysicalAttemptV1(
        attempt_id=attempt_id,
        operation_id=metadata.operation_id,
        budget_scope_id=scope_id,
        project_id=project_id,
        analysis_run_id=analysis_run_id,
        purpose=purpose,
        target_id=target_id,
        attempt_no=metadata.attempt_no,
        parent_attempt_id=metadata.parent_attempt_id,
        attempt_kind=metadata.attempt_kind,
        provider=metadata.provider,
        provider_mode=metadata.provider_mode,
        requested_model=metadata.requested_model,
        input_fingerprint=metadata.input_fingerprint,
        estimated_input_tokens=estimate,
        input_estimate_method=metadata.input_estimate_method,
        requested_max_output_tokens=output_reservation,
        attempt_status=AttemptStatus.STARTED,
        result_status=ResultStatus.PENDING,
        started_at=validation_now,
        usage=unavailable_usage(source=metadata.usage_source),
    )

    try:
        # This must precede every mutation and lock. Some callers perform a
        # read-only budget probe in this session first, so changing isolation
        # here would already be too late; reject an unsafe custom transaction
        # rather than authorizing against a stale snapshot.
        await assert_attempt_accounting_isolation(session)
        # Exact replays and unresolved operations are read before interpreting
        # a possibly new dollar policy or touching any durable budget state.
        await _assert_operation_dispatch_available(
            session,
            operation_id=metadata.operation_id,
            attempt_no=metadata.attempt_no,
        )
        # This lock is intentionally acquired even when the local cap is off:
        # persisted project policy must outrank a stale/misconfigured worker.
        await _advisory_project_budget_lock(session, project_id)
        # PostgreSQL READ COMMITTED takes a fresh snapshot after the lock.  A
        # concurrent winner may have committed between the preflight SELECT
        # and lock acquisition, so this recheck must precede every mutation.
        await _assert_operation_dispatch_available(
            session,
            operation_id=metadata.operation_id,
            attempt_no=metadata.attempt_no,
        )
        try:
            dollar_policy, dollar_reservation = _dollar_reservation_for(
                metadata
            )
        except RunBudgetExceeded as exc:
            run_budget.stop(exc.reason)
            raise
        if require_atomic_dollar_reservation and dollar_policy is None:
            reason = "project_dollar_budget_required"
            run_budget.stop(reason)
            raise RunBudgetExceeded(reason)
        if dollar_policy is not None and dollar_reservation is not None:
            account = await _lock_or_create_project_budget_account(
                session,
                project_id=project_id,
                policy=dollar_policy,
                run_budget=run_budget,
            )
            dollar_now = await _database_utc_now(session)
            await _authorize_project_dollar_reservation(
                session,
                account=account,
                reservation=dollar_reservation,
                now=dollar_now,
                run_budget=run_budget,
            )
        else:
            await _reject_disabled_worker_policy_bypass(
                session,
                project_id=project_id,
                run_budget=run_budget,
            )
        scope, now, sampled_monotonic = await _lock_or_create_budget_scope(
            session,
            project_id=project_id,
            analysis_run_id=analysis_run_id,
            scope_kind=scope_kind,
            run_budget=run_budget,
        )
        # The persisted attempt timestamp uses the post-lock clock too.
        canonical = replace(canonical, started_at=now)
        if metadata.attempt_no > 1:
            parent = await session.get(LLMCall, metadata.parent_attempt_id)
            if (
                parent is None
                or parent.operation_id != metadata.operation_id
                or parent.attempt_no != metadata.attempt_no - 1
                or parent.attempt_status == AttemptStatus.STARTED.value
            ):
                raise LLMAttemptConflict("retry/fallback lineage is invalid")

        await _reserve_locked_budget_scope(
            session,
            scope=scope,
            run_budget=run_budget,
            estimate=estimate,
            output_reservation=output_reservation,
            pre_reserved=pre_reserved,
            now=now,
        )
        call = LLMCall(
            id=canonical.attempt_id,
            project_id=canonical.project_id,
            analysis_run_id=canonical.analysis_run_id,
            target_id=canonical.target_id,
            level=level,
            model_used=canonical.requested_model,
            tokens_used=None,
            status="started",
            fallback_reason=None,
            generated_at=canonical.started_at,
            contract_version=1,
            operation_id=canonical.operation_id,
            budget_scope_id=canonical.budget_scope_id,
            purpose=canonical.purpose.value,
            attempt_no=canonical.attempt_no,
            parent_attempt_id=canonical.parent_attempt_id,
            attempt_kind=canonical.attempt_kind.value,
            provider=canonical.provider,
            provider_mode=canonical.provider_mode,
            requested_model=canonical.requested_model,
            resolved_model=canonical.resolved_model,
            provider_request_id=canonical.provider_request_id,
            input_fingerprint=canonical.input_fingerprint,
            estimated_input_tokens=canonical.estimated_input_tokens,
            input_estimate_method=canonical.input_estimate_method,
            requested_max_output_tokens=canonical.requested_max_output_tokens,
            reserved_input_tokens=(
                dollar_reservation.reserved_input_tokens
                if dollar_reservation is not None
                else None
            ),
            reserved_output_tokens=(
                dollar_reservation.reserved_output_tokens
                if dollar_reservation is not None
                else None
            ),
            reservation_price_contract_id=(
                dollar_reservation.price_contract_id
                if dollar_reservation is not None
                else None
            ),
            reserved_cost_usd=(
                dollar_reservation.reserved_cost_usd
                if dollar_reservation is not None
                else None
            ),
            attempt_status=canonical.attempt_status.value,
            result_status=canonical.result_status.value,
            started_at=canonical.started_at,
            **_usage_values(canonical.usage),
        )
        session.add(call)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        _restore_conflicting_pre_reservation(
            run_budget, pre_reserved, conflict_snapshot
        )
        raise LLMAttemptConflict(
            "physical attempt start collided with another writer"
        ) from exc
    except LLMAttemptConflict:
        if session.in_transaction():
            await session.rollback()
        _restore_conflicting_pre_reservation(
            run_budget, pre_reserved, conflict_snapshot
        )
        raise
    except RunBudgetExceeded:
        # A first denied request must still persist the project-policy row;
        # otherwise a cap-disabled worker could continue to bypass it. Scope
        # limit paths already commit their exhausted state before raising.
        if session.in_transaction():
            await session.commit()
        raise
    except asyncio.CancelledError:
        if session.in_transaction():
            try:
                await asyncio.shield(session.rollback())
            except Exception:  # noqa: BLE001
                pass
        _restore_conflicting_pre_reservation(
            run_budget, pre_reserved, conflict_snapshot
        )
        raise
    except Exception:
        if session.in_transaction():
            await session.rollback()
        # No ticket was returned and therefore no provider dispatch is
        # authorized.  A transient DB/lock failure must not turn the current
        # caller's preflight reservation into phantom spend on the next call.
        _restore_conflicting_pre_reservation(
            run_budget, pre_reserved, conflict_snapshot
        )
        raise

    remaining = _sync_in_memory_budget(
        run_budget,
        scope,
        now=now
        + timedelta(
            seconds=max(0.0, time.monotonic() - sampled_monotonic)
        ),
    )
    return AttemptTicket(
        attempt_id=canonical.attempt_id,
        operation_id=canonical.operation_id,
        budget_scope_id=canonical.budget_scope_id,
        started_at=canonical.started_at,
        remaining_seconds=remaining,
    )


# v1 has no positive subscription/billing attestation.  Agent SDK init can
# prove a bounded API/cloud key source or remain unknown, but it cannot safely
# authorize incremental-dollar exclusion.
_OBSERVED_PROVIDER_MODES = frozenset({"unknown", "api"})


def _terminal_provider_mode(row: LLMCall, outcome: AttemptOutcome) -> str:
    """Resolve one auth-provenance transition under the row lock."""

    stored = cast(str, row.provider_mode)
    observed = outcome.provider_mode
    if observed is None:
        return stored
    if observed not in _OBSERVED_PROVIDER_MODES:
        raise LLMAttemptConflict("provider mode override is invalid")
    if stored == "unknown":
        return observed
    if stored in {"api", "subscription"} and observed == stored:
        return stored
    raise LLMAttemptConflict("provider mode cannot rewrite established provenance")


def _same_terminal(row: LLMCall, outcome: AttemptOutcome) -> bool:
    usage_matches = all(
        getattr(row, name) == value
        for name, value in _usage_values(outcome.usage).items()
    )
    provider_mode_matches = (
        outcome.provider_mode is None
        or row.provider_mode == outcome.provider_mode
    )
    return bool(
        row.attempt_status == outcome.attempt_status.value
        and row.result_status == outcome.result_status.value
        and row.failure_code == outcome.failure_code
        and row.finish_reason == outcome.finish_reason
        and row.resolved_model == outcome.resolved_model
        and row.provider_request_id == outcome.provider_request_id
        and provider_mode_matches
        and usage_matches
    )


def _validate_terminal_price_attestation(
    row: LLMCall,
    outcome: AttemptOutcome,
) -> str | None:
    """Re-derive the reservation and return any terminal breach code.

    Adapter checks are the first line of defence.  This row-lock check keeps a
    future/alternate caller from finalizing a price-reserved attempt with a
    different (potentially more expensive) model or with corrupted reservation
    fields.  Rejected provider responses may still be terminalized so their
    usage and mismatched identity remain durable and block subsequent calls.
    """

    contract_id = row.reservation_price_contract_id
    if contract_id is None:
        return None
    try:
        contract = resolve_price_contract_by_id(contract_id)
        expected = contract.reserve(cast(int, row.requested_max_output_tokens))
    except PriceContractError as exc:
        raise LLMAttemptConflict(
            "stored price reservation has no immutable contract"
        ) from exc
    if (
        contract.provider != row.provider
        or contract.model != row.requested_model
        or contract_id != expected.price_contract_id
        or row.reserved_input_tokens != expected.reserved_input_tokens
        or row.reserved_output_tokens != expected.reserved_output_tokens
        or row.reserved_cost_usd != expected.reserved_cost_usd
    ):
        raise LLMAttemptConflict("stored price reservation was mutated")

    model_unattested = (
        outcome.resolved_model != contract.model
        if outcome.resolved_model is not None
        else outcome.attempt_status == AttemptStatus.COMPLETED
    )
    if model_unattested:
        return PRICE_ATTESTATION_MODEL_MISMATCH
    usage = outcome.usage
    if (
        usage.input_tokens is not None
        and usage.input_tokens > expected.reserved_input_tokens
    ):
        return PRICE_ATTESTATION_INPUT_BREACH
    if (
        usage.output_tokens is not None
        and usage.output_tokens > expected.reserved_output_tokens
    ):
        return PRICE_ATTESTATION_OUTPUT_BREACH
    if usage.tool_input_tokens is not None and usage.tool_input_tokens > 0:
        return PRICE_ATTESTATION_TOOL_BREACH
    if (
        usage.reported_cost_usd is not None
        and usage.reported_cost_usd > expected.reserved_cost_usd
    ):
        return PRICE_ATTESTATION_COST_BREACH
    return None


async def finalize_attempt(
    session: AsyncSession,
    *,
    ticket: AttemptTicket,
    outcome: AttemptOutcome,
    candidate: SemanticCandidate | None = None,
    completed_at: datetime | None = None,
) -> LLMCall:
    """Finalize one STARTED row and optionally commit its encrypted candidate.

    Existing accounting-only callers may omit ``candidate``.  When supplied,
    the terminal attempt update and candidate insert are one transaction;
    neither can become visible without the other.  Identical terminal replay
    is idempotent only when the candidate metadata and digest also match.
    """

    _require_clean_accounting_session(session)
    completed = completed_at
    try:
        row = (
            await session.execute(
                select(LLMCall)
                .where(LLMCall.id == ticket.attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None or row.contract_version != 1:
            raise LLMAttemptConflict("canonical physical attempt is missing")
        if (
            row.operation_id != ticket.operation_id
            or row.budget_scope_id != ticket.budget_scope_id
        ):
            raise LLMAttemptConflict(
                "physical attempt ticket binding does not match"
            )
        price_breach = _validate_terminal_price_attestation(row, outcome)
        reject_semantic_result = bool(
            price_breach
            and outcome.result_status
            in {ResultStatus.PENDING, ResultStatus.ACCEPTED}
        )
        effective_outcome = (
            replace(
                outcome,
                result_status=ResultStatus.NOT_APPLICABLE,
                failure_code=price_breach,
            )
            if price_breach is not None
            else outcome
        )
        effective_candidate = candidate if price_breach is None else None
        if effective_candidate is not None and (
            effective_outcome.attempt_status != AttemptStatus.COMPLETED
            or effective_outcome.result_status != ResultStatus.PENDING
            or effective_outcome.failure_code is not None
        ):
            raise LLMSemanticCandidateError(
                "semantic candidate requires a completed pending transport "
                "without a failure code"
            )
        # Accounting-only legacy/foundation callers remain schema-compatible:
        # do not touch the candidate table unless this finalization actually
        # carries a candidate. Candidate-aware replay must pass it again, or
        # use ``load_terminal_semantic_candidate`` for verified recovery.
        stored_candidate = (
            await session.get(LLMSemanticCandidate, row.id)
            if effective_candidate is not None
            else None
        )
        if row.attempt_status != AttemptStatus.STARTED.value:
            if _same_terminal(row, effective_outcome):
                if stored_candidate is None and effective_candidate is not None:
                    raise LLMAttemptConflict(
                        "terminal attempt lost its semantic candidate"
                    )
                if (
                    stored_candidate is not None
                    and effective_candidate is not None
                    and not _candidate_metadata_matches(
                        stored_candidate,
                        row=row,
                        candidate=effective_candidate,
                    )
                ):
                    raise LLMAttemptConflict(
                        "terminal semantic candidate replay changed"
                    )
                await session.commit()
                if reject_semantic_result:
                    raise LLMPriceContractBreach(price_breach)
                return row
            raise LLMAttemptConflict("physical attempt already finalized differently")
        if effective_candidate is not None and stored_candidate is not None:
            raise LLMAttemptConflict(
                "started attempt already has a semantic candidate"
            )

        provider_mode = _terminal_provider_mode(row, effective_outcome)
        started = _aware_utc(row.started_at or ticket.started_at)
        if completed is None:
            completed = await _database_utc_now(session)
        completed = _aware_utc(completed)
        duration_ms = max(0, int((completed - started).total_seconds() * 1000))
        canonical = LLMPhysicalAttemptV1(
            attempt_id=row.id,
            operation_id=cast(uuid.UUID, row.operation_id),
            budget_scope_id=cast(uuid.UUID, row.budget_scope_id),
            project_id=row.project_id,
            analysis_run_id=row.analysis_run_id,
            purpose=LLMPurpose(cast(str, row.purpose)),
            target_id=row.target_id,
            attempt_no=cast(int, row.attempt_no),
            parent_attempt_id=row.parent_attempt_id,
            attempt_kind=AttemptKind(cast(str, row.attempt_kind)),
            provider=cast(str, row.provider),
            provider_mode=provider_mode,
            requested_model=cast(str, row.requested_model),
            resolved_model=effective_outcome.resolved_model,
            provider_request_id=effective_outcome.provider_request_id,
            input_fingerprint=cast(str, row.input_fingerprint),
            estimated_input_tokens=cast(int, row.estimated_input_tokens),
            input_estimate_method=cast(str, row.input_estimate_method),
            requested_max_output_tokens=cast(
                int, row.requested_max_output_tokens
            ),
            attempt_status=effective_outcome.attempt_status,
            result_status=effective_outcome.result_status,
            failure_code=effective_outcome.failure_code,
            finish_reason=effective_outcome.finish_reason,
            started_at=started,
            completed_at=completed,
            duration_ms=duration_ms,
            usage=effective_outcome.usage,
        )
        row.model_used = canonical.resolved_model or canonical.requested_model
        row.tokens_used = canonical.usage.total_tokens
        row.status = _legacy_status(
            canonical.attempt_status, canonical.result_status
        )
        row.fallback_reason = canonical.failure_code
        row.attempt_status = canonical.attempt_status.value
        row.result_status = canonical.result_status.value
        row.failure_code = canonical.failure_code
        row.finish_reason = canonical.finish_reason
        row.resolved_model = canonical.resolved_model
        row.provider_request_id = canonical.provider_request_id
        row.provider_mode = canonical.provider_mode
        row.completed_at = canonical.completed_at
        row.duration_ms = canonical.duration_ms
        for name, value in _usage_values(canonical.usage).items():
            setattr(row, name, value)
        if effective_candidate is not None:
            session.add(
                LLMSemanticCandidate(
                    **_encrypted_candidate_values(row, effective_candidate)
                )
            )
        await session.commit()
        if reject_semantic_result:
            raise LLMPriceContractBreach(price_breach)
        return row
    except IntegrityError:
        if session.in_transaction():
            await session.rollback()
        raise LLMAttemptConflict(
            "semantic candidate terminal commit collided"
        ) from None
    except Exception:
        if session.in_transaction():
            await session.rollback()
        raise


async def reconcile_stale_attempts(
    session: AsyncSession,
    *,
    grace_seconds: int = 300,
    limit: int = 100,
) -> int:
    """Close bounded, deadline-expired STARTED rows as unknown spend.

    A hard-killed worker cannot finalize its provider call.  Once the owning
    durable scope deadline plus a grace window has passed, no legitimate call
    from that scope may still be running.  The reservation remains consumed;
    only the attempt status is closed, with usage explicitly lost rather than
    fabricated as zero.
    """

    _require_clean_accounting_session(session)
    if (
        isinstance(grace_seconds, bool)
        or not isinstance(grace_seconds, int)
        or grace_seconds < 0
    ):
        raise ValueError("grace_seconds must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 1_000
    ):
        raise ValueError("limit must be between 1 and 1000")
    now = await _database_utc_now(session)
    cutoff = now - timedelta(seconds=grace_seconds)
    rows = (
        await session.execute(
            select(LLMCall)
            .join(
                LLMBudgetScope,
                LLMBudgetScope.id == LLMCall.budget_scope_id,
            )
            .where(
                LLMCall.contract_version == 1,
                LLMCall.attempt_status == AttemptStatus.STARTED.value,
                LLMBudgetScope.deadline_at <= cutoff,
            )
            .order_by(LLMBudgetScope.deadline_at, LLMCall.started_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    failure_code = "stale_started_reconciled"
    for row in rows:
        started = _aware_utc(row.started_at or row.generated_at)
        completed = max(started, now)
        try:
            source = UsageSource(cast(str, row.usage_source))
        except (TypeError, ValueError):
            source = UsageSource.UNKNOWN
        usage = unavailable_usage(
            lost_after_dispatch=True,
            source=source,
        )
        row.tokens_used = None
        row.status = AttemptStatus.UNKNOWN.value
        row.fallback_reason = failure_code
        row.attempt_status = AttemptStatus.UNKNOWN.value
        row.result_status = ResultStatus.NOT_APPLICABLE.value
        row.failure_code = failure_code
        row.completed_at = completed
        row.duration_ms = max(
            0,
            int((completed - started).total_seconds() * 1000),
        )
        for name, value in _usage_values(usage).items():
            setattr(row, name, value)
    await session.commit()
    return len(rows)


async def classify_attempt_result(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    result_status: ResultStatus,
    failure_code: str | None = None,
    commit: bool = True,
) -> None:
    """CAS the consumer result and candidate projection.

    With the default ``commit=True``, this function owns the transaction and
    preserves the historical commit/rollback behavior.  ``commit=False`` is a
    composition boundary: it acquires the same row locks and stages the same
    changes, but the caller owns the surrounding commit or rollback and may
    atomically insert its grounded product before ending the transaction.
    Errors in composition mode are deliberately not rolled back here.
    """

    _require_clean_accounting_session(session)
    if not isinstance(commit, bool):
        raise ValueError("commit must be a boolean")
    if result_status in {ResultStatus.PENDING, ResultStatus.NOT_APPLICABLE}:
        raise ValueError("consumer classification must be a semantic result")
    try:
        row = (
            await session.execute(
                select(LLMCall)
                .where(LLMCall.id == attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None or row.contract_version != 1:
            raise LLMAttemptConflict("canonical physical attempt is missing")
        if row.attempt_status == AttemptStatus.STARTED.value:
            raise LLMAttemptConflict("cannot classify an unfinished transport")
        stored_candidate = (
            await session.execute(
                select(LLMSemanticCandidate)
                .where(LLMSemanticCandidate.attempt_id == attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if stored_candidate is not None:
            _validate_stored_candidate_identity(stored_candidate, row)
        if row.result_status == result_status.value:
            if row.failure_code != failure_code:
                raise LLMAttemptConflict("result replay changed its failure code")
            if stored_candidate is not None:
                _assert_candidate_classification_consistent(
                    stored_candidate,
                    row,
                )
            if commit:
                await session.commit()
            return
        if row.result_status != ResultStatus.PENDING.value:
            raise LLMAttemptConflict("physical result already classified differently")
        if (
            result_status == ResultStatus.ACCEPTED
            and row.attempt_status != AttemptStatus.COMPLETED.value
        ):
            raise LLMAttemptConflict("only completed transport can be accepted")
        if stored_candidate is not None:
            _assert_candidate_classification_consistent(stored_candidate, row)
            candidate_status, candidate_failure = (
                _candidate_classification_target(
                    result_status=result_status,
                    failure_code=failure_code,
                )
            )
            stored_candidate.candidate_status = candidate_status
            stored_candidate.failure_code = candidate_failure
        row.result_status = result_status.value
        row.failure_code = failure_code
        if result_status == ResultStatus.ACCEPTED:
            row.status = "completed"
            row.fallback_reason = None
        else:
            row.status = "fallback"
            row.fallback_reason = failure_code
        if commit:
            await session.commit()
    except Exception:
        if commit and session.in_transaction():
            await session.rollback()
        raise
