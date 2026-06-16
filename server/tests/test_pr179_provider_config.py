"""PR-179 — AI-provider config: encrypted storage, DB resolution, masking.

The Settings UI writes provider config through ``chat_config`` (keys to the
encrypted ``Secret`` table, models/base_url to ``PlatformSetting``), and
``llm_providers.resolve_config`` reads it back DB-over-env at chat time.
These exercise that round-trip on a self-contained in-memory SQLite DB
(same polyglot layer serve_local uses), so they run in CI with no Postgres
and no live LLM. Auditing is stubbed to keep the test on the config path.
"""

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import chat_config as cc
from app.api import llm_providers as lp


class _StubUser:
    id = "test-admin"
    organization_id = None


async def _noop(*a, **k):
    return None


@pytest_asyncio.fixture
async def sqlite_session(monkeypatch):
    # Polyglot makes the PG-typed models (JSONB/UUID) run on SQLite.
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    # Register every table (mirror local_mode.ensure_sqlite_schema) so FKs
    # like users.organization_id → organizations resolve before create_all.
    from app.models import (  # noqa: F401
        audit,
        auth,
        comments,
        findings,
        graph,
        onboarding,
        organization,
        plans,
        projects,
        runtime,
        samples,
        stages,
    )
    from app.models.base import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(cc, "audit_record", _noop)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_put_then_resolve_reads_key_and_model(sqlite_session, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    res = await cc.put_provider_config(
        "openai",
        cc.ProviderConfigUpdate(api_key="sk-secret-xyz", model="gpt-4o-mini"),
        user=_StubUser(), db=sqlite_session,
    )
    assert res["has_key"] is True
    assert res["model"] == "gpt-4o-mini"
    assert res["available"] is True

    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["openai"]["api_key"] == "sk-secret-xyz"   # decrypts back
    assert cfg["openai"]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_get_config_never_returns_the_key(sqlite_session):
    await cc.put_provider_config(
        "openai", cc.ProviderConfigUpdate(api_key="sk-top-secret"),
        user=_StubUser(), db=sqlite_session,
    )
    got = await cc.get_provider_config(user=_StubUser(), db=sqlite_session)
    assert "sk-top-secret" not in str(got)
    op = next(p for p in got["providers"] if p["id"] == "openai")
    assert op["has_key"] is True
    assert op["suggested_models"]  # dropdown seeds present


@pytest.mark.asyncio
async def test_db_key_overrides_env(sqlite_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    await cc.put_provider_config(
        "openai", cc.ProviderConfigUpdate(api_key="sk-from-db"),
        user=_StubUser(), db=sqlite_session,
    )
    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["openai"]["api_key"] == "sk-from-db"


@pytest.mark.asyncio
async def test_clear_key_removes_it(sqlite_session, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    await cc.put_provider_config(
        "openai", cc.ProviderConfigUpdate(api_key="sk-a"),
        user=_StubUser(), db=sqlite_session,
    )
    await cc.put_provider_config(
        "openai", cc.ProviderConfigUpdate(clear_key=True),
        user=_StubUser(), db=sqlite_session,
    )
    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["openai"]["api_key"] is None


@pytest.mark.asyncio
async def test_atlas_base_url_persisted(sqlite_session, monkeypatch):
    monkeypatch.delenv("ATLAS_BASE_URL", raising=False)
    monkeypatch.delenv("ATLAS_API_KEY", raising=False)
    await cc.put_provider_config(
        "atlas",
        cc.ProviderConfigUpdate(
            api_key="a-key", base_url="https://atlas.hansol/v1", model="atlas-1"
        ),
        user=_StubUser(), db=sqlite_session,
    )
    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["atlas"]["base_url"] == "https://atlas.hansol/v1"
    assert cfg["atlas"]["model"] == "atlas-1"
    assert lp.is_provider_available("atlas", cfg) is True


@pytest.mark.asyncio
async def test_put_unknown_provider_404(sqlite_session):
    with pytest.raises(HTTPException) as ei:
        await cc.put_provider_config(
            "bogus", cc.ProviderConfigUpdate(model="x"),
            user=_StubUser(), db=sqlite_session,
        )
    assert ei.value.status_code == 404
