"""Exact, fail-closed organisation scoping for administrative user actions."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.auth import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class UserScopeNotFound(Exception):
    """The target is absent or outside the actor's concrete organisation."""


async def resolve_managed_user(
    session: "AsyncSession",
    *,
    actor_organization_id: uuid.UUID | None,
    user_id: uuid.UUID,
    for_update: bool = False,
) -> User:
    """Resolve an exact same-org target; NULL ownership never matches."""

    if actor_organization_id is None:
        raise UserScopeNotFound
    stmt = (
        select(User)
        .where(
            User.id == user_id,
            User.organization_id == actor_organization_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    target = (await session.execute(stmt)).scalar_one_or_none()
    if target is None:
        raise UserScopeNotFound
    return target
