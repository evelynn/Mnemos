"""Tenant admins are not implicit platform admins when ownership is NULL."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import organizations as org_api
from app.api import users as users_api
from app.models.auth import User
from app.models.base import Base
from app.models.organization import Organization
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


def _user(username: str, org_id, *, role: str = "admin", disabled=False) -> User:
    return User(
        id=uuid.uuid4(),
        username=username,
        password_hash="not-used",
        role=role,
        organization_id=org_id,
        disabled_at=datetime.now(tz=timezone.utc) if disabled else None,
    )


@pytest.mark.asyncio
async def test_organization_and_orgless_user_admin_handlers_fail_closed(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[Organization.__table__, User.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org_a = Organization(
        id=uuid.uuid4(),
        slug="default",
        display_name="Default org",
    )
    org_b = Organization(
        id=uuid.uuid4(),
        slug=f"foreign-{uuid.uuid4().hex[:8]}",
        display_name="Foreign org",
    )
    actor_a = _user("actor-a", org_a.id)
    user_a = _user("user-a", org_a.id, role="viewer")
    user_b = _user("user-b", org_b.id, role="viewer")
    orgless_actor = _user("orphan-admin", None)
    orgless_target = _user("orphan-target", None, role="viewer", disabled=True)

    async def _audit(**_kwargs):
        return None

    monkeypatch.setattr(users_api, "audit_record", _audit)

    try:
        async with factory() as session:
            session.add_all([org_a, org_b])
            await session.flush()
            session.add_all(
                [actor_a, user_a, user_b, orgless_actor, orgless_target]
            )
            await session.commit()

            # Organisation routes expose only the caller's own org. With no
            # platform-admin role, creation and every deletion are disabled.
            assert [
                row.id
                for row in await org_api.list_organizations(session, actor_a)
            ] == [org_a.id]
            assert await org_api.list_organizations(session, orgless_actor) == []

            with pytest.raises(HTTPException) as raised:
                await org_api.create_organization(
                    org_api.OrgCreate(slug="new-org", display_name="New"),
                    session,
                    actor_a,
                )
            assert (raised.value.status_code, raised.value.detail) == (
                403,
                "platform_admin_required",
            )

            for hidden_id in (org_b.id, uuid.uuid4()):
                with pytest.raises(HTTPException) as raised:
                    await org_api.delete_organization(
                        hidden_id,
                        session,
                        actor_a,
                    )
                assert (raised.value.status_code, raised.value.detail) == (
                    404,
                    "not_found",
                )
            with pytest.raises(HTTPException) as raised:
                await org_api.delete_organization(org_a.id, session, actor_a)
            assert (raised.value.status_code, raised.value.detail) == (
                409,
                "organization_deletion_disabled",
            )
            assert int(
                (await session.execute(select(func.count(Organization.id)))).scalar_one()
            ) == 2

            # User listing and creation fail closed for an org-less admin.
            listed = await users_api.list_users(actor_a, session, False)
            assert {row.id for row in listed} == {actor_a.id, user_a.id}
            assert await users_api.list_users(orgless_actor, session, True) == []
            with pytest.raises(HTTPException) as raised:
                await users_api.create_user(
                    users_api.UserCreate(
                        username="must-not-exist",
                        password="ValidPassword123!",
                    ),
                    orgless_actor,
                    session,
                )
            assert (raised.value.status_code, raised.value.detail) == (
                403,
                "organization_required",
            )

            # UUID actions hide both cross-org targets and None==None targets.
            original_b_role = user_b.role
            original_orphan_role = orgless_target.role
            for actor, target in (
                (actor_a, user_b),
                (orgless_actor, orgless_target),
            ):
                with pytest.raises(HTTPException) as raised:
                    await users_api.change_role(
                        target.id,
                        users_api.RoleChange(role="admin"),
                        actor,
                        session,
                    )
                assert raised.value.status_code == 404
                with pytest.raises(HTTPException) as raised:
                    await users_api.disable_user(target.id, actor, session)
                assert raised.value.status_code == 404
                with pytest.raises(HTTPException) as raised:
                    await users_api.enable_user(target.id, actor, session)
                assert raised.value.status_code == 404

            assert user_b.role == original_b_role
            assert orgless_target.role == original_orphan_role
            assert orgless_target.disabled_at is not None

            # The ordinary same-org management path remains functional.
            changed = await users_api.change_role(
                user_a.id,
                users_api.RoleChange(role="operator"),
                actor_a,
                session,
            )
            assert changed.role == "operator"
    finally:
        await engine.dispose()
