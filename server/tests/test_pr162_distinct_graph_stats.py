"""PR-162 — run.stats reports the distinct current graph, not ingest-record counts.

A dogfood run of Mnemos on a real Next.js app reported 66 data entities / 63
contracts where the true distinct graph held 34 / 47 — because ``run.stats``
carried ``totals`` (one tick per ingested record), so any entity referenced
from several sites was double-counted. ``_graph_inventory`` recomputes the
headline from the distinct current nodes/edges (``valid_to IS NULL``).

This pins the helper: ingesting the same data-entity three times leaves one
*current* node (plus superseded history), and the inventory must report 1.
"""

from __future__ import annotations

import os
import uuid as _uuid

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr162")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()

from app.models.base import Base  # noqa: E402
from app.models import audit as _audit  # noqa: E402,F401
from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models import projects as _proj  # noqa: E402,F401
from app.models.graph import Node  # noqa: E402
from app.orchestrator.jobs import _graph_inventory, _record_payload  # noqa: E402


@pytest.mark.asyncio
async def test_run_stats_counts_distinct_current_graph():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    pid = _uuid.uuid4()

    async with Sess() as s:
        totals = {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0, "errors": 0}

        async def ingest(payload, accept):
            await _record_payload(s, project_id=pid, payload=payload, accept_kinds=accept, totals=totals)

        # Same data entity referenced from THREE sites -> 3 ingest records.
        for _ in range(3):
            await ingest(
                {"record_type": "data_entity", "source_name": "ggoss-ts",
                 "data": {"id": "data.orders", "name": "orders", "certainty": "verified"}},
                {"data_entity"},
            )
        # One symbol, one edge.
        await ingest(
            {"record_type": "symbol", "source_name": "ggoss-ts",
             "data": {"id": "ts:f.ts:foo@1:1", "name": "foo"}},
            {"symbol"},
        )
        await ingest(
            {"record_type": "edge", "source_name": "ggoss-ts",
             "data": {"source_id": "ts:f.ts:foo@1:1", "target_id": "data.orders", "kind": "READS"}},
            {"edge"},
        )
        await s.commit()

        # Raw ingest counters double-count the entity (this is what run.stats
        # used to report).
        assert totals["data_entities"] == 3

        # Semantic no-op ingestion no longer creates bitemporal churn for
        # identical payloads.  All three ingest attempts therefore resolve to
        # the same current row.
        total_rows = (await s.execute(
            select(func.count()).where(Node.project_id == pid, Node.kind == "DataEntity"))).scalar()
        current_rows = (await s.execute(
            select(func.count()).where(Node.project_id == pid, Node.kind == "DataEntity",
                                       Node.valid_to.is_(None)))).scalar()
        assert total_rows == 1 and current_rows == 1

        # The fix: headline = distinct current graph.
        inv = await _graph_inventory(s, pid)
        assert inv["data_entities"] == 1, f"expected 1 distinct data entity, got {inv}"
        assert inv["symbols"] == 1
        assert inv["edges"] == 1
        assert inv["contracts"] == 0

    await eng.dispose()
