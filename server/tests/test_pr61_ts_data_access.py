"""PR-61 — TypeScript analyzer ``data_access`` extraction (core-value B).

A critical reassessment scored 솔루션 분석 (solution analysis) the
lowest objective area: the TS analyzer's ``data_access`` command was
a clean stub ("ORM integration is Week-5 work"), so the TS half of a
polyglot TS+C# system contributed no DataEntity nodes and no READS /
WRITES data-flow edges. This PR implements ``cmdDataAccess`` — raw-SQL
table extraction plus ORM-method detection (Prisma ``client.model.op``
and capitalised model statics) — and teaches the orchestrator's verb
gate to accept the DataEntity nodes the command now emits.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_ANALYZER = (
    Path(__file__).resolve().parents[2]
    / "analyzers" / "ggoss-ts" / "src" / "index.mjs"
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Static — the stub is gone
# ---------------------------------------------------------------------------


def test_data_access_no_longer_a_stub():
    body = _read(_ANALYZER)
    assert "Week-5 work" not in body
    assert "leave the stub cleanly empty" not in body
    # The real implementation walks the program and emits records.
    idx = body.find("function cmdDataAccess(")
    slab = body[idx:idx + 3800]
    assert "buildProgram(target)" in slab
    assert "emitDataAccess(" in slab


def test_data_access_detects_raw_sql_and_orm():
    body = _read(_ANALYZER)
    assert "_SQL_TABLE" in body
    assert "_DATA_READ_OPS" in body
    assert "_DATA_WRITE_OPS" in body
    # Prisma client.<model>.<verb>() and capitalised model statics.
    assert "PropertyAccessExpression" in body


def test_schema_declares_data_access_records():
    body = _read(_ANALYZER)
    idx = body.find("function cmdSchema(")
    slab = body[idx:idx + 400]
    assert '"data_entity"' in slab
    assert '"READS"' in slab
    assert '"WRITES"' in slab


# ---------------------------------------------------------------------------
# Orchestrator — the verb gate accepts the new DataEntity records
# ---------------------------------------------------------------------------


def test_verb_gate_accepts_data_entity_from_data_access():
    from app.orchestrator.jobs import _VERB_ACCEPT

    assert "data_entity" in _VERB_ACCEPT["data_access"]
    assert "edge" in _VERB_ACCEPT["data_access"]


@pytest.mark.asyncio
async def test_data_access_records_flow_through_to_upserts():
    """A data_entity + READS edge from the data_access verb must land
    as one node upsert and one edge upsert."""
    from app.orchestrator.jobs import _VERB_ACCEPT, _record_payload

    captured: dict[str, list[dict]] = {"nodes": [], "edges": []}

    async def fake_upsert_node(session, **kw):
        captured["nodes"].append(kw)

    async def fake_upsert_edge(session, **kw):
        captured["edges"].append(kw)

    records = [
        {
            "record_type": "data_entity",
            "data": {"id": "data.orders", "kind": "table", "name": "orders"},
            "source_name": "ggoss-ts",
        },
        {
            "record_type": "edge",
            "data": {
                "source_id": "ts:orders.ts:listOrders@3:1",
                "target_id": "data.orders",
                "kind": "READS",
            },
            "source_name": "ggoss-ts",
        },
    ]
    totals = {"symbols": 0, "contracts": 0, "edges": 0}
    with patch("app.merge.writer.upsert_node", new=fake_upsert_node), \
         patch("app.merge.writer.upsert_edge", new=fake_upsert_edge):
        for rec in records:
            await _record_payload(
                session=AsyncMock(),
                project_id=uuid.uuid4(),
                payload=rec,
                accept_kinds=_VERB_ACCEPT["data_access"],
                totals=totals,
            )

    assert len(captured["nodes"]) == 1
    assert captured["nodes"][0]["kind"] == "DataEntity"
    assert len(captured["edges"]) == 1
    assert captured["edges"][0]["kind"] == "READS"


# ---------------------------------------------------------------------------
# End-to-end — run the real analyzer over a fixture
# ---------------------------------------------------------------------------


def _node_with_typescript() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    probe = subprocess.run(
        [node, "-e", "require('typescript')"],
        cwd=str(_ANALYZER.parent.parent),
        capture_output=True,
    )
    return node if probe.returncode == 0 else None


def test_data_access_end_to_end(tmp_path):
    node = _node_with_typescript()
    if node is None:
        pytest.skip("node + typescript not available")

    src = tmp_path / "orders.ts"
    src.write_text(
        textwrap.dedent(
            """
            import { prisma } from "./db";

            export async function listOrders() {
              return prisma.order.findMany();
            }

            export async function purge(db: any) {
              await db.query("DELETE FROM audit_log WHERE old = 1");
            }
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node, str(_ANALYZER), "data_access", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    recs = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
    edges = [r["data"] for r in recs if r["record_type"] == "edge"]
    entities = {
        r["data"]["id"] for r in recs if r["record_type"] == "data_entity"
    }

    # Prisma findMany → READS data.order.
    assert "data.order" in entities
    assert any(e["kind"] == "READS" and e["target_id"] == "data.order"
               for e in edges)
    # Raw SQL DELETE → WRITES data.audit_log, and NOT a spurious READS
    # (the DELETE FROM must not also trip the READS "FROM" rule).
    assert "data.audit_log" in entities
    audit_edges = [e for e in edges if e["target_id"] == "data.audit_log"]
    assert audit_edges
    assert all(e["kind"] == "WRITES" for e in audit_edges)
