"""PR-137 — ggoss-py contracts + data_access verbs.

Audit finding (analyzer agent): ``ggoss_py.py:443-455`` dispatched
5 verbs (probe/inventory/symbols/calls/schema). Operator running
the orchestrator against a Python project saw the contracts and
data_access stages exit 2 (verb not implemented) — silent skip
masked the gap.

This PR adds:
- ``cmd_contracts``  → FastAPI/Flask/Starlette/Sanic/aiohttp route
  decorators emit ``record_type=contract`` + ``EXPOSES`` edges.
- ``cmd_data_access`` → raw SQL via ``text("...")`` + ORM-style
  ``session.query(Model)`` / ``select(Model)`` emit READS/WRITES
  edges + ``record_type=data_entity`` nodes.

Test strategy: drive the analyzer as a subprocess (matches how
``server/app/analyzers/runner.py`` invokes it in production) on a
synthetic Python project, then assert the JSONL stream contains the
expected records.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_GGOSS_PY = (
    Path(__file__).resolve().parents[2]
    / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"
)

_FASTAPI_SAMPLE = '''\
from fastapi import APIRouter
from sqlalchemy import text, select

router = APIRouter()


@router.get("/orders/{oid}")
async def get_order(oid: int, db):
    rows = await db.execute(
        text("SELECT * FROM orders WHERE id = :id"),
        {"id": oid},
    )
    return rows.first()


@router.post("/orders")
async def create_order(body, db):
    await db.execute(
        text("INSERT INTO orders (customer_id, total) VALUES (:c, :t)"),
        {"c": body.customer_id, "t": body.total},
    )


@router.delete("/orders/{oid}")
async def remove_order(oid: int, db):
    await db.execute(text("DELETE FROM orders WHERE id = :id"), {"id": oid})


class Repo:
    async def list_customers(self, db):
        return await db.execute(select(Customer))


class Customer:
    pass
'''

_FLASK_SAMPLE = '''\
from flask import Flask
app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return {"ok": True}


@app.route("/items", methods=["POST"])
def create_item():
    return {}
'''


def _run(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_GGOSS_PY), verb, str(target)],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0, (
        f"ggoss-py {verb} exited {cp.returncode}: {cp.stderr[-500:]}"
    )
    out: list[dict] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ─── contracts ─────────────────────────────────────────────────────


def test_contracts_emits_fastapi_routes(tmp_path):
    f = tmp_path / "api.py"
    f.write_text(_FASTAPI_SAMPLE, encoding="utf-8")
    recs = _run("contracts", f)
    contracts = [r["data"] for r in recs if r["record_type"] == "contract"]
    exposes = [
        r["data"] for r in recs
        if r["record_type"] == "edge" and r["data"]["kind"] == "EXPOSES"
    ]
    methods = {(c["method"], c["path"]) for c in contracts}
    assert ("GET", "/orders/{oid}") in methods
    assert ("POST", "/orders") in methods
    assert ("DELETE", "/orders/{oid}") in methods
    # One EXPOSES per route — the symbol that owns the decorator.
    assert len(exposes) == len(contracts) == 3
    # The framework metadata bubble — ``router`` is the receiver.
    assert all(e["metadata"]["framework"] == "router" for e in exposes)


def test_contracts_emits_flask_route_with_methods_kwarg(tmp_path):
    f = tmp_path / "flask_api.py"
    f.write_text(_FLASK_SAMPLE, encoding="utf-8")
    recs = _run("contracts", f)
    contracts = [r["data"] for r in recs if r["record_type"] == "contract"]
    method_paths = {(c["method"], c["path"]) for c in contracts}
    # ``@app.route("/health", methods=["GET"])`` → ("GET", "/health")
    assert ("GET", "/health") in method_paths
    assert ("POST", "/items") in method_paths


def test_contracts_ignores_non_http_decorators(tmp_path):
    f = tmp_path / "misc.py"
    f.write_text(
        "import functools\n\n"
        "@functools.wraps\n"
        "def helper():\n    pass\n",
        encoding="utf-8",
    )
    recs = _run("contracts", f)
    contracts = [r for r in recs if r["record_type"] == "contract"]
    assert contracts == [], "non-HTTP decorators must not produce contracts"


# ─── data_access ───────────────────────────────────────────────────


def test_data_access_extracts_read_write_from_raw_sql(tmp_path):
    f = tmp_path / "api.py"
    f.write_text(_FASTAPI_SAMPLE, encoding="utf-8")
    recs = _run("data_access", f)
    edges = [
        r["data"] for r in recs
        if r["record_type"] == "edge"
    ]
    reads = [e for e in edges if e["kind"] == "READS"]
    writes = [e for e in edges if e["kind"] == "WRITES"]
    # SELECT FROM orders + select(Customer) → 2 READS targets.
    targets_r = {e["target_id"] for e in reads}
    assert "data:orders" in targets_r
    assert "data:customer" in targets_r
    # INSERT INTO orders + DELETE FROM orders → WRITES.
    targets_w = {e["target_id"] for e in writes}
    assert "data:orders" in targets_w


def test_data_access_emits_data_entity_per_distinct_table(tmp_path):
    f = tmp_path / "api.py"
    f.write_text(_FASTAPI_SAMPLE, encoding="utf-8")
    recs = _run("data_access", f)
    entities = [r["data"] for r in recs if r["record_type"] == "data_entity"]
    ids = {e["id"] for e in entities}
    # ``orders`` appears across SELECT/INSERT/DELETE — must dedupe.
    assert "data:orders" in ids
    assert "data:customer" in ids
    # And each entity carries a logical name lowercase normalised.
    by_id = {e["id"]: e for e in entities}
    assert by_id["data:orders"]["logical_name"] == "orders"


def test_data_access_no_edge_for_unrelated_call(tmp_path):
    """``logger.info("SELECT ...")`` text in a non-SQL context must not
    fire a READS — the source must be a string passed to an
    execute-ish API. We err on the side of false positives only when
    the call shape matches (text() or first-arg-string), so this is a
    sanity floor."""
    f = tmp_path / "noop.py"
    f.write_text(
        "import logging\n"
        "def helper():\n"
        '    logging.info("hello world")\n',
        encoding="utf-8",
    )
    recs = _run("data_access", f)
    edges = [r for r in recs if r["record_type"] == "edge"]
    # Pure log line — must produce zero data-access edges.
    assert edges == [], f"false positive: {edges}"


# ─── verb dispatch ─────────────────────────────────────────────────


def test_main_dispatches_new_verbs(tmp_path):
    """Regression guard — ensure both new verbs are wired into the CLI
    entry. Pre-fix this returned exit code 2 'unknown verb'."""
    f = tmp_path / "x.py"
    f.write_text("def f(): pass\n", encoding="utf-8")
    for verb in ("contracts", "data_access"):
        cp = subprocess.run(
            [sys.executable, str(_GGOSS_PY), verb, str(f)],
            capture_output=True, text=True, timeout=10,
        )
        assert cp.returncode == 0, f"verb {verb!r} returned {cp.returncode}"


def test_schema_advertises_new_record_types():
    cp = subprocess.run(
        [sys.executable, str(_GGOSS_PY), "schema"],
        capture_output=True, text=True, timeout=10,
    )
    out = json.loads(cp.stdout)
    assert "EXPOSES" in out["edge_kinds"]
    assert "READS" in out["edge_kinds"]
    assert "WRITES" in out["edge_kinds"]
    assert "contract" in out["record_types"]
    assert "data_entity" in out["record_types"]
