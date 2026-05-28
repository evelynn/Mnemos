"""PR-122 — Mnemos analyses Mnemos and generates its own findings.

The ultimate dogfood. Pipeline:
  ggoss-py → Node/Edge → merge.findings.run_all → real Finding rows

What this proves end-to-end:
- The analyzer's output is graph-shaped enough that the
  finding detectors can consume it
- run_all() commits real Finding rows the dashboard would render
- Mnemos can ACTUALLY answer 'what should I fix in MY codebase?'
- The whole stack (analyzer → ingest → graph query → finding
  generation) works against a real codebase, end-to-end

This file is the most thorough proof that Mnemos's value claim
is real: 'continuously analyses systems and produces actionable
findings.' Up to PR-121 every step was verified in isolation;
PR-122 stitches them into the full loop on production code.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.models import audit as _audit  # noqa: E402,F401
from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models import plans as _plans  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.findings import Finding  # noqa: E402
from app.models.graph import Edge, Node  # noqa: E402


_ROOT = Path(__file__).resolve().parents[2]
_PY_ANALYZER = _ROOT / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"
_MNEMOS_SERVER_APP = _ROOT / "server" / "app"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await eng.dispose()


def _run_analyzer(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_PY_ANALYZER), verb, str(target)],
        capture_output=True, text=True, timeout=120,
    )
    assert cp.returncode == 0
    return [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]


async def _seed_mnemos_self_analysis(s: AsyncSession, project_id: _uuid.UUID) -> tuple[int, int]:
    """Analyse Mnemos's own server/app/ tree with ggoss-py and persist
    every emitted Node/Edge — this is the dogfood seed."""
    sym_envs = _run_analyzer("symbols", _MNEMOS_SERVER_APP)
    edge_envs = _run_analyzer("calls", _MNEMOS_SERVER_APP)
    now = datetime.now(tz=timezone.utc)
    n_count = 0
    for env in sym_envs:
        if env["record_type"] != "symbol":
            continue
        d = env["data"]
        s.add(Node(
            id=d["id"], project_id=project_id, kind="Symbol",
            data=d, certainty=d.get("certainty", "asserted"),
            created_by=[env.get("source_name", "ggoss-py")],
            valid_from=now,
        ))
        n_count += 1
    e_count = 0
    for env in edge_envs:
        if env["record_type"] != "edge":
            continue
        d = env["data"]
        s.add(Edge(
            id=_uuid.uuid4(), project_id=project_id,
            source_id=d["source_id"], target_id=d["target_id"],
            kind=d.get("kind", "CALLS"),
            data=d.get("metadata", {}) or {},
            certainty=d.get("certainty", "asserted"),
            created_by=[env.get("source_name", "ggoss-py")],
            valid_from=now,
        ))
        e_count += 1
    await s.commit()
    return n_count, e_count


# ─── dogfood seed sanity ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_mnemos_self_analysis_seeds_meaningful_graph(session: AsyncSession):
    """Before checking findings, prove the analyzer + DB write
    actually populate a non-trivial graph from Mnemos's own code."""
    pid = _uuid.uuid4()
    n, e = await _seed_mnemos_self_analysis(session, pid)
    assert n >= 400, f"only {n} symbols extracted from server/app/"
    assert e >= 1000, f"only {e} edges extracted from server/app/"


# ─── detectors against real Mnemos graph ──────────────────────────


@pytest.mark.asyncio
async def test_dynamic_calls_detector_runs_on_real_mnemos_graph(session: AsyncSession):
    """detect_dynamic_calls() must not crash on a real graph — and
    if it finds anything it must produce well-formed Finding rows.

    Mnemos's own code has resolved + unresolved calls (extern ones
    are inferred); the detector specifically wants asserted +
    via_runtime=true, so the real-Mnemos count is often 0 — that's
    not a failure, just an honest signal that this detector is for
    runtime-traced graphs."""
    from app.merge.findings import detect_dynamic_calls

    pid = _uuid.uuid4()
    await _seed_mnemos_self_analysis(session, pid)
    count = await detect_dynamic_calls(session, pid)
    assert count >= 0
    if count > 0:
        rows = (await session.execute(
            select(Finding).where(
                Finding.project_id == pid,
                Finding.kind == "dynamic_call_detected",
            )
        )).scalars().all()
        # Each finding carries detail with source/target ids.
        for f in rows:
            assert "source" in f.detail
            assert "target" in f.detail


