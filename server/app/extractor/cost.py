"""PR-138 — LLM cost accounting + budget guard.

The MCP/LLM audit noted: token counts are recorded per Summary row
and the ROI dashboard already converts ``MNEMOS_LLM_USD_PER_MTOK *
total_tokens`` to dollars, but there is no module owning that math
and no budget guard — an extractor loop could runaway-spend with
nothing stopping it.

This module is the single place that:

1. Converts tokens → dollars (single source of truth for the rate;
   ``api/findings.py`` and ``api/analysis.py`` import from here so
   the dashboard, the ROI panel, and the budget guard all agree).
2. Sums per-project known spend over a window from the physical ``LLMCall``
   ledger (including map/reduce partials and rejected responses). Exact
   provider cost wins; otherwise a configured flat token rate is an estimate.
3. Implements ``BudgetGuard.check_budget(project_id)`` — raises
   ``BudgetExceeded`` when the project's running spend in the
   configured window crosses the per-project cap. Callers
   (extractor runner) catch and fall back to the stub path
   (visible via PR-138 ``stub:budget_exceeded`` model_used label) so
   the system degrades gracefully instead of stopping.

Configuration (env)
-------------------
- ``MNEMOS_LLM_USD_PER_MTOK`` (finite positive float, default 3.0) — a
  display/budget estimate for rows without provider-reported cost. It is not
  a versioned model/input/output price schedule or a hard billing upper bound.
- ``MNEMOS_LLM_BUDGET_USD_PER_PROJECT`` (float, default 0) — unset/0
  disables new paid production dispatch. A finite positive value is explicit
  authorization for exact routes with immutable worst-price contracts; their
  no-refund reservations must remain within this rolling-window cap.
- ``MNEMOS_LLM_BUDGET_WINDOW_SEC`` (int, default 86400 = 24h).

Failure mode
------------
When known configured-rate exposure reaches the cap, or a v1 attempt has
neither usage nor reported cost, the extractor records a
``BudgetExceeded`` event and the result falls through to the stub
path with ``fallback_reason="budget_exceeded"``. Both
``mnemos_llm_fallback_total`` (from PR-138) and an audit-log entry
make the event visible. v1 has no positive subscription/billing attestation,
so no call is excluded as free. An invalid explicit cap fails closed.
"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, case, false, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.llm.pricing import PRICE_CATALOG_POLICY_VERSION

log = logging.getLogger(__name__)


# ─── single source of truth for the rate ─────────────────────────


def rate_usd_per_mtok() -> float:
    """Return the configured cost rate.

    Kept as a function (not a module constant) so test cases that
    monkey-patch the env see the new value without reload."""
    raw = os.environ.get("MNEMOS_LLM_USD_PER_MTOK")
    if raw is None:
        return 3.0
    try:
        value = float(raw)
    except ValueError:
        raise BudgetConfigurationError(
            "MNEMOS_LLM_USD_PER_MTOK must be a finite positive number"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise BudgetConfigurationError(
            "MNEMOS_LLM_USD_PER_MTOK must be a finite positive number"
        )
    return value


def tokens_to_usd(tokens: int | None) -> float:
    """Convert a token count to USD using the configured rate.

    ``None`` is treated as zero by this conversion helper; callers that need
    to distinguish unknown API usage from subscription usage must use the
    physical-ledger classification in ``project_budget_exposure``.  Keep the
    full floating-point precision here: rounding is a presentation concern,
    and rounding before the guard comparison can authorize spend above a
    very small configured cap.
    """
    if not tokens or tokens <= 0:
        return 0.0
    return (tokens * rate_usd_per_mtok()) / 1_000_000.0


# ─── budget enforcement ──────────────────────────────────────────


class BudgetExceeded(Exception):
    """The configured-rate cap cannot safely authorize another call.

    This covers both known exposure at/above the cap and indeterminate
    non-subscription calls. Callers should fall back to the stub path so the
    deterministic pipeline keeps moving.
    """


class BudgetConfigurationError(BudgetExceeded):
    """An explicitly configured monetary guard is unsafe to evaluate."""


class RunBudgetExceeded(Exception):
    """One optional narration run crossed a non-monetary hard limit."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


