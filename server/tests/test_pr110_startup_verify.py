"""PR-110 — FastAPI startup-time self-verify.

The CLI ``verify`` (PR-102) is operator-initiated. Useful, but
optional. Pre-PR, a platform deployed under ``docker compose up``
with a broken FERNET_KEY or mis-rotated SECRET_KEY still BOOTED
— every secret decrypt throws on first use, by which time a
webhook may have enqueued real work.

This PR runs the same checks at FastAPI startup via the
lifespan handler. Hard fails (config, crypto round-trip) raise
``StartupCheckFailed`` so uvicorn exits non-zero. Soft fails
(DB / Redis transiently down, missing analyzer binaries) log
a warning and let the platform boot — /health/ready will flip
503 until they recover, and the LB drains traffic.

Tests EXECUTE the verify path against patched dependencies —
Karpathy policy. Each test sets up a failure mode (missing
key, broken crypto, dead DB) and confirms the right
hard/soft outcome.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


# ─── _check_config — re-runs the PR-97 placeholder guard ───────────


@pytest.mark.asyncio
async def test_check_config_passes_in_test_env():
    """Sanity: the test env (set in conftest) is not production
    so get_settings() doesn't raise."""
    from app.startup_verify import _check_config

    await _check_config()  # raises if broken


@pytest.mark.asyncio
async def test_check_config_propagates_settings_runtime_error():
    """When get_settings() raises (PR-97 placeholder guard),
    startup_verify must surface it — startup should die."""
    from app import startup_verify

    with patch(
        "app.config.get_settings",
        side_effect=RuntimeError("config: SECRET_KEY is a placeholder"),
    ):
        with pytest.raises(RuntimeError) as ei:
            await startup_verify._check_config()
        assert "placeholder" in str(ei.value)


# ─── _check_crypto — round-trip via app.safety.crypto ──────────────


@pytest.mark.asyncio
async def test_check_crypto_passes_with_working_round_trip():
    """The real KMS roundtrip should succeed in the test env
    (FERNET_KEY is set in the harness)."""
    from app.startup_verify import _check_crypto

    await _check_crypto()


@pytest.mark.asyncio
async def test_check_crypto_raises_when_round_trip_mismatches():
    """If encrypt/decrypt return different bytes for the probe,
    the deployment has a wrong-rotated key — must hard-fail."""
    from app import startup_verify

    def bad_decrypt(ct, iv):
        return "WRONG-VALUE"

    with patch("app.safety.crypto.decrypt", bad_decrypt):
        with pytest.raises(startup_verify.StartupCheckFailed) as ei:
            await startup_verify._check_crypto()
        assert "round_trip" in str(ei.value)


# ─── _check_db_soft / _check_redis_soft — must NOT raise ───────────


@pytest.mark.asyncio
async def test_db_soft_returns_false_on_failure_does_not_raise():
    """A briefly-unreachable DB at boot is normal during
    ``docker compose restart``. The check must return False, not
    raise — the platform proceeds and /health/ready reflects the
    state."""
    from app import startup_verify

    class BoomSession:
        async def __aenter__(self): raise ConnectionRefusedError("nope")
        async def __aexit__(self, *_): return False

    with patch.object(startup_verify, "_check_db_soft",
                      wraps=startup_verify._check_db_soft), \
            patch("app.db.SessionLocal", return_value=BoomSession()):
        ok = await startup_verify._check_db_soft()
    assert ok is False


@pytest.mark.asyncio
async def test_db_reachable_with_unsafe_isolation_fails_startup_hard():
    """Reachability cannot downgrade a broken dollar-cap proof to a warning."""

    from app import startup_verify
    from app.llm.lifecycle import LLMLifecycleError

    class ReachableSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, _statement):
            return None

    unsafe = AsyncMock(
        side_effect=LLMLifecycleError(
            "postgresql_attempt_accounting_requires_read_committed"
        )
    )
    with patch("app.db.SessionLocal", return_value=ReachableSession()), patch(
        "app.llm.lifecycle.assert_attempt_accounting_isolation", unsafe
    ):
        with pytest.raises(
            startup_verify.StartupCheckFailed,
            match="database.postgresql_isolation_requires_read_committed",
        ):
            await startup_verify._check_db_soft()
    unsafe.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_soft_returns_false_on_failure_does_not_raise():
    from app import startup_verify

    async def bad_get_redis():
        raise ConnectionError("redis nope")

    with patch("app.orchestrator.redis_pool.get_redis", bad_get_redis):
        ok = await startup_verify._check_redis_soft()
    assert ok is False