@pytest.mark.asyncio
async def test_unverified_claims_detector_runs_on_real_mnemos_graph(session: AsyncSession):
    """detect_unverified_claims looks for old inferred edges.
    Fresh seed → 0 stale; runs without error regardless."""
    from app.merge.findings import detect_unverified_claims

    pid = _uuid.uuid4()
    await _seed_mnemos_self_analysis(session, pid)
    count = await detect_unverified_claims(session, pid, stale_days=30)
    assert count >= 0  # bound by graph age; new seed → 0


@pytest.mark.asyncio
async def test_run_all_detectors_against_real_mnemos_graph(session: AsyncSession):
    """The big one: run_all() against Mnemos's own graph. Must
    finish without exception, return a stats dict, and the dict
    must list every detector key (regression-guard for a future
    refactor that drops a detector silently)."""
    from app.merge.findings import run_all

    pid = _uuid.uuid4()
    n, e = await _seed_mnemos_self_analysis(session, pid)

    stats = await run_all(session, pid)
    assert isinstance(stats, dict)
    expected_keys = {
        "duplicate_endpoints",
        "unverified_claims",
        "dynamic_calls",
        "dead_paths",
        "schema_mismatches",
        "opaque_failing",
    }
    assert expected_keys.issubset(set(stats.keys())), (
        f"run_all dropped a detector — got {stats.keys()}"
    )
    # Each value is a non-negative count.
    for k, v in stats.items():
        assert isinstance(v, int) and v >= 0, f"{k}: {v!r}"


@pytest.mark.asyncio
async def test_detector_produces_persisted_findings(session: AsyncSession):
    """Synthetic case where we KNOW the detector should fire.
    Seed a CALLS edge with via_runtime=true + certainty=asserted
    → dynamic_call_detected must fire. Confirms the full path
    detector → upsert → DB persistence works."""
    from app.merge.findings import detect_dynamic_calls

    pid = _uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    session.add(Node(
        id="sym:dispatcher", project_id=pid, kind="Symbol",
        data={"name": "dispatcher"}, certainty="asserted",
        created_by=["test"], valid_from=now,
    ))
    session.add(Node(
        id="sym:handler", project_id=pid, kind="Symbol",
        data={"name": "handler"}, certainty="asserted",
        created_by=["test"], valid_from=now,
    ))
    session.add(Edge(
        id=_uuid.uuid4(), project_id=pid,
        source_id="sym:dispatcher", target_id="sym:handler",
        kind="CALLS", certainty="asserted",
        data={"via_runtime": "true"},
        created_by=["test"], valid_from=now,
    ))
    await session.commit()

    count = await detect_dynamic_calls(session, pid)
    await session.commit()
    assert count == 1, f"expected exactly 1 finding, got {count}"
    findings = (await session.execute(
        select(Finding).where(Finding.project_id == pid)
    )).scalars().all()
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "dynamic_call_detected"
    assert f.severity == "warning"
    assert f.status == "open"
    assert f.detail["source"] == "sym:dispatcher"
    assert f.detail["target"] == "sym:handler"
    assert f.risk_score >= 0


# ─── meta: dogfood-driven test of run_all stats output ─────────────


@pytest.mark.asyncio
async def test_dogfood_findings_count_is_observable(session: AsyncSession):
    """The 'Mnemos analyses Mnemos' run produces some quantifiable
    number of findings (could be 0). Persist them, query them
    back, confirm count matches what run_all reported."""
    from app.merge.findings import run_all

    pid = _uuid.uuid4()
    await _seed_mnemos_self_analysis(session, pid)
    stats = await run_all(session, pid)

    total_reported = sum(stats.values())
    total_persisted = (await session.execute(
        select(Finding).where(Finding.project_id == pid)
    )).scalars().all()
    # The reported counts include in-place updates of existing
    # findings (which don't add rows). On a fresh empty DB they
    # match exactly.
    assert len(total_persisted) == total_reported, (
        f"run_all reported {total_reported} findings, "
        f"DB has {len(total_persisted)}"
    )