@dataclass
class LLMRunBudget:
    """Hard, provider-independent bounds for one opt-in narration run.

    Dollar accounting cannot protect subscription calls whose provider does
    not return usage.  This budget therefore reserves calls and estimated
    prompt tokens *before* invoking any backend and also owns one absolute
    wall deadline shared by L1, L2 and L3.  None of these guards can be
    disabled; operators may raise the finite ceilings through environment
    variables when a deliberately larger narration pass is required.
    """

    max_calls: int = 64
    max_input_tokens: int = 120_000
    max_output_tokens: int = 128_000
    wall_time_sec: int = 600
    scope_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_monotonic: float = field(default_factory=time.monotonic)
    calls_started: int = 0
    input_tokens_reserved: int = 0
    output_tokens_reserved: int = 0
    exhausted_reason: str | None = None
    # Last counters copied from a durable scope.  Opaque consumers that have
    # not migrated to the physical-attempt ledger can still reserve against
    # this object; the next migrated call absorbs only that local delta.
    _durable_scope_id: uuid.UUID | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _durable_calls_synced: int = field(
        default=0, init=False, repr=False, compare=False
    )
    _durable_input_tokens_synced: int = field(
        default=0, init=False, repr=False, compare=False
    )
    _durable_output_tokens_synced: int = field(
        default=0, init=False, repr=False, compare=False
    )

    @classmethod
    def from_env(
        cls, *, scope_id: uuid.UUID | None = None
    ) -> "LLMRunBudget":
        return cls(
            max_calls=_bounded_env_int(
                "MNEMOS_LLM_MAX_CALLS_PER_RUN", 64, minimum=1, maximum=10_000
            ),
            max_input_tokens=_bounded_env_int(
                "MNEMOS_LLM_MAX_INPUT_TOKENS_PER_RUN",
                120_000,
                minimum=1_000,
                maximum=50_000_000,
            ),
            max_output_tokens=_bounded_env_int(
                "MNEMOS_LLM_MAX_OUTPUT_TOKENS_PER_RUN",
                128_000,
                minimum=1_000,
                maximum=50_000_000,
            ),
            wall_time_sec=_bounded_env_int(
                "MNEMOS_LLM_WALL_TIME_SEC", 600, minimum=30, maximum=7_200
            ),
            scope_id=scope_id or uuid.uuid4(),
        )

    @property
    def exhausted(self) -> bool:
        return self.exhausted_reason is not None

    def remaining_seconds(self) -> float:
        return max(
            0.0,
            self.wall_time_sec - (time.monotonic() - self.started_monotonic),
        )

    def stop(self, reason: str) -> None:
        if self.exhausted_reason is None:
            self.exhausted_reason = reason

    def reserve(
        self,
        estimated_input_tokens: int,
        requested_output_tokens: int = 0,
    ) -> float:
        """Reserve one physical provider attempt or fail closed.

        ``requested_output_tokens`` is the provider cap, or the equivalent
        client-side ceiling for opaque subscription backends.  Existing
        extraction callers may omit it while they migrate; consumers that do
        know their output limit reserve it before dispatch so retries and
        fallbacks cannot multiply output work without bound.
        """

        if self.exhausted_reason is not None:
            raise RunBudgetExceeded(self.exhausted_reason)
        if self.remaining_seconds() <= 0:
            self.stop("run_deadline_exceeded")
            raise RunBudgetExceeded("run_deadline_exceeded")
        if self.calls_started >= self.max_calls:
            self.stop("run_call_limit_exceeded")
            raise RunBudgetExceeded("run_call_limit_exceeded")
        estimate = max(1, int(estimated_input_tokens))
        if self.input_tokens_reserved + estimate > self.max_input_tokens:
            self.stop("run_input_token_limit_exceeded")
            raise RunBudgetExceeded("run_input_token_limit_exceeded")
        output_reservation = max(0, int(requested_output_tokens))
        if (
            self.output_tokens_reserved + output_reservation
            > self.max_output_tokens
        ):
            self.stop("run_output_token_limit_exceeded")
            raise RunBudgetExceeded("run_output_token_limit_exceeded")
        self.calls_started += 1
        self.input_tokens_reserved += estimate
        self.output_tokens_reserved += output_reservation
        return self.remaining_seconds()

    def release_unstarted(
        self,
        estimated_input_tokens: int,
        requested_output_tokens: int = 0,
    ) -> None:
        """Release exactly one local reservation that never reached dispatch.

        This is deliberately narrower than a general refund.  A provider
        attempt that may have crossed the network boundary remains spent even
        when it times out or returns unusable output.  Callers may use this
        method only while they still own a pre-dispatch reservation that has
        not been copied into the durable budget scope.
        """

        estimate = max(1, int(estimated_input_tokens))
        output_reservation = max(0, int(requested_output_tokens))
        if (
            self.calls_started <= self._durable_calls_synced
            or self.input_tokens_reserved - estimate
            < self._durable_input_tokens_synced
            or self.output_tokens_reserved - output_reservation
            < self._durable_output_tokens_synced
        ):
            raise RuntimeError(
                "cannot release a missing or durably persisted reservation"
            )
        self.calls_started -= 1
        self.input_tokens_reserved -= estimate
        self.output_tokens_reserved -= output_reservation

    def expand_unstarted_output(
        self,
        current_output_tokens: int,
        required_output_tokens: int,
    ) -> None:
        """Conservatively enlarge one unpersisted call's output reservation."""

        current = max(0, int(current_output_tokens))
        required = max(0, int(required_output_tokens))
        if required < current:
            raise ValueError("required output reservation cannot shrink")
        if (
            self.calls_started <= self._durable_calls_synced
            or self.output_tokens_reserved - current
            < self._durable_output_tokens_synced
        ):
            raise RuntimeError(
                "cannot expand a missing or durably persisted reservation"
            )
        additional = required - current
        if self.output_tokens_reserved + additional > self.max_output_tokens:
            self.stop("run_output_token_limit_exceeded")
            raise RunBudgetExceeded("run_output_token_limit_exceeded")
        self.output_tokens_reserved += additional

    def stats(self) -> dict[str, int | float | str | None]:
        return {
            "max_calls": self.max_calls,
            "calls_started": self.calls_started,
            "max_input_tokens": self.max_input_tokens,
            "estimated_input_tokens": self.input_tokens_reserved,
            "max_output_tokens": self.max_output_tokens,
            "reserved_output_tokens": self.output_tokens_reserved,
            "wall_time_sec": self.wall_time_sec,
            "elapsed_sec": round(
                max(0.0, time.monotonic() - self.started_monotonic), 3
            ),
            "exhausted_reason": self.exhausted_reason,
        }


