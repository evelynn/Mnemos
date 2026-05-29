"""PR-93 — close the small open items from the platform review:

* ``findings/rebuild`` POST was org-member-only — cheap DoS. Now
  requires ``operator``.
* spec §13.2 ``GET /mcp_sessions`` + ``GET /mcp_sessions/{actor}/requests``
  were absent — the platform reviewer flagged both. The data already
  lives in ``audit_log`` rows tagged ``actor='claude_code:mcp'`` and
  ``action='mcp.tool.*'``; this PR adds the read surface that
  groups them per actor with first / last / count.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_findings_rebuild_requires_operator():
    body = _read(_APP / "api" / "findings.py")
    idx = body.find("async def rebuild_findings(")
    decorator = body[max(0, idx - 280):idx]
    assert "Depends(require_operator)" in decorator
    # Org check stays — defence in depth.
    assert "require_project_org" in decorator


def test_mcp_sessions_endpoint_defined():
    body = _read(_APP / "api" / "audit.py")
    assert "/mcp_sessions" in body
    assert "async def list_mcp_sessions(" in body
    # Drill-down endpoint too.
    assert "/mcp_sessions/{actor}/requests" in body
    assert "async def mcp_session_requests(" in body


def test_mcp_sessions_router_registered():
    body = _read(_APP / "main.py")
    assert "audit_api.mcp_router" in body


def test_mcp_sessions_groups_audit_log():
    body = _read(_APP / "api" / "audit.py")
    idx = body.find("async def list_mcp_sessions(")
    slab = body[idx:idx + 1400]
    assert 'AuditLog.action.like("mcp.tool.%")' in slab
    # Grouped per actor with min/max occurred_at and count.
    assert "group_by(AuditLog.actor)" in slab
    assert "min(AuditLog.occurred_at)" in slab
    assert "max(AuditLog.occurred_at)" in slab
