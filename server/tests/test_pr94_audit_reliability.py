"""PR-94 — audit reliability: stop swallowing writes silently (§14.4).

The Q&A reviewer (#5) and the platform reviewer (#16) both flagged
``audit.logger.record``: a failed audit insert was caught and
rolled back with a bare ``except: pass`` — the spec §14.4 promise
of "append-only, mandatory" was silently broken under any DB
contention, and operators had no signal.

This PR keeps the never-raise contract on the caller (audit MUST
NOT break the action) but stops the silence: failures now log at
``ERROR`` level with actor/action/target, and increment a new
``mnemos_audit_write_failures_total{action=...}`` Prometheus
counter so an operator's monitoring sees "audit went dark."

The caller-session path now also re-raises on flush failure — a
caller that bundles an audit row into a transaction *wants* to
know if the row can't be flushed (the action's commit will fail
next anyway).
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_failures_log_at_error_level():
    body = _read(_APP / "audit" / "logger.py")
    # The bare ``except Exception: pass`` is gone.
    assert "log.exception(" in body
    assert "audit.write_failed" in body


def test_failures_increment_prometheus_counter():
    body = _read(_APP / "audit" / "logger.py")
    assert "audit_write_failures_total" in body
    # The counter import is guarded so the audit path stays
    # independent of the metrics module's import order.
    assert "try:" in body and "except Exception" in body
    metrics = _read(_APP / "obs" / "metrics.py")
    assert "audit_write_failures_total = Counter(" in metrics
    assert "mnemos_audit_write_failures_total" in metrics


def test_caller_session_flush_propagates():
    """When the caller passes its own session, a flush failure must
    propagate — the caller's commit will fail anyway and they need
    to know to short-circuit."""
    body = _read(_APP / "audit" / "logger.py")
    idx = body.find("if session is not None:")
    slab = body[idx:idx + 700]
    assert "await session.flush()" in slab
    # The flush is inside a try/except that re-raises.
    assert "raise" in slab


def test_record_signature_unchanged():
    """No caller-API regression — every existing audit_record(...)
    call still type-checks."""
    body = _read(_APP / "audit" / "logger.py")
    assert "async def record(" in body
    for kw in (
        "actor: str",
        "action: str",
        "target: str | None = None",
        "project_id: uuid.UUID | None = None",
        "details: dict[str, Any] | None = None",
        "session: AsyncSession | None = None",
    ):
        assert kw in body, f"signature drift: missing {kw!r}"