@dataclass
class BudgetStatus:
    project_id: uuid.UUID
    window_sec: int
    cap_usd: float
    spent_usd: float
    remaining_usd: float
    enabled: bool
    # ``spent_usd`` remains the reported-token lower bound used by existing
    # ROI consumers.  The configured guard uses ``budget_exposure_usd`` so a
    # dispatched provider-API attempt whose usage response was lost cannot
    # silently become a zero-dollar call.  ``None`` preserves the semantics
    # of older direct BudgetStatus construction.
    budget_exposure_usd: float | None = None
    unknown_provider_exposure_usd: float = 0.0
    unknown_provider_calls: int = 0
    unpriced_provider_calls: int = 0
    authorized_reservation_usd: float = 0.0
    # A project account is the durable policy source of truth.  A worker whose
    # environment differs must report that fact and refuse authorization; it
    # must not present the local cap as though it governed this project.
    policy_mismatch: bool = False

    @property
    def effective_spend_usd(self) -> float:
        return (
            self.spent_usd
            if self.budget_exposure_usd is None
            else self.budget_exposure_usd
        )

    @property
    def exceeded(self) -> bool:
        return self.policy_mismatch or (self.enabled and (
            self.unpriced_provider_calls > 0
            or self.effective_spend_usd >= self.cap_usd
        ))


@dataclass(frozen=True)
class ProjectBudgetExposure:
    """Known spend plus a conservative guard-only unknown reservation.

    ``known_spend_usd`` is the token-ledger/provider-cost lower bound. Unknown
    provider-API attempts retain NULL usage facts.  Their committed input
    estimate and requested output ceiling are exposed as a diagnostic proxy,
    not as provider-reported billing or a dollar upper bound: any such call
    makes the configured-cap decision indeterminate and therefore fail-closed.
    v1 has no positive subscription/billing attestation, so no attempt is
    excluded from this API-dollar policy.
    """

    known_spend_usd: float
    unknown_provider_calls: int
    unknown_provider_exposure_usd: float
    budget_exposure_usd: float
    authorized_reservation_usd: float = 0.0
    unpriced_provider_calls: int = 0


@dataclass(frozen=True, slots=True)
class ProjectDollarBudgetConfig:
    """Exact configuration consumed by the atomic reservation transaction."""

    cap_usd: Decimal
    window_sec: int
    policy_version: str = PRICE_CATALOG_POLICY_VERSION


