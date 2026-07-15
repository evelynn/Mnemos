"""Stable FastAPI database dependency.

``app.db`` is deliberately reloaded by the Docker-free test harness when it
switches between PostgreSQL and SQLite.  Route dependency overrides are keyed
by callable identity, so defining ``get_session`` in that reloadable module
left already-created routes pointing at a stale function object.  Keep the
callable here and resolve the current session factory lazily; reloading
``app.db`` can then replace its engine/factory without invalidating any route
or test override.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession


async def get_session() -> AsyncIterator[AsyncSession]:
    from app.db import SessionLocal

    async with SessionLocal() as session:
        yield session
