from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_settings = get_settings()
engine = create_async_engine(_settings.database_url, pool_pre_ping=True, future=True)
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
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