def canonical_subscription_call_predicate() -> ColumnElement[bool]:
    """Return the v1 predicate authorized for API-dollar exclusion.

    v1 records local SDK auth hints but has no positive billing/subscription
    attestation.  Therefore no row is currently safe to exclude. Keeping this
    shared false predicate makes the budget guard, API, ROI, and report fail
    closed together until a future attested contract and live canary exist.
    """

    return false()


def canonical_price_attestation_failure_predicate() -> ColumnElement[bool]:
    """Return terminal rows that breached their immutable price contract.

    A no-refund reservation proves an invoice ceiling only for the exact
    provider route/model named by its price contract.  A provider response
    that resolves to another model (or a completed response with no model
    identity), reported token ceilings, non-standard tool input, or an
    authoritative cost above the reserved amount is therefore unpriced even
    though the original reservation is retained. STARTED/transport-failed
    rows may legitimately have no response model and remain covered by the
    requested-model reservation.
    """

    from app.models.findings import LLMCall  # local: model import cycle

    return and_(
        LLMCall.reserved_cost_usd.is_not(None),
        LLMCall.attempt_status != "started",
        or_(
            and_(
                LLMCall.resolved_model.is_not(None),
                LLMCall.resolved_model != LLMCall.requested_model,
            ),
            and_(
                LLMCall.attempt_status == "completed",
                LLMCall.resolved_model.is_(None),
            ),
            LLMCall.input_tokens > LLMCall.reserved_input_tokens,
            LLMCall.output_tokens > LLMCall.reserved_output_tokens,
            LLMCall.reported_cost_usd > LLMCall.reserved_cost_usd,
            LLMCall.tool_input_tokens > 0,
        ),
    )


def _budget_cap_usd() -> float:
    return float(_budget_cap_decimal())


def _budget_cap_decimal() -> Decimal:
    """Parse a DB-representable cap without a binary-float round trip."""

    raw = os.environ.get("MNEMOS_LLM_BUDGET_USD_PER_PROJECT")
    if raw is None:
        return Decimal(0)
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise BudgetConfigurationError(
            "MNEMOS_LLM_BUDGET_USD_PER_PROJECT must be a finite "
            "non-negative number"
        ) from None
    if not value.is_finite() or value < 0:
        raise BudgetConfigurationError(
            "MNEMOS_LLM_BUDGET_USD_PER_PROJECT must be a finite "
            "non-negative number"
        )
    # ``llm_project_budget_accounts.cap_usd`` is NUMERIC(20, 10).  Silently
    # rounding an operator's cap upward would weaken the hard bound.
    if (
        value > Decimal("9999999999.9999999999")
        or value.normalize().as_tuple().exponent < -10
    ):
        raise BudgetConfigurationError(
            "MNEMOS_LLM_BUDGET_USD_PER_PROJECT must fit NUMERIC(20,10) exactly"
        )
    return value


def _budget_window_sec() -> int:
    raw = os.environ.get("MNEMOS_LLM_BUDGET_WINDOW_SEC")
    if raw is None:
        return 86400
    try:
        value = int(raw)
    except ValueError:
        raise BudgetConfigurationError(
            "MNEMOS_LLM_BUDGET_WINDOW_SEC must be an integer >= 60"
        ) from None
    if value < 60:
        raise BudgetConfigurationError(
            "MNEMOS_LLM_BUDGET_WINDOW_SEC must be an integer >= 60"
        )
    return value


def configured_project_dollar_budget() -> ProjectDollarBudgetConfig | None:
    """Return explicit paid-dispatch authority, or ``None`` when denied.

    Production callsites pass ``require_atomic_dollar_reservation=True`` to
    the lifecycle boundary, where ``None`` blocks a new provider dispatch.
    Generic lifecycle helpers retain an explicit non-production mode for
    isolated contract tests and durable-ledger maintenance.
    """

    cap = _budget_cap_decimal()
    window = _budget_window_sec()
    if cap == 0:
        return None
    return ProjectDollarBudgetConfig(cap_usd=cap, window_sec=window)


async def project_spend(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    since: datetime | None = None,
) -> float:
    """Return known physical-call spend for the rolling window.

    The ledger is indexed by ``(project_id, generated_at)`` and records calls
    that never became a Summary, avoiding the old partial-call undercount.
    Calls whose provider usage is unknown remain absent from this reported
    lower bound; ``project_budget_exposure`` accounts for their committed
    reservation without falsifying it as actual spend.
    """
    exposure = await project_budget_exposure(
        session,
        project_id,
        since=since,
    )
    return exposure.known_spend_usd


