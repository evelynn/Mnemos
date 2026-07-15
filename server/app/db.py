from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings

_settings = get_settings()
# Test runs drive the app from pytest-asyncio, which (with asyncio_mode=auto
# and function-scoped loops) hands many tests their own event loop. A pooled
# async connection binds to the loop that first opened it, so the *next*
# test reusing this module-level engine hits
# "<...> is bound to a different event loop" the moment it touches the pool.
# NullPool opens a fresh connection per checkout and discards it on release,
# so every test (and the app code it drives) connects on its own live loop.
# Only the test env pays the per-connect cost; real deployments keep the
# pre-ping pool.
_engine_kwargs: dict = {"pool_pre_ping": True, "future": True}
if (_settings.mnemos_env or "").lower() == "test":
    _engine_kwargs = {"poolclass": NullPool, "future": True}
engine = create_async_engine(_settings.database_url, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# Local mode (PR-135) runs the API and the inline job loop in one process,
# so several sessions write the same SQLite file concurrently — e.g. a long
# analysis stage's session plus the StageTracker's progress flushes. SQLite
# allows a single writer and defaults to busy_timeout=0, which surfaces as
# "database is locked" the instant two writers overlap (PR-141 — first hit
# by the Claude-Code extraction stage writing real OpenClaw symbols). WAL +
# a busy_timeout let writers queue instead of failing. Postgres is
# unaffected; this only attaches to SQLite connections.
if _settings.database_url.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _conn_record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        # SQLite parses foreign-key declarations but does not enforce them
        # unless each connection opts in. Graph publication relies on its
        # run/project FKs for stage cleanup and published-run retention, so
        # local mode must match PostgreSQL's fail-closed behaviour.
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
