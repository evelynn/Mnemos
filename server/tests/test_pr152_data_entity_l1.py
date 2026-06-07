"""PR-152 — DataEntity (table) L1 summaries.

PR-142's honest limit: tables landed in the graph with columns/PII/FKs but
were never LLM-summarised (_priority_symbols is Symbol-only), so "what does
table X hold / who touches it?" had no narrative. summarise_l1 now also
summarises the busiest DataEntity nodes via _priority_data_entities (ranked
by incoming READS/WRITES/REFERENCES degree).
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr152")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


@pytest.mark.asyncio
async def test_priority_data_entities_ranks_by_incoming_degree():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.extractor.runner import _priority_data_entities
    from app.models.base import Base
    from app.models.graph import Edge, Node
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    pid = uuid.uuid4()
    async with AsyncSession(eng, expire_on_commit=False) as s:
        s.add_all([
            Node(id="data:orders", project_id=pid, kind="DataEntity",
                 data={"name": "orders"}, certainty="inferred", created_by="t"),
            Node(id="data:audit_log", project_id=pid, kind="DataEntity",
                 data={"name": "audit_log"}, certainty="inferred", created_by="t"),
            Node(id="py:a::f", project_id=pid, kind="Symbol",
                 data={"name": "f"}, certainty="inferred", created_by="t"),
            Node(id="py:b::g", project_id=pid, kind="Symbol",
                 data={"name": "g"}, certainty="inferred", created_by="t"),
        ])
        # orders is touched twice, audit_log once → orders ranks first.
        s.add_all([
            Edge(project_id=pid, source_id="py:a::f", target_id="data:orders",
                 kind="WRITES", data={}, certainty="inferred", created_by="t"),
            Edge(project_id=pid, source_id="py:b::g", target_id="data:orders",
                 kind="READS", data={}, certainty="inferred", created_by="t"),
            Edge(project_id=pid, source_id="py:a::f", target_id="data:audit_log",
                 kind="WRITES", data={}, certainty="inferred", created_by="t"),
        ])
        await s.commit()

        ranked = await _priority_data_entities(s, pid, 5)
        ids = [n.id for n in ranked]
        assert ids[0] == "data:orders", f"busiest table first, got {ids}"
        assert set(ids) == {"data:orders", "data:audit_log"}
        # symbols are not pulled into the data-entity pass
        assert all(n.kind == "DataEntity" for n in ranked)

        assert await _priority_data_entities(s, pid, 0) == []
