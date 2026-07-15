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
2. Sums per-project spend over a window from the physical ``LLMCall`` ledger
   (including map/reduce partials and rejected responses).
3. Implements ``BudgetGuard.check_budget(project_id)`` — raises
   ``BudgetExceeded`` when the project's running spend in the
   configured window crosses the per-project cap. Callers
   (extractor runner) catch and fall back to the stub path
   (visible via PR-138 ``stub:budget_exceeded`` model_used label) so
   the system degrades gracefully instead of stopping.

Configuration (env)
-------------------
- ``MNEMOS_LLM_USD_PER_MTOK`` (float, default 3.0) — Sonnet-class
  rate. Operators on a smaller / cheaper model can drop this.
- ``MNEMOS_LLM_BUDGET_USD_PER_PROJECT`` (float, default 0 = disabled)
  — when > 0, sum of spend over the rolling window must stay below
  this to permit a new LLM call.
- ``MNEMOS_LLM_BUDGET_WINDOW_SEC`` (int, default 86400 = 24h).

Failure mode
------------
When the budget is exceeded the extractor records a
``BudgetExceeded`` event and the result falls through to the stub
path with ``fallback_reason="budget_exceeded"``. Both
``mnemos_llm_fallback_total`` (from PR-138) and an audit-log entry
make the event visible.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


# ─── single source of truth for the rate ─────────────────────────


def rate_usd_per_mtok() -> float:
    """Return the configured cost rate.

    Kept as a function (not a module constant) so test cases that
    monkey-patch the env see the new value without reload."""
    try:
        return float(os.environ.get("MNEMOS_LLM_USD_PER_MTOK", "3.0"))
    except ValueError:
        log.warning("invalid MNEMOS_LLM_USD_PER_MTOK; falling back to 3.0")
        return 3.0


def tokens_to_usd(tokens: int | None) -> float:
    """Convert a token count to USD using the configured rate.

    ``None`` (agent-SDK path returns no count) is treated as 0 — the
    operator's billing comes from their Claude Code subscription, not
    Anthropic API charges. This honest accounting prevents the ROI
    panel from double-counting subscription usage.
    """
    if not tokens or tokens <= 0:
        return 0.0
    return round((tokens / 1_000_000.0) * rate_usd_per_mtok(), 4)


# ─── budget enforcement ──────────────────────────────────────────


class BudgetExceeded(Exception):
    """Raised when a project has spent above its configured cap in
    the rolling window. Callers should catch and fall back to the
    stub path so the pipeline keeps moving."""


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
    wall_time_sec: int = 600
    started_monotonic: float = field(default_factory=time.monotonic)
    calls_started: int = 0
    input_tokens_reserved: int = 0
    exhausted_reason: str | None = None

    @classmethod
    def from_env(cls) -> "LLMRunBudget":
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
            wall_time_sec=_bounded_env_int(
                "MNEMOS_LLM_WALL_TIME_SEC", 600, minimum=30, maximum=7_200
            ),
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

    def reserve(self, estimated_input_tokens: int) -> float:
        """Reserve one logical/physical provider attempt or fail closed."""

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
        self.calls_started += 1
        self.input_tokens_reserved += estimate
        return self.remaining_seconds()

    def stats(self) -> dict[str, int | float | str | None]:
        return {
            "max_calls": self.max_calls,
            "calls_started": self.calls_started,
            "max_input_tokens": self.max_input_tokens,
            "estimated_input_tokens": self.input_tokens_reserved,
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

    @property
    def exceeded(self) -> bool:
        return self.enabled and self.spent_usd >= self.cap_usd


def _budget_cap_usd() -> float:
    try:
        return max(0.0, float(os.environ.get(
            "MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0"
        )))
    except ValueError:
        return 0.0


def _budget_window_sec() -> int:
    try:
        return max(60, int(os.environ.get(
            "MNEMOS_LLM_BUDGET_WINDOW_SEC", "86400"
        )))
    except ValueError:
        return 86400


async def project_spend(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    since: datetime | None = None,
) -> float:
    """Sum physical-call tokens for the project within the rolling window.

    The ledger is indexed by ``(project_id, generated_at)`` and records calls
    that never became a Summary, avoiding the old partial-call undercount.
    """
    from app.models.findings import LLMCall  # local: model import cycle

    if since is None:
        since = datetime.now(tz=timezone.utc) - timedelta(
            seconds=_budget_window_sec()
        )
    stmt = (
        select(func.coalesce(func.sum(LLMCall.tokens_used), 0))
        .where(LLMCall.project_id == project_id)
        .where(LLMCall.generated_at >= since)
    )
    total_tokens = int((await session.execute(stmt)).scalar_one())
    return tokens_to_usd(total_tokens)


async def check_budget(
    session: AsyncSession, project_id: uuid.UUID,
) -> BudgetStatus:
    """Compute the project's current budget status. Returns a
    ``BudgetStatus`` describing remaining headroom (the caller decides
    whether to raise).
    """
    cap = _budget_cap_usd()
    win = _budget_window_sec()
    # Disabled means disabled: avoid a growing SUM over summary history on
    # every optional LLM call when no monetary guard was configured.
    spent = await project_spend(session, project_id) if cap > 0 else 0.0
    return BudgetStatus(
        project_id=project_id,
        window_sec=win,
        cap_usd=cap,
        spent_usd=spent,
        remaining_usd=max(0.0, cap - spent),
        enabled=cap > 0,
    )


async def require_budget(
    session: AsyncSession, project_id: uuid.UUID,
) -> BudgetStatus:
    """Guard helper for extractor callsites — raises ``BudgetExceeded``
    when the project is over cap."""
    status = await check_budget(session, project_id)
    if status.exceeded:
        raise BudgetExceeded(
            f"project {project_id} spent ${status.spent_usd:.2f} "
            f"in the last {status.window_sec}s "
            f"(cap: ${status.cap_usd:.2f})"
        )
    return status
