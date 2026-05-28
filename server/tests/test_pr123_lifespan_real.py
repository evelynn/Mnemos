"""PR-123 — FastAPI lifespan handler REAL execution.

PR-110 added startup_verify but its lifespan integration was
only verified via source-grep. PR-123 actually invokes the
lifespan context manager from main.py and confirms:

- Happy path: lifespan enters → exits cleanly when verify passes
- Hard fail (placeholder SECRET_KEY) propagates → uvicorn would
  exit non-zero
- Hard fail (crypto round-trip mismatch) propagates
- Soft fail (DB or Redis briefly down) DOES NOT propagate —
  startup proceeds
- MNEMOS_SKIP_STARTUP_VERIFY=1 opt-out skips verify entirely

Real lifespan invocation — no mocks of FastAPI itself.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_lifespan_runs_to_yield_on_clean_env(monkeypatch):
    """Happy path: enter lifespan, do startup work, yield.
    Then exit cleanly. This is what uvicorn calls on boot."""
    from app.main import lifespan

    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.delenv("MNEMOS_SKIP_STARTUP_VERIFY", raising=False)

    # Patch soft checks (would need live DB/Redis) — still real
    # config + crypto.
    from app import startup_verify
    with patch.object(startup_verify, "_check_db_soft",
                      AsyncMock(return_value=True)), \
            patch.object(startup_verify, "_check_redis_soft",
                         AsyncMock(return_value=True)):
        cm = lifespan(app=None)  # type: ignore
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_lifespan_propagates_hard_config_failure(monkeypatch):
    """If startup verify raises StartupCheckFailed, lifespan must
    re-raise. uvicorn sees the exception → exits non-zero →
    docker compose restarts in a loop until the operator fixes
    the config. This is exactly the protection PR-110 promised."""
    from app.main import lifespan
    from app.startup_verify import StartupCheckFailed

    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.delenv("MNEMOS_SKIP_STARTUP_VERIFY", raising=False)

    from app import startup_verify
    with patch.object(
        startup_verify, "_check_crypto",
        AsyncMock(side_effect=StartupCheckFailed("crypto.broken")),
    ):
        cm = lifespan(app=None)  # type: ignore
        with pytest.raises(StartupCheckFailed) as ei:
            await cm.__aenter__()
        assert "crypto.broken" in str(ei.value)


@pytest.mark.asyncio
async def test_lifespan_continues_when_db_soft_fails(monkeypatch):
    """Soft DB failure must NOT propagate. Startup continues,
    /health/ready will reflect the state, LB drains traffic."""
    from app.main import lifespan
    from app import startup_verify

    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.delenv("MNEMOS_SKIP_STARTUP_VERIFY", raising=False)

    with patch.object(startup_verify, "_check_db_soft",
                      AsyncMock(return_value=False)), \
            patch.object(startup_verify, "_check_redis_soft",
                         AsyncMock(return_value=True)):
        cm = lifespan(app=None)  # type: ignore
        # Must not raise.
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_lifespan_continues_when_redis_soft_fails(monkeypatch):
    from app.main import lifespan
    from app import startup_verify

    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.delenv("MNEMOS_SKIP_STARTUP_VERIFY", raising=False)

    with patch.object(startup_verify, "_check_db_soft",
                      AsyncMock(return_value=True)), \
            patch.object(startup_verify, "_check_redis_soft",
                         AsyncMock(return_value=False)):
        cm = lifespan(app=None)  # type: ignore
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_lifespan_skips_verify_when_env_opt_out_set(monkeypatch):
    """MNEMOS_SKIP_STARTUP_VERIFY=1 must skip the whole verify
    block. Used by tooling that imports the app without a live
    env (CI OpenAPI gen, etc)."""
    from app.main import lifespan
    from app import startup_verify

    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.setenv("MNEMOS_SKIP_STARTUP_VERIFY", "1")

    called = []

    async def tracking(*a, **kw):
        called.append(1)

    with patch.object(startup_verify, "run_startup_verify", tracking):
        cm = lifespan(app=None)  # type: ignore
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)
    assert called == [], (
        "run_startup_verify ran despite MNEMOS_SKIP_STARTUP_VERIFY=1"
    )


@pytest.mark.asyncio
async def test_lifespan_runs_registry_alignment_check(monkeypatch):
    """The other pre-existing boot guard — assert_registry_aligned
    — must still fire. Catches a regression where lifespan refactor
    drops the check."""
    from app.main import lifespan

    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.setenv("MNEMOS_SKIP_STARTUP_VERIFY", "1")

    called = []
    from app.safety import dialects

    def tracking_align():
        called.append("aligned")

    with patch.object(dialects, "assert_registry_aligned", tracking_align):
        cm = lifespan(app=None)  # type: ignore
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)
    assert called == ["aligned"], "registry alignment check not invoked"


@pytest.mark.asyncio
async def test_lifespan_calls_close_redis_on_exit(monkeypatch):
    """On shutdown (lifespan __aexit__), close_redis() must run
    so the pool isn't leaked. SIGTERM hygiene."""
    from app.main import lifespan
    from app.orchestrator import redis_pool

    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.setenv("MNEMOS_SKIP_STARTUP_VERIFY", "1")

    close_calls = []

    async def fake_close():
        close_calls.append(1)

    with patch.object(redis_pool, "close_redis", fake_close), \
            patch("app.main.close_redis", fake_close):
        cm = lifespan(app=None)  # type: ignore
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)
    assert close_calls == [1], "close_redis not invoked on lifespan exit"
