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

# The CI DATABASE_URL (Postgres) as it was BEFORE any test module ran.
# Several local-mode test modules (test_pr135 / test_pr138_full_value_chain
# / test_pr138d) point ``DATABASE_URL`` at a throwaway SQLite file and
# ``importlib.reload(app.db)`` without restoring it, so every
# alphabetically-later integration test would otherwise inherit a leaked
# SQLite engine. db_session restores this so the real integration tier
# always runs against the configured (Postgres) database.
_ORIGINAL_DATABASE_URL = os.environ.get("DATABASE_URL")

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


@pytest.fixture
def fake_llm_attempt_callbacks():
    """Install an explicit in-memory accounting capability for adapter tests.

    Product code must never synthesize this capability.  Focused provider
    unit tests use it only to exercise response/terminal parsing after the
    no-bypass guard; durable DB lifecycle tests install their real callbacks
    inside this outer fixture and therefore still verify persistence.
    """

    from datetime import datetime, timezone

    # Import through the extractor package's normal order. Importing lifecycle
    # first would reverse the existing agent -> lifecycle dependency and create
    # a partially-initialized module only in this test fixture.
    from app.extractor import agent as _agent  # noqa: F401
    from app.llm.lifecycle import (
        AttemptCallbacks,
        AttemptTicket,
        LLMSemanticCandidateUnavailable,
        _ATTEMPT_CALLBACKS,
    )

    async def start(metadata):  # noqa: ANN001
        return AttemptTicket(
            attempt_id=uuid.uuid4(),
            operation_id=metadata.operation_id,
            budget_scope_id=uuid.uuid4(),
            started_at=datetime.now(tz=timezone.utc),
            remaining_seconds=3_600.0,
        )

    async def finish(_ticket, _outcome):  # noqa: ANN001
        return None

    async def finish_candidate(_ticket, _outcome, _candidate):  # noqa: ANN001
        return None

    async def replay_candidate(_request):  # noqa: ANN001
        raise LLMSemanticCandidateUnavailable(
            "in-memory adapter fixture has no terminal candidate"
        )

    callbacks = AttemptCallbacks(
        start=start,
        finish=finish,
        finish_candidate=finish_candidate,
        replay_candidate=replay_candidate,
        mark_provider_dispatch=lambda: None,
    )
    token = _ATTEMPT_CALLBACKS.set(callbacks)
    try:
        yield callbacks
    finally:
        _ATTEMPT_CALLBACKS.reset(token)


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped loop so async fixtures can share resources."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _ensure_configured_engine() -> None:
    """Restore ``app.db`` to the originally-configured database.

    A reloader module (test_pr135 / test_pr138*) may have rebound
    ``app.db.engine`` to a throwaway SQLite file and left it that way.
    If the integration tier (which needs the real Postgres) runs after
    one of those, the leaked SQLite engine would carry over — wrong
    backend, polluted/locked state. When the live engine no longer
    matches the original DATABASE_URL, dispose it and reload app.db so
    this test connects to the configured database again.
    """
    if not _ORIGINAL_DATABASE_URL or "sqlite" in _ORIGINAL_DATABASE_URL:
        return
    import app.db as _db

    # The only leak in practice is a reloader binding the engine to SQLite
    # while the configured database is Postgres. Comparing full URLs is
    # unreliable (str(url) masks the password), so key off the backend.
    if _db.engine.url.get_backend_name() != "sqlite":
        return

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool

    os.environ["DATABASE_URL"] = _ORIGINAL_DATABASE_URL
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        await _db.engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    # Rebind the module attributes in place rather than importlib.reload:
    # reloading would create a NEW get_session object, but app.main.app's
    # routes still reference the original, so the http_client override
    # (keyed on the function object) would silently stop applying. get_session
    # looks up SessionLocal from module globals at call time, so swapping
    # these is enough and keeps its identity stable.
    _db.engine = create_async_engine(
        _ORIGINAL_DATABASE_URL, poolclass=NullPool, future=True
    )
    _db.SessionLocal = async_sessionmaker(
        _db.engine, expire_on_commit=False, class_=AsyncSession
    )


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator:
    """Open a SAVEPOINT-wrapped session and roll it back on teardown.

    Every integration test that wants to touch the database should use
    this fixture rather than ``app.db.SessionLocal`` directly — the
    wrapper keeps tests isolated.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    await _ensure_configured_engine()

    from app.db import SessionLocal, engine

    async with engine.connect() as connection:
        trans = await connection.begin()
        # join_transaction_mode="create_savepoint": the session runs inside
        # a SAVEPOINT that is automatically *restarted* after every
        # commit()/rollback() the test or the request handler issues. The
        # default ("conditional_savepoint") does not restart, so the first
        # commit() ends the savepoint and later reads on asyncpg silently
        # miss rows the test seeded (the webhook push test's project lookup
        # returned None even though the row was committed). The outer
        # ``trans.rollback()`` still discards everything at teardown.
        async_session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
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
        # Production defaults session cookies to ``Secure``.  Use an HTTPS
        # origin here so httpx behaves like the browser deployment we claim
        # to exercise and sends the login cookie on subsequent requests.
        # An HTTP origin silently retains-but-withholds a Secure cookie,
        # turning every successful login into a misleading follow-up 401.
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
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
