"""PR-154 — cross-file CALLS linking for agent-extracted code.

Agent code extraction is per-file, so CALLS edges only connected symbols
within one file — find_callers/callees were blind across files. Each
Claude-extracted Symbol now carries data.calls_out (callee names, possibly
cross-file); link_inferred_calls resolves each to a unique Symbol by name
and creates a CALLS edge (unambiguous matches only).
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr154")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


def test_to_envelopes_carries_calls_out():
    from app.extractor.agent_extract import to_envelopes

    envs = to_envelopes("python", "a.py", {
        "symbols": [{"id": "py:a.py::f", "name": "f", "calls": ["helper", "g"]}],
        "edges": [], "data_access": [],
    })
    sym = next(e for e in envs if e["record_type"] == "symbol")
    assert sym["data"]["calls_out"] == ["helper", "g"]


@pytest.mark.asyncio
async def test_link_inferred_calls_resolves_cross_file_unambiguous():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    # Register every model so create_all resolves cross-table FKs.
    from app.models import (  # noqa: F401
        audit, auth, comments, findings, graph, onboarding,
        organization, plans, projects, runtime, samples, stages,
    )
    from app.models.base import Base
    from app.models.graph import Edge, Node
    from app.orchestrator.jobs import link_inferred_calls
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    pid = uuid.uuid4()
    async with AsyncSession(eng, expire_on_commit=False) as s:
        s.add_all([
            # caller in file a invokes "helper" (file b) and "ambig" (2 defs)
            Node(id="py:a.py::run", project_id=pid, kind="Symbol", created_by="t",
                 data={"name": "run", "file": "a.py", "calls_out": ["helper", "ambig", "missing"]},
                 certainty="inferred"),
            Node(id="py:b.py::helper", project_id=pid, kind="Symbol", created_by="t",
                 data={"name": "helper", "file": "b.py"}, certainty="inferred"),
            Node(id="py:c.py::ambig", project_id=pid, kind="Symbol", created_by="t",
                 data={"name": "ambig", "file": "c.py"}, certainty="inferred"),
            Node(id="py:d.py::ambig", project_id=pid, kind="Symbol", created_by="t",
                 data={"name": "ambig", "file": "d.py"}, certainty="inferred"),
        ])
        await s.commit()

        totals = {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0}
        linked = await link_inferred_calls(s, pid, totals)
        await s.commit()

        edges = (await s.execute(select(Edge).where(Edge.project_id == pid))).scalars().all()

    # only the unambiguous cross-file call (run -> helper) is linked;
    # "ambig" (2 defs) and "missing" (0 defs) are skipped for precision.
    assert linked == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.source_id == "py:a.py::run" and e.target_id == "py:b.py::helper"
    assert e.kind == "CALLS" and e.certainty == "inferred"


# imported lazily so the module-level env is set first
from sqlalchemy import select  # noqa: E402
