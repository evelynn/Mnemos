"""PR-142 — Claude-Code extraction of DB schema from SQL/DDL files.

DB knowledge previously entered the graph only via (a) the source
analyzers' data_access verb or (b) the mssql/oracle live_schema path
(real connection, those two dialects only). DB info handed over as
plain SQL/DDL files — any dialect — had no path in and was invisible to
the analysis. PR-142 routes SQL files through the same Claude-Code
agent stage, emitting DataEntity nodes (tables) + REFERENCES edges.

Deterministic tests (no live LLM): the SQL language must be agent-
eligible and DB-typed, and the envelope conversion must produce
DataEntity records the standard ingest accepts.
"""

from __future__ import annotations

import os

os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr142")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


def test_sql_is_agent_eligible_db_language_with_no_file_analyzer():
    from app.analyzers.registry import binary_for
    from app.extractor.agent_extract import (
        AGENT_DB_LANGUAGES,
        AGENT_LANGUAGE_EXTENSIONS,
    )

    # "sql" (file-based DDL) routes to the agent stage; mssql/oracle keep
    # their deterministic live_schema analyzers.
    assert binary_for("sql") is None
    assert "sql" in AGENT_LANGUAGE_EXTENSIONS
    assert "sql" in AGENT_DB_LANGUAGES
    assert ".sql" in AGENT_LANGUAGE_EXTENSIONS["sql"]


def test_sql_project_language_is_accepted():
    from app.api.projects import SUPPORTED_LANGUAGES

    assert "sql" in SUPPORTED_LANGUAGES


def test_discover_finds_sql_files(tmp_path):
    from app.extractor.agent_extract import discover_source_files

    (tmp_path / "schema.sql").write_text("CREATE TABLE t(id int);\n")
    (tmp_path / "migrate.ddl").write_text("CREATE TABLE u(id int);\n")
    (tmp_path / "app.py").write_text("x=1\n")
    files = {f.name for f in discover_source_files(str(tmp_path), "sql", limit=10)}
    assert files == {"schema.sql", "migrate.ddl"}


def test_to_envelopes_db_mode_emits_data_entities_and_fk_edges():
    from app.extractor.agent_extract import to_envelopes
    from app.orchestrator.jobs import _VERB_ACCEPT

    extracted = {
        "entities": [
            {
                "id": "data:public.customers",
                "name": "customers",
                "kind": "table",
                "is_sensitive": True,
                "columns": [{"name": "email", "type": "text", "sensitive": True}],
                "summary": "app customers (PII)",
            },
            {"id": "data:public.orders", "name": "orders", "kind": "table"},
            {"name": "noid"},  # dropped
        ],
        "edges": [
            {"source_id": "data:public.orders", "target_id": "data:public.customers", "kind": "REFERENCES"},
            {"source_id": "data:public.orders", "target_id": "data:gone.x", "kind": "REFERENCES"},
        ],
    }
    envs = to_envelopes("sql", "schema.sql", extracted)
    ents = [e for e in envs if e["record_type"] == "data_entity"]
    edges = [e for e in envs if e["record_type"] == "edge"]

    assert len(ents) == 2, "id-less entity dropped"
    # DataEntity is what the live_schema/data_access verbs emit, so the
    # standard ingest accepts it.
    assert "data_entity" in _VERB_ACCEPT["live_schema"]
    cust = next(e for e in ents if e["data"]["id"] == "data:public.customers")
    assert cust["data"]["is_sensitive"] is True
    assert cust["data"]["columns"] and cust["data"]["certainty"] == "inferred"
    # only the FK whose endpoints both exist survives
    assert len(edges) == 1
    assert edges[0]["data"]["kind"] == "REFERENCES"
    assert edges[0]["data"]["target_id"] == "data:public.customers"