@pytest.mark.asyncio
async def test_redis_soft_returns_false_on_no_pong():
    from app import startup_verify

    fake_redis = SimpleNamespace(ping=AsyncMock(return_value=False))

    async def good_get_redis():
        return fake_redis

    with patch("app.orchestrator.redis_pool.get_redis", good_get_redis):
        ok = await startup_verify._check_redis_soft()
    assert ok is False


# ─── _check_analyzers_soft — always advisory ───────────────────────


def test_analyzers_check_is_always_advisory():
    """Whether binaries are present or missing, this check never
    raises. PR-98's graceful degradation handles the missing case
    at the analyzer dispatch layer."""
    from app.startup_verify import _check_analyzers_soft

    # Just calling it without exception is the assertion.
    _check_analyzers_soft()


# ─── run_startup_verify — orchestrator ─────────────────────────────


@pytest.mark.asyncio
async def test_run_startup_verify_full_path_succeeds():
    """Happy path: every check passes (or soft-passes). Function
    returns None and logs the completion."""
    from app import startup_verify

    # Patch the two soft checks so they don't actually need a live
    # DB/Redis in CI. Hard checks (config, crypto) use the real
    # paths — they work in the test env.
    with patch.object(startup_verify, "_check_db_soft", AsyncMock(return_value=True)), \
            patch.object(startup_verify, "_check_redis_soft", AsyncMock(return_value=True)):
        await startup_verify.run_startup_verify()


@pytest.mark.asyncio
async def test_run_startup_verify_raises_on_hard_fail():
    """Hard fail in crypto must propagate, killing the boot."""
    from app import startup_verify

    with patch.object(
        startup_verify, "_check_crypto",
        AsyncMock(side_effect=startup_verify.StartupCheckFailed("crypto.test")),
    ):
        with pytest.raises(startup_verify.StartupCheckFailed):
            await startup_verify.run_startup_verify()


@pytest.mark.asyncio
async def test_run_startup_verify_proceeds_when_db_soft_fails():
    """A soft DB failure logs but does NOT raise — startup
    continues so the operator can fix DB via /health/ready
    feedback without restarting."""
    from app import startup_verify

    with patch.object(startup_verify, "_check_db_soft", AsyncMock(return_value=False)), \
            patch.object(startup_verify, "_check_redis_soft", AsyncMock(return_value=True)):
        # No exception — soft fail just logs.
        await startup_verify.run_startup_verify()


@pytest.mark.asyncio
async def test_arbitrary_crypto_exception_becomes_startup_check_failed():
    """A bug in app.safety.crypto (e.g. ImportError, missing key
    file) is still a hard fail — the wrapper converts it to
    StartupCheckFailed so the lifespan-handler caller knows to
    bail out."""
    from app import startup_verify

    with patch.object(
        startup_verify, "_check_crypto",
        AsyncMock(side_effect=KeyError("FERNET_KEY")),
    ):
        with pytest.raises(startup_verify.StartupCheckFailed) as ei:
            await startup_verify.run_startup_verify()
        assert str(ei.value) == "crypto_unavailable"
        assert "FERNET_KEY" not in str(ei.value)


# ─── lifespan wiring ───────────────────────────────────────────────


def test_main_lifespan_calls_startup_verify():
    """Source-level guard: a future refactor of main.py mustn't
    silently drop the startup-verify call. If it does, the boot
    safety net is back to /health/ready only."""
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1] / "app" / "main.py"
    ).read_text(encoding="utf-8")
    assert "from app.startup_verify import run_startup_verify" in body
    assert "await run_startup_verify()" in body


def test_startup_verify_can_be_disabled_via_env():
    """For tooling that imports ``app`` without a live env (CI
    OpenAPI generators, etc.) the env-var opt-out keeps the boot
    from blocking on missing dependencies."""
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1] / "app" / "main.py"
    ).read_text(encoding="utf-8")
    assert 'MNEMOS_SKIP_STARTUP_VERIFY' in body
    # Default behaviour is to RUN — opt-out, not opt-in.
    assert '!= "1"' in body
