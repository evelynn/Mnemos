"""PR-163 — SQL system catalogs / pseudo-tables are not domain data entities.

A dogfood run on a real Next.js app (whose datasource feature introspects
information_schema / sqlite_master and whose UPDATE..SET statements mis-parse
to a table named ``set``) extracted sqlite_master, pg_namespace, the Oracle
data dictionary, DUAL and ``set`` as DataEntity nodes — noise in the data map.
``_record_payload`` now drops those nodes and the READS/WRITES edges to them.
"""

from __future__ import annotations

import os
import uuid as _uuid

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr163")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from sqlalchemy import select  # noqa: E402
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
from app.models.graph import Edge, Node  # noqa: E402
from app.orchestrator.jobs import _is_system_data_entity, _record_payload  # noqa: E402


def test_is_system_data_entity_helper():
    for n in ["set", "dual", "sqlite_master", "SQLITE_SEQUENCE", "pg_namespace",
              "schemata", "user_tables", "user_tab_columns", "v$session", "dba_tables"]:
        assert _is_system_data_entity(n) is True, n
    # domain tables — and intentionally-ambiguous bare words — are kept
    for n in ["users", "reports", "dx_ingest_jobs", "orders", "billing_events",
              "tables", "columns", "views", "", None]:
        assert _is_system_data_entity(n) is False, n


@pytest.mark.asyncio
async def test_system_entities_and_their_edges_are_dropped():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    pid = _uuid.uuid4()

    async with Sess() as s:
        totals = {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0, "errors": 0}

        async def ingest(payload, accept):
            await _record_payload(s, project_id=pid, payload=payload, accept_kinds=accept, totals=totals)

        for name in ("orders", "sqlite_master", "set", "pg_namespace"):
            await ingest({"record_type": "data_entity", "source_name": "ggoss-ts",
                          "data": {"id": f"data.{name}", "name": name}}, {"data_entity"})
        for name in ("orders", "sqlite_master"):
            await ingest({"record_type": "edge", "source_name": "ggoss-ts",
                          "data": {"source_id": "ts:f.ts:h@1:1", "target_id": f"data.{name}",
                                   "kind": "READS"}}, {"edge"})
        await s.commit()

        names = sorted(
            (n.data or {}).get("name")
            for n in (await s.execute(
                select(Node).where(Node.project_id == pid, Node.kind == "DataEntity"))).scalars().all()
        )
        assert names == ["orders"], f"system catalogs should be dropped, got {names}"

        edges = (await s.execute(select(Edge).where(Edge.project_id == pid))).scalars().all()
        assert len(edges) == 1 and edges[0].target_id == "data.orders", \
            "READS edge to sqlite_master must be dropped"

    await eng.dispose()
