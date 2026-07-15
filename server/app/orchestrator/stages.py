"""Per-stage progress tracker.

Wraps a single ``analysis_stages`` row for the lifetime of one pipeline
step: creates the row in ``running`` status, records incremental progress,
and closes it with ``completed`` / ``partial`` / ``failed`` while publishing
SSE events so the Analysis tab can live-update.

Usage::

    async with StageTracker(bus, run_id, project_id, "symbols",
                            language="csharp", position=3,
                            time_budget_sec=600) as stage:
        async for rec in analyzer.run("symbols", path):
            ...
            await stage.increment()
        stage.set_stats({"symbols": totals["symbols"]})

If the ``with`` block raises, the stage is closed as ``failed`` with the
exception text; budget timeouts raise :class:`StageBudgetExceeded` and the
stage closes as ``partial``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update

from app.db import SessionLocal
from app.models.stages import AnalysisStage
from app.orchestrator.progress import ProgressBus

_MAX_STAGE_ERROR_CHARS = 2048


def _bounded_stage_error(value: Any) -> str:
    text = str(value)
    if len(text) <= _MAX_STAGE_ERROR_CHARS:
        return text
    return text[: _MAX_STAGE_ERROR_CHARS - 16] + "\n...[truncated]"


class StageBudgetExceeded(Exception):
    """Raised when a stage exceeds its wall-clock budget."""


class StageTracker:
    def __init__(
        self,
        bus: ProgressBus,
        run_id: uuid.UUID,
        project_id: uuid.UUID,
        name: str,
        *,
        language: str | None = None,
        position: int = 0,
        items_total: int = 0,
        time_budget_sec: int | None = None,
        suppress_budget_exceeded: bool = True,
    ) -> None:
        self._bus = bus
        self._run_id = run_id
        self._project_id = project_id
        self._name = name
        self._language = language
        self._position = position
        self._items_total = items_total
        self._items_done = 0
        self._budget = time_budget_sec
        self._suppress_budget_exceeded = suppress_budget_exceeded
        self._started_at: float | None = None
        self._stats: dict[str, Any] = {}
        self._stage_id: uuid.UUID | None = None
        self._publish_every = 25
        self._last_publish = 0
        self._partial_reason: str | None = None

    async def __aenter__(self) -> "StageTracker":
        async with SessionLocal() as session:
            stage = AnalysisStage(
                run_id=self._run_id,
                project_id=self._project_id,
                name=self._name,
                language=self._language,
                position=self._position,
                items_total=self._items_total,
                status="running",
                time_budget_sec=self._budget,
                started_at=datetime.now(tz=timezone.utc),
            )
            session.add(stage)
            await session.commit()
            await session.refresh(stage)
            self._stage_id = stage.id
        self._started_at = time.monotonic()
        await self._bus.publish(
            self._run_id,
            {
                "event": "stage_started",
                "stage_id": str(self._stage_id),
                "name": self._name,
                "language": self._language,
                "position": self._position,
                "items_total": self._items_total,
            },
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and (
            issubclass(exc_type, asyncio.CancelledError)
            or exc_type.__name__ == "AnalysisCancelled"
        ):
            await self._close("cancelled", error_log="analysis_cancelled")
            await self._bus.publish(
                self._run_id,
                {
                    "event": "stage_cancelled",
                    "stage_id": str(self._stage_id),
                    "name": self._name,
                    "items_done": self._items_done,
                    "items_total": self._items_total,
                },
            )
            return False
        if exc_type is StageBudgetExceeded:
            bounded_error = _bounded_stage_error(exc)
            await self._close("partial", error_log=bounded_error)
            await self._bus.publish(
                self._run_id,
                {
                    "event": "stage_partial",
                    "stage_id": str(self._stage_id),
                    "name": self._name,
                    "items_done": self._items_done,
                    "items_total": self._items_total,
                    "stats": self._stats,
                    "reason": "budget_exceeded",
                },
            )
            return self._suppress_budget_exceeded
        if exc_type is not None:
            bounded_error = _bounded_stage_error(exc)
            await self._close("failed", error_log=bounded_error)
            await self._bus.publish(
                self._run_id,
                {
                    "event": "stage_failed",
                    "stage_id": str(self._stage_id),
                    "name": self._name,
                    "error": bounded_error,
                },
            )
            return False
        if self._partial_reason is not None:
            bounded_reason = _bounded_stage_error(self._partial_reason)
            await self._close("partial", error_log=bounded_reason)
            await self._bus.publish(
                self._run_id,
                {
                    "event": "stage_partial",
                    "stage_id": str(self._stage_id),
                    "name": self._name,
                    "items_done": self._items_done,
                    "items_total": self._items_total,
                    "stats": self._stats,
                    "reason": bounded_reason,
                },
            )
            return False
        await self._close("completed")
        await self._bus.publish(
            self._run_id,
            {
                "event": "stage_done",
                "stage_id": str(self._stage_id),
                "name": self._name,
                "items_done": self._items_done,
                "items_total": self._items_total,
                "stats": self._stats,
                "duration_ms": int((time.monotonic() - (self._started_at or 0)) * 1000),
            },
        )
        return False

    def set_total(self, total: int) -> None:
        self._items_total = total

    def set_stats(self, stats: dict[str, Any]) -> None:
        self._stats = dict(stats)

    def mark_partial(self, reason: str) -> None:
        """Close an otherwise non-throwing stage as explicitly incomplete."""

        self._partial_reason = reason

    async def increment(self, amount: int = 1) -> None:
        self._items_done += amount
        self._check_budget()
        if self._items_done - self._last_publish >= self._publish_every:
            await self._flush()
            self._last_publish = self._items_done

    def _check_budget(self) -> None:
        if self._budget is None or self._started_at is None:
            return
        if time.monotonic() - self._started_at > self._budget:
            raise StageBudgetExceeded(
                f"stage {self._name} exceeded {self._budget}s budget"
            )

    async def _flush(self) -> None:
        if self._stage_id is None:
            return
        async with SessionLocal() as session:
            await session.execute(
                update(AnalysisStage)
                .where(AnalysisStage.id == self._stage_id)
                .values(items_done=self._items_done, stats=self._stats or None)
            )
            await session.commit()
        await self._bus.publish(
            self._run_id,
            {
                "event": "stage_progress",
                "stage_id": str(self._stage_id),
                "name": self._name,
                "items_done": self._items_done,
                "items_total": self._items_total,
            },
        )

    async def _close(self, status: str, error_log: str | None = None) -> None:
        if self._stage_id is None:
            return
        async with SessionLocal() as session:
            await session.execute(
                update(AnalysisStage)
                .where(AnalysisStage.id == self._stage_id)
                .values(
                    status=status,
                    items_done=self._items_done,
                    stats=self._stats or None,
                    completed_at=datetime.now(tz=timezone.utc),
                    error_log=error_log,
                )
            )
            await session.commit()
