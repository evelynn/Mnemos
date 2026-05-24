"""PR-101 — cap the ``details`` payload audit rows receive.

The MCP dispatch audits each tool call with
``details={"arguments": arguments}``. An agent passing tens of KB
of arguments would write that into ``audit_log.details`` JSONB —
on a hot, append-only table this bloats fast. Spec §14.4 wants
audit to scale to "every operation". This PR caps the payload.

When details serialises larger than the cap (default 4 KiB,
overridable via ``MNEMOS_AUDIT_DETAILS_CAP``) the row stores a
marker with the original byte count and the top-level keys, so an
investigator still knows the call happened and can probe further
via other signals.
"""

from __future__ import annotations



def test_small_details_pass_through_unchanged():
    from app.audit.logger import _cap_details

    payload = {"k": "v", "n": 7}
    assert _cap_details(payload) == payload


def test_oversized_details_replaced_with_marker():
    from app.audit.logger import _cap_details

    big = {"arguments": {"blob": "x" * 8000}}
    out = _cap_details(big)
    assert out is not None
    assert out["truncated"] is True
    assert out["original_bytes"] > 4096
    # Top-level keys preserved.
    assert "arguments" in out["keys"]


def test_none_details_stay_none():
    from app.audit.logger import _cap_details

    assert _cap_details(None) is None


def test_non_serialisable_details_get_marker():
    """A payload with bytes / a function reference can't go to JSONB;
    the cap must short-circuit cleanly."""
    from app.audit.logger import _cap_details

    out = _cap_details({"fn": object()})
    assert out is not None
    assert out.get("truncated") is True


def test_env_override(monkeypatch):
    """MNEMOS_AUDIT_DETAILS_CAP lifts or tightens the cap."""
    from app.audit.logger import _cap_details

    monkeypatch.setenv("MNEMOS_AUDIT_DETAILS_CAP", "100")
    payload = {"x": "y" * 200}
    out = _cap_details(payload)
    assert out is not None
    assert out["truncated"] is True
    assert out["cap_bytes"] == 100


def test_record_pipes_details_through_cap():
    """``record(details=...)`` runs the cap on its way to the
    AuditLog row. Source-level guard against a future refactor that
    drops the call."""
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1] / "app" / "audit" / "logger.py"
    ).read_text(encoding="utf-8")
    idx = body.find("entry = AuditLog(")
    slab = body[idx:idx + 400]
    assert "details=_cap_details(details)" in slab
