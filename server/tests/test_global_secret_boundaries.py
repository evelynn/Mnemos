"""Platform-owned config/secret namespaces are not tenant-admin surfaces."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import chat_config, llm_providers, secrets as secrets_api, webhooks
from app.models.auth import PlatformSetting, Secret, User
from app.models.base import Base
from app.models.organization import Organization
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _ScalarRows(self._rows)


class _SequenceSession:
    def __init__(self, *results):
        self.results = list(results)

    async def execute(self, _statement):
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_platform_secret_boundaries_against_real_sqlite(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    Organization.__table__,
                    User.__table__,
                    Secret.__table__,
                    PlatformSetting.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org = Organization(
        id=uuid.uuid4(),
        slug=f"secret-boundary-{uuid.uuid4().hex[:8]}",
        display_name="Secret boundary",
    )
    actor = User(
        id=uuid.uuid4(),
        username=f"secret-admin-{uuid.uuid4().hex[:8]}",
        password_hash="not-used",
        role="admin",
        organization_id=org.id,
    )
    regular = Secret(
        id=uuid.uuid4(),
        label=f"regular-{uuid.uuid4().hex}",
        kind="other",
        ciphertext=b"regular",
        iv=b"iv",
        organization_id=org.id,
    )
    contaminated_webhook = Secret(
        id=uuid.uuid4(),
        label="gitlab_webhook_secret",
        kind="other",
        ciphertext=b"tenant-webhook",
        iv=b"iv",
        organization_id=org.id,
    )
    contaminated_chat = Secret(
        id=uuid.uuid4(),
        label="chat-provider:openai",
        kind="llm_api_key",
        ciphertext=b"tenant-chat",
        iv=b"iv",
        organization_id=org.id,
    )

    try:
        async with factory() as session:
            session.add(org)
            await session.flush()
            session.add(actor)
            await session.flush()
            session.add_all([regular, contaminated_webhook, contaminated_chat])
            await session.commit()

            for label in (
                "gitlab_webhook_secret",
                "webhook:gitlab_token",
                "chat-provider:openai",
                "chat-provider:any-future-provider",
            ):
                with pytest.raises(HTTPException) as raised:
                    await secrets_api.create_secret(
                        secrets_api.SecretCreate(
                            label=label,
                            kind="other",
                            value="must-not-store",
                        ),
                        session,
                        actor,
                    )
                assert (raised.value.status_code, raised.value.detail) == (
                    409,
                    "reserved_global_secret_label",
                )

            with pytest.raises(HTTPException) as raised:
                await secrets_api.update_secret(
                    regular.id,
                    secrets_api.SecretUpdate(label="chat-provider:openai"),
                    session,
                    actor,
                )
            assert raised.value.detail == "reserved_global_secret_label"
            assert regular.label.startswith("regular-")

            with pytest.raises(HTTPException) as raised:
                await secrets_api.update_secret(
                    contaminated_webhook.id,
                    secrets_api.SecretUpdate(value="replacement"),
                    session,
                    actor,
                )
            assert raised.value.detail == "reserved_global_secret_label"
            assert contaminated_webhook.ciphertext == b"tenant-webhook"

            # Global provider-config management is not required for runtime
            # resolution: environment/legacy platform config remains separate.
            for call in (
                chat_config.get_provider_config(actor, session),
                chat_config.put_provider_config(
                    "openai",
                    chat_config.ProviderConfigUpdate(model="gpt-test"),
                    actor,
                    session,
                ),
                chat_config.test_provider_config("openai", actor, session),
            ):
                with pytest.raises(HTTPException) as raised:
                    await call
                assert (raised.value.status_code, raised.value.detail) == (
                    403,
                    "platform_admin_required",
                )

            decrypt_calls: list[bytes] = []

            def _webhook_decrypt(ciphertext, _iv):
                decrypt_calls.append(ciphertext)
                return "decrypted"

            monkeypatch.setattr("app.safety.crypto.decrypt", _webhook_decrypt)
            assert await webhooks._secret(session) is None
            assert decrypt_calls == []

            monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
            cfg = await llm_providers.resolve_config(session)
            assert cfg["openai"]["api_key"] == "env-openai-key"

            # A single platform-owned NULL row remains readable for legacy
            # deployments, while tenant rows above were ignored.
            await session.delete(contaminated_webhook)
            await session.delete(contaminated_chat)
            await session.commit()
            global_webhook = Secret(
                id=uuid.uuid4(),
                label="gitlab_webhook_secret",
                kind="other",
                ciphertext=b"global-webhook",
                iv=b"iv",
                organization_id=None,
            )
            global_chat = Secret(
                id=uuid.uuid4(),
                label="chat-provider:openai",
                kind="llm_api_key",
                ciphertext=b"global-chat",
                iv=b"iv",
                organization_id=None,
            )
            session.add_all([global_webhook, global_chat])
            await session.commit()
            assert await webhooks._secret(session) == "decrypted"
            assert decrypt_calls == [b"global-webhook"]

            monkeypatch.setattr(
                llm_providers,
                "decrypt",
                lambda ciphertext, _iv: (
                    "global-openai-key" if ciphertext == b"global-chat" else ""
                ),
            )
            cfg = await llm_providers.resolve_config(session)
            assert cfg["openai"]["api_key"] == "global-openai-key"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_platform_secret_labels_fail_closed(monkeypatch):
    duplicate = [
        SimpleNamespace(
            label="gitlab_webhook_secret",
            ciphertext=b"one",
            iv=b"iv",
            organization_id=None,
        ),
        SimpleNamespace(
            label="gitlab_webhook_secret",
            ciphertext=b"two",
            iv=b"iv",
            organization_id=None,
        ),
    ]
    decrypt_calls: list[bytes] = []
    monkeypatch.setattr(
        "app.safety.crypto.decrypt",
        lambda ciphertext, _iv: decrypt_calls.append(ciphertext),
    )
    assert await webhooks._secret(_SequenceSession(_Result(rows=duplicate))) is None
    assert decrypt_calls == []

    provider_duplicates = [
        SimpleNamespace(label="chat-provider:openai", ciphertext=b"one", iv=b"iv"),
        SimpleNamespace(label="chat-provider:openai", ciphertext=b"two", iv=b"iv"),
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "env-fallback")
    cfg = await llm_providers.resolve_config(
        _SequenceSession(
            _Result(scalar=None),
            _Result(rows=provider_duplicates),
        )
    )
    assert cfg["openai"]["api_key"] == "env-fallback"
