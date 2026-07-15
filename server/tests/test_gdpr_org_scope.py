"""GDPR export/erase must not cross exact organisation ownership."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import gdpr as api
from app.models.audit import AuditLog
from app.models.auth import ApiKey, User
from app.models.base import Base
from app.models.organization import Organization
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


def _user(name: str, org_id, *, role: str = "viewer") -> User:
    return User(
        id=uuid.uuid4(),
        username=name,
        password_hash="not-used",
        role=role,
        organization_id=org_id,
    )


@pytest.mark.asyncio
async def test_gdpr_export_and_erase_are_exact_org_in_real_sqlite(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    Organization.__table__,
                    User.__table__,
                    ApiKey.__table__,
                    AuditLog.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org_a = Organization(
        id=uuid.uuid4(),
        slug=f"gdpr-a-{uuid.uuid4().hex[:8]}",
        display_name="GDPR A",
    )
    org_b = Organization(
        id=uuid.uuid4(),
        slug=f"gdpr-b-{uuid.uuid4().hex[:8]}",
        display_name="GDPR B",
    )
    actor_a = _user("gdpr-actor-a", org_a.id, role="admin")
    target_a = _user("gdpr-target-a", org_a.id)
    target_b = _user("gdpr-target-b", org_b.id)
    target_null = _user("gdpr-target-null", None)
    actor_null = _user("gdpr-actor-null", None, role="admin")
    key_a = ApiKey(
        id=uuid.uuid4(),
        user_id=target_a.id,
        token_hash=f"hash-{uuid.uuid4().hex}",
        label="a-key",
    )
    key_b = ApiKey(
        id=uuid.uuid4(),
        user_id=target_b.id,
        token_hash=f"hash-{uuid.uuid4().hex}",
        label="b-key",
    )
    audit_a = AuditLog(
        actor=f"user:{target_a.id}",
        action="user.profile_updated",
        target=f"user:{target_a.id}",
        details={"email": "a@example.test"},
    )
    audit_b = AuditLog(
        actor=f"user:{target_b.id}",
        action="user.profile_updated",
        target=f"user:{target_b.id}",
        details={"email": "b@example.test"},
    )

    async def _audit(**_kwargs):
        return None

    monkeypatch.setattr(api, "audit_record", _audit)

    try:
        async with factory() as session:
            session.add_all([org_a, org_b])
            await session.flush()
            session.add_all(
                [actor_a, target_a, target_b, target_null, actor_null]
            )
            await session.flush()
            session.add_all([key_a, key_b, audit_a, audit_b])
            await session.commit()

            exported = await api.export_user(target_a.id, session, actor_a)
            assert exported["user"]["id"] == str(target_a.id)
            assert exported["api_keys"][0]["id"] == str(key_a.id)
            assert exported["audit_entries"][0]["details"] == {
                "email": "a@example.test"
            }

            # Foreign, NULL-owned, missing, and org-less-actor cases all hide
            # the target identically and perform no erase side effects.
            hidden_cases = (
                (actor_a, target_b.id),
                (actor_a, target_null.id),
                (actor_a, uuid.uuid4()),
                (actor_null, target_a.id),
            )
            for actor, target_id in hidden_cases:
                with pytest.raises(HTTPException) as raised:
                    await api.export_user(target_id, session, actor)
                assert (raised.value.status_code, raised.value.detail) == (
                    404,
                    "not_found",
                )
                with pytest.raises(HTTPException) as raised:
                    await api.erase_user(target_id, session, actor)
                assert (raised.value.status_code, raised.value.detail) == (
                    404,
                    "not_found",
                )

            assert await session.get(User, target_b.id) is not None
            assert await session.get(User, target_null.id) is not None
            assert await session.get(ApiKey, key_b.id) is not None
            await session.refresh(audit_b)
            assert audit_b.actor == f"user:{target_b.id}"
            assert audit_b.details == {"email": "b@example.test"}

            erased = await api.erase_user(target_a.id, session, actor_a)
            assert erased == {
                "status": "erased",
                "redacted_actor": f"redacted:{target_a.id}",
            }
            assert await session.get(User, target_a.id) is None
            assert await session.get(ApiKey, key_a.id) is None
            redacted = (
                await session.execute(
                    select(AuditLog).where(AuditLog.id == audit_a.id)
                )
            ).scalar_one()
            assert redacted.actor == f"redacted:{target_a.id}"
            assert redacted.details is None
    finally:
        await engine.dispose()
