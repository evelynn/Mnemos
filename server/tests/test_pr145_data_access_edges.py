"""PR-145 — agent code-extraction also emits function→table data-access
edges, so get_data_access can answer "what DB does this touch?" for
agent-extracted (analyzer-less / docker-free) code.

Before this, the Q&A verification found get_data_access returned empty
for a Python handler that clearly INSERTs into orders/order_items — the
code extraction only produced CALLS/CONTAINS, never READS/WRITES.
"""

from __future__ import annotations

import os

os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr145")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


def test_code_data_access_becomes_reads_writes_edges_to_data_entities():
    from app.extractor.agent_extract import to_envelopes

    extracted = {
        "symbols": [{"id": "python:h.py::handle", "name": "handle", "kind": "function"}],
        "edges": [],
        "data_access": [
            {"symbol_id": "python:h.py::handle", "table": "orders", "access": "WRITES"},
            {"symbol_id": "python:h.py::handle", "table": "data:customers", "access": "READS"},
            {"symbol_id": "ghost::x", "table": "orders", "access": "READS"},  # dropped
        ],
    }
    envs = to_envelopes("python", "h.py", extracted)
    edges = [e for e in envs if e["record_type"] == "edge"]

    kinds = {(e["data"]["kind"], e["data"]["target_id"]) for e in edges}
    assert ("WRITES", "data:orders") in kinds, "bare table name -> data: id"
    assert ("READS", "data:customers") in kinds, "already-prefixed id kept"
    # data-access edge from a symbol not extracted in this file is dropped
    assert all(e["data"]["source_id"] == "python:h.py::handle" for e in edges)
    assert all(e["data"]["certainty"] == "inferred" for e in edges)


def test_get_data_access_queries_reads_writes_edges():
    """The READS/WRITES edge kinds the extraction emits are exactly what
    get_data_access filters on — keeps producer and consumer in sync."""
    import inspect

    from app.mcp import queries

    src = inspect.getsource(queries.get_data_access)
    assert '"READS"' in src and '"WRITES"' in src


def test_db_language_extraction_has_no_data_access_key():
    """DB (sql) extraction emits DataEntity nodes, not function data-access
    edges — to_envelopes must not invent data_access edges for it."""
    from app.extractor.agent_extract import to_envelopes

    envs = to_envelopes(
        "sql",
        "schema.sql",
        {"entities": [{"id": "data:orders", "name": "orders", "kind": "table"}], "edges": []},
    )
    assert all(e["record_type"] == "data_entity" for e in envs)
