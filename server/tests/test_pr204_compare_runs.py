"""PR-204 — bitemporal graph diff across two analysis runs (surpass cbm).

codebase-memory-mcp re-indexes without keeping history, so it cannot compare
graph states across commits. Mnemos's bitemporal graph makes an as-of snapshot
a query, so a run-to-run diff is two snapshots subtracted. These tests pin the
added / removed / modified classification, edge deltas, and new findings.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr204")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.mcp.queries import compare_runs  # noqa: E402
from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.findings import Finding  # noqa: E402
from app.models.graph import AnalysisRun, Edge, Node  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_T_A = _BASE + timedelta(seconds=10)
_T_MID = _BASE + timedelta(seconds=20)
_T_ADD = _BASE + timedelta(seconds=25)
_T_B = _BASE + timedelta(seconds=30)


@pytest.fixture()
async def seeded():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        pid = uuid.uuid4()
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        s.add_all([
            AnalysisRun(id=run_a, project_id=pid, status="completed",
                        triggered_by="t", git_sha="aaa", scope="full",
                        started_at=_T_A, completed_at=_T_A),
            AnalysisRun(id=run_b, project_id=pid, status="completed",
                        triggered_by="t", git_sha="bbb", scope="full",
                        started_at=_T_B, completed_at=_T_B),
        ])

        def node(nid, vfrom, vto, name="n"):
            return Node(id=nid, project_id=pid, kind="Symbol",
                        data={"name": name}, certainty="asserted",
                        created_by=["t"], valid_from=vfrom, valid_to=vto)

        s.add_all([
            node("sym:keep", _BASE, None),                 # live at both
            node("sym:added", _T_ADD, None),               # only at B → added
            node("sym:removed", _BASE, _T_ADD),            # only at A → removed
            node("sym:modified", _BASE, _T_MID),           # old version …
            node("sym:modified", _T_MID, None),            # … new version → modified
        ])
        s.add(Edge(id=uuid.uuid4(), project_id=pid, source_id="sym:keep",
                   target_id="sym:added", kind="CALLS", data={},
                   certainty="asserted", created_by=["t"],
                   valid_from=_T_ADD, valid_to=None))      # edge added
        s.add(Finding(id=uuid.uuid4(), project_id=pid, kind="schema_mismatch",
                      severity="warning", status="open",
                      subject_node_id="sym:added", detail={}, risk_score=50,
                      first_seen_at=_T_ADD, last_seen_at=_T_B))  # new finding
        await s.commit()
        yield s, pid, run_a, run_b
    await eng.dispose()


@pytest.mark.asyncio
async def test_symbol_add_remove_modify_classified(seeded):
    s, pid, run_a, run_b = seeded
    diff = await compare_runs(s, project_id=pid, run_a_id=run_a, run_b_id=run_b)

    sym = diff["symbols"]
    added = {x["id"] for x in sym["added"]}
    removed = {x["id"] for x in sym["removed"]}
    modified = {x["id"] for x in sym["modified"]}
    assert added == {"sym:added"}
    assert removed == {"sym:removed"}
    assert modified == {"sym:modified"}
    assert "sym:keep" not in (added | removed | modified)


@pytest.mark.asyncio
async def test_order_independent_before_is_older(seeded):
    """Passing the runs in either order gives the same 'before=older' diff."""
    s, pid, run_a, run_b = seeded
    d1 = await compare_runs(s, project_id=pid, run_a_id=run_a, run_b_id=run_b)
    d2 = await compare_runs(s, project_id=pid, run_a_id=run_b, run_b_id=run_a)
    assert d1["before"]["git_sha"] == d2["before"]["git_sha"] == "aaa"
    assert d1["summary"]["symbols_added"] == d2["summary"]["symbols_added"] == 1


@pytest.mark.asyncio
async def test_edge_delta_and_new_findings(seeded):
    s, pid, run_a, run_b = seeded
    diff = await compare_runs(s, project_id=pid, run_a_id=run_a, run_b_id=run_b)
    assert diff["edges"].get("CALLS", {}).get("added") == 1
    assert diff["summary"]["edges_added"] == 1
    assert diff["new_findings_count"] == 1
    assert diff["new_findings"][0]["kind"] == "schema_mismatch"


@pytest.mark.asyncio
async def test_missing_run_errors(seeded):
    s, pid, run_a, _run_b = seeded
    diff = await compare_runs(s, project_id=pid, run_a_id=run_a,
                              run_b_id=uuid.uuid4())
    assert diff.get("error") == "run_not_found"
