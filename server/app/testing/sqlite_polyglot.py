"""PR-118 — SQLAlchemy + aiosqlite polyglot type layer.

Production runs on Postgres with JSONB/UUID/ARRAY column types.
That's correct for prod but blocks in-memory testing because
SQLite (which has no docker dependency, no setup cost) doesn't
speak any of those.

This module installs runtime polyglot for the three PG-native
types Mnemos's models use, scoped to a SQLite dialect bind:
- JSONB → JSON  (DDL + value passthrough)
- UUID  → CHAR(36)  (with auto str↔UUID coercion)
- ARRAY → JSON  (with auto list↔json-text coercion)

Behaviour on Postgres is completely unchanged — the patches
only fire when ``dialect.name == 'sqlite'``.

Used by tests that want to run real SQLAlchemy queries against
the real models WITHOUT spinning up a Postgres container.
Critical for verifying queries.py — which up to PR-117 was
only exercised against a fake session that couldn't enforce
SQL WHERE.

Usage:
    from app.testing.sqlite_polyglot import install_polyglot
    install_polyglot()  # idempotent

Then a normal async engine works:
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
"""

from __future__ import annotations

import json
import uuid as _uuid

from sqlalchemy import ARRAY  # generic ARRAY — models import THIS
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

_INSTALLED = False


def install_polyglot() -> None:
    """Patch SQLAlchemy so the production PG types compile and
    bind correctly against a SQLite dialect. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return

    # ─── DDL compiler — what CREATE TABLE emits ───────────────────

    def _visit_array(self, type_, **kw):
        return "JSON"

    def _visit_jsonb(self, type_, **kw):
        return "JSON"

    def _visit_uuid(self, type_, **kw):
        return "CHAR(36)"

    SQLiteTypeCompiler.visit_ARRAY = _visit_array
    SQLiteTypeCompiler.visit_JSONB = _visit_jsonb
    SQLiteTypeCompiler.visit_UUID = _visit_uuid

    # ─── Bind / result processors — value coercion ────────────────
    #
    # ARRAY (list) <-> JSON text
    _orig_array_bind = ARRAY.bind_processor
    _orig_array_result = ARRAY.result_processor

    def _array_bind(self, dialect):
        if dialect.name == "sqlite":
            def _p(value):
                if value is None:
                    return None
                return json.dumps(list(value))
            return _p
        return _orig_array_bind(self, dialect)

    def _array_result(self, dialect, coltype):
        if dialect.name == "sqlite":
            def _p(value):
                if value is None or value == "":
                    return []
                if isinstance(value, list):
                    return value
                try:
                    return json.loads(value)
                except (TypeError, ValueError):
                    return []
            return _p
        return _orig_array_result(self, dialect, coltype)

    ARRAY.bind_processor = _array_bind
    ARRAY.result_processor = _array_result
    # PG_ARRAY is a subclass — re-bind so its MRO picks up the patches.
    PG_ARRAY.bind_processor = _array_bind
    PG_ARRAY.result_processor = _array_result

    # JSONB (dict/list) <-> JSON text
    _orig_jsonb_bind = JSONB.bind_processor
    _orig_jsonb_result = JSONB.result_processor

    def _jsonb_bind(self, dialect):
        if dialect.name == "sqlite":
            def _p(value):
                if value is None:
                    return None
                return json.dumps(value)
            return _p
        return _orig_jsonb_bind(self, dialect)

    def _jsonb_result(self, dialect, coltype):
        if dialect.name == "sqlite":
            def _p(value):
                if value is None or value == "":
                    return None
                if isinstance(value, (dict, list)):
                    return value
                try:
                    return json.loads(value)
                except (TypeError, ValueError):
                    return None
            return _p
        return _orig_jsonb_result(self, dialect, coltype)

    JSONB.bind_processor = _jsonb_bind
    JSONB.result_processor = _jsonb_result

    # UUID (Python uuid.UUID) <-> CHAR(36) string
    _orig_uuid_bind = PG_UUID.bind_processor
    _orig_uuid_result = PG_UUID.result_processor

    def _uuid_bind(self, dialect):
        if dialect.name == "sqlite":
            def _p(value):
                if value is None:
                    return None
                if isinstance(value, _uuid.UUID):
                    return str(value)
                return str(value)
            return _p
        return _orig_uuid_bind(self, dialect)

    def _uuid_result(self, dialect, coltype):
        if dialect.name == "sqlite":
            as_uuid = getattr(self, "as_uuid", True)

            def _p(value):
                if value is None:
                    return None
                if isinstance(value, _uuid.UUID):
                    return value if as_uuid else str(value)
                if as_uuid:
                    try:
                        return _uuid.UUID(str(value))
                    except (TypeError, ValueError):
                        return None
                return str(value)
            return _p
        return _orig_uuid_result(self, dialect, coltype)

    PG_UUID.bind_processor = _uuid_bind
    PG_UUID.result_processor = _uuid_result

    _INSTALLED = True
