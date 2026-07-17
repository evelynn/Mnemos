"""PR-204 — bitemporal graph diff across two analysis runs (surpass cbm).

codebase-memory-mcp re-indexes without keeping history, so it cannot compare
graph states across commits. Mnemos's bitemporal graph makes an as-of snapshot
a query, so a run-to-run diff is two snapshots subtracted. These tests pin the
atomic publication receipt, added / removed / modified classification, and
edge deltas. Finding history is explicitly unavailable until it is run-linked.
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

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.mcp.queries import compare_runs  # noqa: E402
from app.merge.writer import upsert_node  # noqa: E402
from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun, Edge, Node  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_T_A = _BASE + timedelta(seconds=10)
_T_MID = _BASE + timedelta(seconds=20)
_T_ADD = _BASE + timedelta(seconds=25)
_T_B = _BASE + timedelta(seconds=30)


def _published_run(
    *,
    run_id: uuid.UUID,
    project_id: uuid.UUID,
    git_sha: str,
    published_at: datetime,
    base_generation: int,
    scope: str = "full",
) -> AnalysisRun:
    sealed_at = published_at - timedelta(seconds=1)
    sources = ["t"]
    return AnalysisRun(
        id=run_id,
        project_id=project_id,
        status="completed",
        triggered_by="t",
        git_sha=git_sha,
        scope=scope,
        started_at=sealed_at,
        completed_at=published_at,
        **published_run_fields(
            generation=base_generation + 1,
            published_at=published_at,
            authoritative_sources=sources,
            coverage_sealed_at=sealed_at,
        ),
    )


@pytest.fixture()
async def seeded():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        pid = uuid.uuid4()
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        s.add_all(
            [
                _published_run(
                    run_id=run_a,
                    project_id=pid,
                    git_sha="aaa",
                    published_at=_T_A,
                    base_generation=0,
                ),
                _published_run(
                    run_id=run_b,
                    project_id=pid,
                    git_sha="bbb",
                    published_at=_T_B,
                    base_generation=1,
                ),
            ]
        )

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
        # sym:keep called the modified symbol in the BEFORE graph → blast radius.
        s.add(Edge(id=uuid.uuid4(), project_id=pid, source_id="sym:keep",
                   target_id="sym:modified", kind="CALLS", data={},
                   certainty="asserted", created_by=["t"],
                   valid_from=_BASE, valid_to=None))
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
async def test_edge_delta_and_findings_fail_closed_without_run_history(seeded):
    s, pid, run_a, run_b = seeded
    diff = await compare_runs(s, project_id=pid, run_a_id=run_a, run_b_id=run_b)
    assert diff["edges"].get("CALLS", {}).get("added") == 1
    assert diff["summary"]["edges_added"] == 1
    assert diff["new_findings_count"] == 0
    assert diff["new_findings"] == []
    assert diff["findings_delta"]["status"] == "unavailable"
    assert diff["summary"]["new_findings"] is None


@pytest.mark.asyncio
async def test_change_impact_shows_affected_callers(seeded):
    """The analysis+comparison fusion: who called a changed symbol (blast
    radius) — what a reviewer must re-check. cbm cannot produce this."""
    s, pid, run_a, run_b = seeded
    diff = await compare_runs(s, project_id=pid, run_a_id=run_a, run_b_id=run_b)
    impact = {c["changed_symbol"]: c for c in diff["change_impact"]}
    assert "sym:modified" in impact
    assert "sym:keep" in impact["sym:modified"]["affected_callers"]
    assert impact["sym:modified"]["change_kind"] == "modified"
    assert diff["summary"]["impacted_callers"] >= 1


@pytest.mark.asyncio
async def test_missing_run_errors(seeded):
    s, pid, run_a, _run_b = seeded
    diff = await compare_runs(s, project_id=pid, run_a_id=run_a,
                              run_b_id=uuid.uuid4())
    assert diff.get("error") == "run_not_found"


@pytest.mark.parametrize("status", ["queued", "running", "published", "failed", "cancelled"])
async def test_nonterminal_or_failed_run_is_not_a_snapshot(seeded, status):
    s, pid, run_a, run_b = seeded
    row = await s.get(AnalysisRun, run_b)
    row.status = status
    await s.commit()

    diff = await compare_runs(s, project_id=pid, run_a_id=run_a, run_b_id=run_b)

    assert diff["error"] == "run_not_published"
    assert diff["rejected_runs"] == [
        {"run_id": str(run_b), "reason": "run_not_terminal_publication"}
    ]
    # The public response is deliberately canonical: persisted status details
    # belong to internal diagnosis and must not be reflected to MCP callers.
    assert status not in repr(diff["rejected_runs"])


@pytest.mark.parametrize(
    ("mutation", "internal_detail"),
    [
        (lambda run: setattr(run, "stats", {}), "graph_publication receipt is missing"),
        (
            lambda run: run.stats["graph_publication"].update(atomic=False),
            "graph_publication receipt is not atomic",
        ),
        (
            lambda run: run.stats["graph_publication"].update(generation=999),
            "publication generation does not match the persisted base",
        ),
        (
            lambda run: run.stats["graph_publication"].update(generation=True),
            "publication generation does not match the persisted base",
        ),
        (
            lambda run: run.stats["graph_publication"].update(
                authoritative_sources=["other"]
            ),
            "receipt authoritative sources do not match the run",
        ),
        (
            lambda run: run.stats["graph_publication"].update(
                published_at="not-a-timestamp"
            ),
            "publication timestamp is invalid",
        ),
        (
            lambda run: run.stats["graph_publication"].update(
                published_at="2026-01-01T00:00:30"
            ),
            "publication timestamp must be timezone-aware",
        ),
        (
            lambda run: run.stats["graph_publication"]["counts"].update(
                nodes_inserted=True
            ),
            "publication receipt promotion counts must be real integers",
        ),
    ],
)
async def test_malformed_publication_receipt_fails_closed(
    seeded, mutation, internal_detail
):
    s, pid, run_a, run_b = seeded
    row = await s.get(AnalysisRun, run_b)
    # JSON columns do not track nested mutation portably; assign a fresh copy.
    row.stats = {
        **row.stats,
        "graph_publication": dict(row.stats["graph_publication"]),
    }
    mutation(row)
    row.stats = dict(row.stats)
    await s.commit()

    diff = await compare_runs(s, project_id=pid, run_a_id=run_a, run_b_id=run_b)

    assert diff["error"] == "run_not_published"
    assert diff["rejected_runs"] == [
        {"run_id": str(run_b), "reason": "run_graph_publication_invalid"}
    ]
    # GraphPublicationInvariantError messages may describe stored graph state.
    # Only the stable public reason code may cross this privacy boundary.
    assert internal_detail not in repr(diff["rejected_runs"])


@pytest.mark.asyncio
async def test_identical_reingest_does_not_create_false_modified_diff():
    """A repeated analyzer fact at a later run is not a source-code change."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        pid = uuid.uuid4()
        await upsert_node(
            s, project_id=pid, node_id="sym:stable", kind="Symbol",
            data={"name": "stable", "file": "stable.py", "line": 3},
            certainty="asserted", source_name="ggoss-py",
        )
        await s.commit()
        first_publication = datetime.now(tz=timezone.utc)
        run_a = _published_run(
            run_id=uuid.uuid4(),
            project_id=pid,
            git_sha="same",
            published_at=first_publication,
            base_generation=0,
        )
        s.add(run_a)
        await s.commit()

        await upsert_node(
            s, project_id=pid, node_id="sym:stable", kind="Symbol",
            data={"line": 3, "file": "stable.py", "name": "stable"},
            certainty="asserted", source_name="ggoss-py",
        )
        await s.commit()
        second_publication = datetime.now(tz=timezone.utc)
        run_b = _published_run(
            run_id=uuid.uuid4(),
            project_id=pid,
            git_sha="same",
            published_at=second_publication,
            base_generation=1,
            scope="incremental",
        )
        s.add(run_b)
        await s.commit()

        row_count = (await s.execute(
            select(func.count()).select_from(Node).where(Node.project_id == pid)
        )).scalar_one()
        assert row_count == 1
        diff = await compare_runs(
            s, project_id=pid, run_a_id=run_a.id, run_b_id=run_b.id,
        )
        assert diff["symbols"]["modified_count"] == 0
        assert diff["summary"]["symbols_modified"] == 0
    await eng.dispose()
