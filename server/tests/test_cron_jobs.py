"""Cron infrastructure tests (spec §2.7, §2.10).

Pure-function coverage of the advisory-lock helper and the registration
of cron entries on the ARQ WorkerSettings. The actual sweep behaviour
is integration-tested separately because it needs a live Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest


def test_worker_settings_registers_all_crons():
    """If any of the cron entries is missing, periodic work stops.

    PR-33 added ``run_reset_stale_runs`` (auto-flip wedged
    analysis_runs to failed) — now four entries total.
    """
    from app.orchestrator.jobs import WorkerSettings

    names = {c.coroutine.__name__ for c in WorkerSettings.cron_jobs}
    assert names == {
        "run_break_glass_expiry",
        "run_probe_recheck",
        "run_retention_purge",
        "run_reset_stale_runs",
    }


def test_probe_recheck_window_matches_spec():
    """Daily refresh — spec §2.7 wants 24h cache for probe results."""
    from app.orchestrator.cron_jobs import PROBE_RECHECK_INTERVAL

    assert PROBE_RECHECK_INTERVAL == timedelta(hours=24)


@pytest.mark.asyncio
async def test_advisory_lock_loser_returns_none(monkeypatch):
    """When another worker holds the lock, ``with_advisory_lock`` no-ops."""
    from app.orchestrator import cron_jobs

    class FakeSession:
        async def execute(self, *_args, **_kwargs):
            class R:
                def scalar(self_inner):
                    return False  # pg_try_advisory_lock returned False

            return R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def fake_factory():
        @asynccontextmanager
        async def cm():
            yield FakeSession()

        # Imitate sessionmaker(): calling the returned object yields a session.
        class _SM:
            def __call__(self):
                return cm()

        return _SM()

    monkeypatch.setattr(cron_jobs, "SessionLocal_factory", fake_factory)

    called = False

    async def body(session):
        nonlocal called
        called = True

    result = await cron_jobs.with_advisory_lock(
        fake_factory(), "test_job", body
    )
    assert result is None
    assert called is False, "loser must not execute the body"


@pytest.mark.asyncio
async def test_advisory_lock_winner_runs_body_and_releases(monkeypatch):
    """Winner runs the body and releases the lock even if the body raises."""
    from app.orchestrator import cron_jobs

    execute_calls: list[str] = []

    class FakeSession:
        async def execute(self, stmt, params=None):
            sql = str(stmt).lower()
            if "pg_try_advisory_lock" in sql:
                execute_calls.append("acquire")

                class R:
                    def scalar(_):
                        return True

                return R()
            if "pg_advisory_unlock" in sql:
                execute_calls.append("release")

                class R2:
                    def scalar(_):
                        return True

                return R2()
            execute_calls.append(f"body:{sql[:20]}")

            class R3:
                def scalar(_):
                    return None

            return R3()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _SM:
        def __call__(self):
            return FakeSession()

    def fake_factory():
        return _SM()

    monkeypatch.setattr(cron_jobs, "SessionLocal_factory", fake_factory)

    async def body(session):
        return "ok"

    out = await cron_jobs.with_advisory_lock(fake_factory(), "test_winner", body)
    assert out == "ok"
    assert execute_calls[0] == "acquire"
    assert execute_calls[-1] == "release"
