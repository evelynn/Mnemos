import json
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models.audit import AuditLog

log = logging.getLogger(__name__)

# Hard cap on the serialised ``details`` JSON. MCP tool calls can pass
# tens of KB of args; the audit table is hot, so a few thousand bytes
# is plenty and anything bigger is replaced with a marker pointing at
# the original size. Override with MNEMOS_AUDIT_DETAILS_CAP if needed.
_DETAILS_CAP_BYTES = 4096


def _cap_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """Replace an oversized ``details`` payload with a small marker so
    the audit row stays compact. The original size is preserved so an
    operator looking at the trace knows the call really did pass that
    much data."""
    if details is None:
        return None
    import os

    try:
        cap = int(os.environ.get("MNEMOS_AUDIT_DETAILS_CAP", "")) or _DETAILS_CAP_BYTES
    except ValueError:
        cap = _DETAILS_CAP_BYTES
    try:
        # No ``default=str`` — JSONB only accepts pure JSON types, so a
        # payload that needs coercion is a contract bug; surface it as
        # a marker rather than silently stringifying via default=str.
        serialised = json.dumps(details)
    except (TypeError, ValueError):
        return {"truncated": True, "reason": "not_json_serialisable"}
    if len(serialised.encode("utf-8")) <= cap:
        return details
    return {
        "truncated": True,
        "original_bytes": len(serialised.encode("utf-8")),
        "cap_bytes": cap,
        # Keep just the top-level keys — they're usually the agent's
        # tool-name + small scalars; bulky inputs ride inside.
        "keys": list(details.keys()) if isinstance(details, dict) else None,
    }


async def record(
    *,
    actor: str,
    action: str,
    target: str | None = None,
    project_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Append an audit log entry. Never raises into the caller path —
    but never silent either: a failed audit insert is itself audited
    via ``logger.error`` so an operator's log pipeline still sees the
    gap. Spec §14.4 + §11.6 — "append-only, mandatory"; a swallowed
    write breaks both promises.
    """
    entry = AuditLog(
        actor=actor,
        action=action,
        target=target,
        project_id=project_id,
        details=_cap_details(details),
    )
    if session is not None:
        # Caller-managed transaction: ride it. If the caller's commit
        # later rolls back, the audit row rolls back with it — that
        # is intentional (the action didn't happen, no audit either).
        session.add(entry)
        try:
            await session.flush()
        except Exception:
            log.exception(
                "audit.write_failed (caller session) actor=%s action=%s target=%s",
                actor, action, target,
            )
            raise
        return

    async with SessionLocal() as db:
        try:
            db.add(entry)
            await db.commit()
        except Exception:
            await db.rollback()
            # Loud failure path — visible in centralised logs and
            # Prometheus (the caller's request still succeeds, but the
            # operator can see the audit-gap rate).
            log.exception(
                "audit.write_failed actor=%s action=%s target=%s",
                actor, action, target,
            )
            try:
                from app.obs.metrics import audit_write_failures_total

                audit_write_failures_total.labels(action=action).inc()
            except Exception:  # noqa: BLE001
                # The metric counter is best-effort; never block the
                # caller because the metric module didn't import.
                pass
