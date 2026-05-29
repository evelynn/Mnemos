"""PR-91 — spec §13.2 GET /api/v1/projects/{id}/data_query_log.

Platform review flagged this as a spec-mandated endpoint that simply
didn't exist. The ``DataQueryLog`` rows have been written since
the query verb landed; this PR adds the read surface so an operator
can audit who ran which SQL against a ProjectDB.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_endpoint_path_matches_spec():
    body = _read(_APP / "api" / "data.py")
    assert '"/api/v1/projects/{project_id}/data_query_log"' in body
    assert "async def list_data_query_log(" in body


def test_endpoint_org_scoped():
    body = _read(_APP / "api" / "data.py")
    idx = body.find("query_log_router = APIRouter(")
    slab = body[idx:idx + 400]
    assert "require_project_org" in slab


def test_endpoint_orders_newest_first():
    body = _read(_APP / "api" / "data.py")
    idx = body.find("async def list_data_query_log(")
    slab = body[idx:idx + 1400]
    assert "DataQueryLog.executed_at.desc()" in slab


def test_router_registered_in_main():
    body = _read(_APP / "main.py")
    assert "data_api.query_log_router" in body
