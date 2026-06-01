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
2. Sums per-project spend over a window (DB-backed via the existing
   ``Summary.tokens_used`` column).
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
import uuid
from dataclasses import dataclass
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
    """Sum ``Summary.tokens_used`` for the project's summaries within
    the rolling window, then convert to USD. Quick (single SELECT
    over an indexed column) — safe to call on every extractor invocation.
    """
    from app.models.findings import Summary  # local: model import cycle

    if since is None:
        since = datetime.now(tz=timezone.utc) - timedelta(
            seconds=_budget_window_sec()
        )
    stmt = (
        select(func.coalesce(func.sum(Summary.tokens_used), 0))
        .where(Summary.project_id == project_id)
        .where(Summary.generated_at >= since)
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
    spent = await project_spend(session, project_id)
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