def _project_budget_exposure_stmt(
    project_id: uuid.UUID,
    *,
    since: datetime,
):
    from app.models.findings import LLMCall  # local: model import cycle

    # A provider-API-reported dollar value takes precedence over the configured
    # flat token rate. Agent SDK ``total_cost_usd`` is only a client-side price
    # estimate, so it never authorizes a dollar-cap decision and never replaces
    # token accounting.
    subscription_call = canonical_subscription_call_predicate()
    # v1 currently attests no free/subscription call. COALESCE keeps malformed
    # metadata monetary/fail-closed too.
    monetary_call = not_(func.coalesce(subscription_call, false()))
    agent_sdk_call = LLMCall.usage_source == "agent_sdk"
    authoritative_reported_cost = and_(
        monetary_call,
        not_(agent_sdk_call),
        LLMCall.reported_cost_usd.is_not(None),
    )
    rated_tokens = case(
        (
            and_(
                monetary_call,
                not_(authoritative_reported_cost),
            ),
            LLMCall.tokens_used,
        ),
        else_=0,
    )
    cost_reserved_call = LLMCall.reserved_cost_usd.is_not(None)
    price_attestation_failed = canonical_price_attestation_failure_predicate()
    budget_rated_tokens = case(
        (
            and_(
                monetary_call,
                not_(authoritative_reported_cost),
                not_(cost_reserved_call),
            ),
            LLMCall.tokens_used,
        ),
        else_=0,
    )
    legacy_opaque_agent = and_(
        LLMCall.contract_version == 0,
        LLMCall.tokens_used.is_(None),
        or_(
            LLMCall.model_used.like("claude_code:%"),
            LLMCall.model_used.like("%:agent_sdk"),
        ),
    )
    unknown_provider = or_(
        legacy_opaque_agent,
        price_attestation_failed,
        and_(
            LLMCall.contract_version == 1,
            monetary_call,
            or_(
                agent_sdk_call,
                and_(
                    LLMCall.tokens_used.is_(None),
                    LLMCall.reported_cost_usd.is_(None),
                ),
            ),
        ),
    )
    unpriced_provider = or_(
        price_attestation_failed,
        and_(unknown_provider, not_(cost_reserved_call)),
    )
    unknown_provider_reservation = case(
        (
            and_(unknown_provider, not_(cost_reserved_call)),
            func.coalesce(LLMCall.estimated_input_tokens, 0)
            + func.coalesce(LLMCall.requested_max_output_tokens, 0),
        ),
        else_=0,
    )
    return (
        select(
            func.coalesce(func.sum(rated_tokens), 0),
            func.coalesce(
                func.sum(
                    case(
                        (
                            authoritative_reported_cost,
                            LLMCall.reported_cost_usd,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((unknown_provider, 1), else_=0)),
                0,
            ),
            func.coalesce(func.sum(unknown_provider_reservation), 0),
            func.coalesce(func.sum(LLMCall.reserved_cost_usd), 0),
            func.coalesce(
                func.sum(case((unpriced_provider, 1), else_=0)),
                0,
            ),
            func.coalesce(func.sum(budget_rated_tokens), 0),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                authoritative_reported_cost,
                                not_(cost_reserved_call),
                            ),
                            LLMCall.reported_cost_usd,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .where(LLMCall.project_id == project_id)
        .where(LLMCall.generated_at >= since)
    )


async def project_budget_exposure(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    since: datetime | None = None,
) -> ProjectBudgetExposure:
    """Compute the project cap's conservative physical-call exposure.

    Exact provider-API cost wins when present; otherwise known tokens use the
    configured flat estimate rate. Agent SDK cost and top-level usage are
    client-side/incomplete estimates, so every v1 Agent call also remains
    indeterminate. Likewise, a v1 API attempt with neither usage nor exact cost
    can have crossed the network boundary. An enabled configured-dollar guard
    blocks further calls until such a row leaves the window or is reconciled
    with authoritative billing.
    """
    if since is None:
        since = datetime.now(tz=timezone.utc) - timedelta(
            seconds=_budget_window_sec()
        )
    stmt = _project_budget_exposure_stmt(project_id, since=since)
    (
        known_tokens,
        reported_cost_usd,
        unknown_provider_calls,
        unknown_reserved_tokens,
        reserved_cost_usd,
        unpriced_provider_calls,
        budget_rated_tokens,
        budget_reported_cost_usd,
    ) = (await session.execute(stmt)).one()
    known_tokens = int(known_tokens)
    unknown_provider_calls = int(unknown_provider_calls)
    unknown_reserved_tokens = int(unknown_reserved_tokens)
    unpriced_provider_calls = int(unpriced_provider_calls)
    reported_cost = float(reported_cost_usd)
    reserved_cost = float(reserved_cost_usd)
    budget_reported_cost = float(budget_reported_cost_usd)
    known_spend = reported_cost + tokens_to_usd(known_tokens)
    return ProjectBudgetExposure(
        known_spend_usd=known_spend,
        unknown_provider_calls=unknown_provider_calls,
        unknown_provider_exposure_usd=tokens_to_usd(unknown_reserved_tokens),
        budget_exposure_usd=(
            reserved_cost
            + budget_reported_cost
            + tokens_to_usd(
                int(budget_rated_tokens) + unknown_reserved_tokens
            )
        ),
        authorized_reservation_usd=reserved_cost,
        unpriced_provider_calls=unpriced_provider_calls,
    )


async def check_budget(
    session: AsyncSession, project_id: uuid.UUID,
) -> BudgetStatus:
    """Compute the project's current budget status. Returns a
    ``BudgetStatus`` describing remaining headroom (the caller decides
    whether to raise).
    """
    from app.models.findings import LLMProjectBudgetAccount

    # Parse the local policy even when an account already exists. Invalid
    # worker configuration is never silently replaced by database state.
    local_policy = configured_project_dollar_budget()
    account = await session.get(LLMProjectBudgetAccount, project_id)
    if account is not None:
        cap_decimal = Decimal(account.cap_usd)
        cap = float(cap_decimal)
        win = int(account.window_sec)
        policy_mismatch = bool(
            local_policy is None
            or account.policy_version != local_policy.policy_version
            or cap_decimal != local_policy.cap_usd
            or win != local_policy.window_sec
        )
    else:
        cap_decimal = local_policy.cap_usd if local_policy is not None else Decimal(0)
        cap = float(cap_decimal)
        win = (
            local_policy.window_sec
            if local_policy is not None
            else _budget_window_sec()
        )
        policy_mismatch = False

    # Once a project account exists, its persisted window/cap own reporting
    # even on a stale worker.  With neither local nor durable policy, avoid a
    # growing SUM over history on every optional call.
    exposure = (
        await project_budget_exposure(
            session,
            project_id,
            since=datetime.now(tz=timezone.utc) - timedelta(seconds=win),
        )
        if account is not None or cap > 0
        else ProjectBudgetExposure(0.0, 0, 0.0, 0.0)
    )
    return BudgetStatus(
        project_id=project_id,
        window_sec=win,
        cap_usd=cap,
        spent_usd=exposure.known_spend_usd,
        remaining_usd=(
            0.0
            if exposure.unpriced_provider_calls > 0
            else max(0.0, cap - exposure.budget_exposure_usd)
        ),
        enabled=cap > 0,
        budget_exposure_usd=exposure.budget_exposure_usd,
        unknown_provider_exposure_usd=(
            exposure.unknown_provider_exposure_usd
        ),
        unknown_provider_calls=exposure.unknown_provider_calls,
        unpriced_provider_calls=exposure.unpriced_provider_calls,
        authorized_reservation_usd=exposure.authorized_reservation_usd,
        policy_mismatch=policy_mismatch,
    )


async def require_budget(
    session: AsyncSession, project_id: uuid.UUID,
) -> BudgetStatus:
    """Guard helper for extractor callsites — raises ``BudgetExceeded``
    when the project is over cap."""
    status = await check_budget(session, project_id)
    if status.policy_mismatch:
        raise BudgetConfigurationError(
            f"project {project_id} has a durable dollar policy that does not "
            "match this worker configuration"
        )
    if status.exceeded:
        if status.unpriced_provider_calls > 0:
            raise BudgetExceeded(
                f"project {project_id} has "
                f"{status.unpriced_provider_calls} unpriced call(s) with "
                "unknown usage/cost in the budget window; configured-dollar "
                f"authorization is indeterminate (known: "
                f"${status.spent_usd:.2f}, reservation proxy: "
                f"${status.unknown_provider_exposure_usd:.2f}, "
                f"cap: ${status.cap_usd:.2f})"
            )
        raise BudgetExceeded(
            f"project {project_id} has ${status.effective_spend_usd:.2f} "
            f"budget exposure (${status.spent_usd:.2f} known, "
            f"${status.unknown_provider_exposure_usd:.2f} unknown-provider) "
            f"in the last {status.window_sec}s "
            f"(cap: ${status.cap_usd:.2f})"
        )
    return status
