"""Invite creation and consumption require a concrete live organization."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import onboarding
from app.models.auth import User
from app.models.base import Base
from app.models.onboarding import UserInvite
from app.models.organization import Organization
from app.safety.tokens import hash_token
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


def _admin(username: str, organization_id: uuid.UUID | None) -> User:
    return User(
        id=uuid.uuid4(),
        username=username,
        password_hash="not-used",
        role="admin",
        organization_id=organization_id,
    )


@pytest.mark.asyncio
async def test_invites_fail_closed_without_a_live_organization(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    Organization.__table__,
                    User.__table__,
                    UserInvite.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org = Organization(
        id=uuid.uuid4(),
        slug="invite-org",
        display_name="Invite org",
    )
    actor = _admin("invite-admin", org.id)
    orgless_actor = _admin("orgless-admin", None)

    async def _audit(**_kwargs):
        return None

    monkeypatch.setattr(onboarding, "audit_record", _audit)

    try:
        async with factory() as session:
            session.add(org)
            await session.flush()
            session.add_all([actor, orgless_actor])
            await session.commit()

            with pytest.raises(HTTPException) as raised:
                await onboarding.create_invite(
                    onboarding.InviteCreate(email="blocked@example.com"),
                    orgless_actor,
                    session,
                )
            assert (raised.value.status_code, raised.value.detail) == (
                403,
                "organization_required",
            )
            assert int(
                (
                    await session.execute(select(func.count(UserInvite.id)))
                ).scalar_one()
            ) == 0

            created = await onboarding.create_invite(
                onboarding.InviteCreate(
                    email="member@example.com",
                    role="operator",
                ),
                actor,
                session,
            )
            assert created.token is not None

            accepted = await onboarding.accept_invite(
                onboarding.InviteAccept(
                    token=created.token,
                    username="accepted-member",
                    password="ValidPassword123!",
                ),
                session,
            )
            accepted_user = (
                await session.execute(
                    select(User).where(User.id == uuid.UUID(accepted["user_id"]))
                )
            ).scalar_one()
            assert accepted_user.organization_id == org.id
            assert accepted_user.role == "operator"

            legacy_token = "legacy-null-organization-token"
            legacy_invite = UserInvite(
                id=uuid.uuid4(),
                email="legacy@example.com",
                token_hash=hash_token(legacy_token),
                role="admin",
                organization_id=None,
                expires_at=datetime.now(tz=timezone.utc) + timedelta(days=1),
            )
            session.add(legacy_invite)
            await session.commit()

            with pytest.raises(HTTPException) as raised:
                await onboarding.accept_invite(
                    onboarding.InviteAccept(
                        token=legacy_token,
                        username="must-not-exist",
                        password="ValidPassword123!",
                    ),
                    session,
                )
            assert (raised.value.status_code, raised.value.detail) == (
                400,
                "invite_invalid_or_expired",
            )
            assert (
                await session.execute(
                    select(User).where(User.username == "must-not-exist")
                )
            ).scalar_one_or_none() is None
            await session.refresh(legacy_invite)
            assert legacy_invite.consumed_at is None
            assert legacy_invite.consumed_by_user_id is None
    finally:
        await engine.dispose()
