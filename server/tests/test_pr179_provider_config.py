"""PR-179 provider runtime config and its platform-ownership boundary.

Provider configuration is global, while Mnemos currently has tenant-admin
roles only.  Tenant admins therefore cannot read or mutate the config API.
Runtime resolution still supports platform-provisioned encrypted rows and
environment fallback.
"""

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import chat_config as cc
from app.api import llm_providers as lp
from app.models.auth import PlatformSetting, Secret
from app.safety.crypto import encrypt


class _StubUser:
    id = "test-admin"
    organization_id = None


async def _noop(*a, **k):
    return None


async def _store_global_key(session, provider: str, value: str) -> None:
    ciphertext, iv = encrypt(value)
    session.add(
        Secret(
            label=lp.secret_label(provider),
            kind=lp.SECRET_KIND,
            ciphertext=ciphertext,
            iv=iv,
            organization_id=None,
        )
    )
    await session.commit()


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

    sqlite_session.add(
        PlatformSetting(
            key=lp.SETTING_KEY,
            value={"openai": {"model": "gpt-4o-mini"}},
        )
    )
    await _store_global_key(sqlite_session, "openai", "sk-secret-xyz")

    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["openai"]["api_key"] == "sk-secret-xyz"   # decrypts back
    assert cfg["openai"]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_provider_config_endpoints_require_platform_admin(sqlite_session):
    calls = (
        cc.get_provider_config(user=_StubUser(), db=sqlite_session),
        cc.put_provider_config(
            "openai",
            cc.ProviderConfigUpdate(api_key="sk-top-secret"),
            user=_StubUser(),
            db=sqlite_session,
        ),
        cc.test_provider_config(
            "openai",
            user=_StubUser(),
            db=sqlite_session,
        ),
    )
    for call in calls:
        with pytest.raises(HTTPException) as raised:
            await call
        assert (raised.value.status_code, raised.value.detail) == (
            403,
            "platform_admin_required",
        )


@pytest.mark.asyncio
async def test_db_key_overrides_env(sqlite_session, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    await _store_global_key(sqlite_session, "openai", "sk-from-db")
    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["openai"]["api_key"] == "sk-from-db"


@pytest.mark.asyncio
async def test_clear_key_removes_it(sqlite_session, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    await _store_global_key(sqlite_session, "openai", "sk-a")
    with pytest.raises(HTTPException) as raised:
        await cc.put_provider_config(
            "openai",
            cc.ProviderConfigUpdate(clear_key=True),
            user=_StubUser(),
            db=sqlite_session,
        )
    assert raised.value.status_code == 403
    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["openai"]["api_key"] == "sk-a"


@pytest.mark.asyncio
async def test_atlas_config_persisted(sqlite_session, monkeypatch):
    monkeypatch.delenv("ATLAS_BASE_URL", raising=False)
    monkeypatch.delenv("ATLAS_API_KEY", raising=False)
    monkeypatch.delenv("ATLAS_AGENT_ID", raising=False)
    sqlite_session.add(
        PlatformSetting(
            key=lp.SETTING_KEY,
            value={
                "atlas": {
                    "base_url": "https://atlas.hansol/api/v1/public",
                    "agent_id": "agent-xyz",
                }
            },
        )
    )
    await _store_global_key(sqlite_session, "atlas", "a-key")
    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["atlas"]["base_url"] == "https://atlas.hansol/api/v1/public"
    assert cfg["atlas"]["agent_id"] == "agent-xyz"
    # Configuration is preserved without advertising an unsafe generation
    # route until Atlas exposes the required token/cost/receipt contract.
    assert lp.is_provider_available("atlas", cfg) is False


@pytest.mark.asyncio
async def test_atlas_default_base_url(sqlite_session, monkeypatch):
    # With nothing set, Atlas base URL defaults to the hansol public endpoint.
    monkeypatch.delenv("ATLAS_BASE_URL", raising=False)
    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["atlas"]["base_url"] == "https://ai-atlas.hansol.net/api/v1/public"


@pytest.mark.asyncio
async def test_claude_chat_defaults_to_bounded_api_mode(sqlite_session, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = await lp.resolve_config(sqlite_session)
    assert cfg["claudecode"]["mode"] == "api"


def test_claude_status_never_advertises_key_free_agent_sdk_chat():
    cfg = {
        pid: {"api_key": None, "model": None, "base_url": None}
        for pid in lp.PROVIDER_ORDER
    }
    cfg["claudecode"]["mode"] = "api"

    status = cc._status("claudecode", cfg)

    assert status["available"] is False
    assert status["needs_key"] is True
    assert status["subscription_available"] is False
    assert status["agent_sdk_policy"] == (
        "explicit_128k_reservation_and_attribution_required"
    )


def test_status_dot_reflects_last_test():
    # The Settings dot derives connected/failed from the persisted
    # last_test_result string (OK-prefixed == success). Pure, no DB.
    cfg = {pid: {"api_key": None, "model": None, "base_url": None}
           for pid in lp.PROVIDER_ORDER}
    cfg["openai"]["api_key"] = "k"

    class _Sec:
        last_tested_at = None
        last_test_result = "OK — 5 models"

    by_label = {lp.secret_label("openai"): _Sec()}
    assert cc._status("openai", cfg, by_label)["last_test_ok"] is True
    _Sec.last_test_result = "HTTP 401"
    assert cc._status("openai", cfg, by_label)["last_test_ok"] is False
    # No secret row → untested (None, not False).
    assert cc._status("gemini", cfg, {})["last_test_ok"] is None


@pytest.mark.asyncio
async def test_put_unknown_provider_does_not_bypass_platform_boundary(
    sqlite_session,
):
    with pytest.raises(HTTPException) as ei:
        await cc.put_provider_config(
            "bogus", cc.ProviderConfigUpdate(model="x"),
            user=_StubUser(), db=sqlite_session,
        )
    assert (ei.value.status_code, ei.value.detail) == (
        403,
        "platform_admin_required",
    )
