"""Shared pytest fixtures.

Splits the suite into two tiers so unit tests stay trivially runnable:
- Tier 1 (default): pure-function tests, no fixtures, no services.
- Tier 2 (marked ``integration``): spin up a real Postgres + Redis (from
  docker-compose or the CI service containers) and drive the FastAPI app
  through ``httpx.AsyncClient``. Each test runs inside its own savepoint
  so side effects don't leak between cases.

Run only unit tests: ``pytest -m "not integration"`` (default in CI if
Postgres is unavailable).
Run everything:    ``pytest``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# PR-97 — the test suite isn't production. config.get_settings refuses
# to boot when SECRET_KEY is a placeholder AND MNEMOS_ENV=production.
# Mark the test env explicitly before any app module imports.
os.environ.setdefault("MNEMOS_ENV", "test")
# PR-110 — startup verify mirrors the CLI ``verify`` at boot.
# Tests that import ``app.main`` shouldn't drag the lifespan
# checks in (they fail without a live DB/Redis); the live tests
# in test_pr110_startup_verify.py exercise it directly.
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")
# PR-144 — the Claude-Code extraction fallback now fires whenever a
# deterministic analyzer binary is absent, which is always true in the
# test env. Tests must never dial the real subscription (slow, non-
# deterministic), so disable the Agent SDK path by default; the agent
# stage then records a clean "agent_sdk_unavailable" skip. Unit tests for
# the agent code exercise the pure helpers, not the live call.
os.environ.setdefault("MNEMOS_DISABLE_AGENT_SDK", "1")


def pytest_collection_modifyitems(config, items):
    """Skip ``integration`` tests when DATABASE_URL isn't reachable.

    Keeps ``pytest`` green on a laptop with nothing running.
    """
    if os.getenv("MNEMOS_SKIP_INTEGRATION") == "1":
        skip_marker = pytest.mark.skip(reason="MNEMOS_SKIP_INTEGRATION=1")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _reset_async_singletons():
    """Drop cached async-redis singletons so each test rebinds them to
    its own event loop.

    A handful of test modules set ``MNEMOS_LOCAL_MODE=1`` at import time;
    because pytest imports every module at collection, the *whole*
    session runs in local mode and ``get_redis()`` / ``sessions._redis``
    hand out a process-global fakeredis whose internal Queue binds to the
    loop that first touched it. pytest-asyncio hands many async tests a
    fresh loop, so a singleton built in an earlier test then raises
    ``<Queue ...> is bound to a different event loop``. Clearing the
    caches before and after every test makes each one lazily rebuild its
    client on its own loop. Cheap no-op for the unit tier (it never
    touches redis); the objects are in-memory so no close() is needed.
    """
    import app.auth.sessions as _sessions
    import app.local_mode as _local_mode
    import app.orchestrator.redis_pool as _redis_pool

    def _clear() -> None:
        _redis_pool._client = None
        _sessions._redis = None
        _local_mode._fake_server = None

    _clear()
    yield
    _clear()


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped loop so async fixtures can share resources."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator:
    """Open a SAVEPOINT-wrapped session and roll it back on teardown.

    Every integration test that wants to touch the database should use
    this fixture rather than ``app.db.SessionLocal`` directly — the
    wrapper keeps tests isolated.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db import SessionLocal, engine

    async with engine.connect() as connection:
        trans = await connection.begin()
        async_session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield async_session
        finally:
            await async_session.close()
            await trans.rollback()

    # Silence SQLAlchemy warnings about unclosed resources in CI.
    _ = SessionLocal


@pytest_asyncio.fixture
async def http_client(db_session):
    """Provide an ``httpx.AsyncClient`` bound to the FastAPI app.

    The app's ``get_session`` dependency is overridden to hand request
    handlers the *same* savepoint-wrapped session the test uses. Without
    this, a handler opened its own pool connection via ``SessionLocal``
    and could not see a row the test had created+committed inside its
    savepoint — under SQLAlchemy 2.0 the test's ``session.commit()`` only
    releases the savepoint, so the outer transaction never reaches other
    connections. That cross-connection gap is what made the auth /
    org-boundary / webhook integration tests 401/403 even though the
    fixtures had clearly inserted the user/project. Sharing one
    connection makes the end-to-end HTTP flow see the seeded rows, and
    the outer ``trans.rollback()`` still isolates the test.
    """
    from httpx import ASGITransport, AsyncClient

    from app.db import get_session
    from app.main import app

    async def _use_test_session() -> AsyncIterator:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # Simulate a browser that already holds the double-submit CSRF
            # cookie: set the cookie + matching X-CSRF-Token header so
            # state-changing POSTs from authed tests pass the CSRF gate
            # (CSRFMiddleware compares cookie == header). Endpoints that
            # are CSRF-exempt ignore the extra header. The dedicated
            # csrf-rejection test uses its own client, not this fixture.
            _csrf = "test-csrf-token"
            client.cookies.set("mnemos_csrf", _csrf)
            client.headers["x-csrf-token"] = _csrf
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest_asyncio.fixture
async def admin_user(db_session):
    """Create a throwaway admin user and yield it.

    The outer SAVEPOINT rollback removes the row at teardown.
    """
    from app.auth.passwords import hash_password
    from app.models.auth import User

    user = User(
        username=f"admin-{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("pw-test-123456"),
        role="admin",
    )
    db_session.add(user)
    await db_session.flush()
    return user
