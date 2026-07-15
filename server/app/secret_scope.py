"""Fail-closed organisation scoping for bearer-like Secret UUIDs."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.auth import Secret
from app.models.projects import Project

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class SecretScopeNotFound(Exception):
    """A Secret is absent or does not have the required exact ownership."""


# Labels consumed without a project/user organisation are platform-owned.
# Tenant Secret CRUD must never create or rename into this namespace.
GLOBAL_RESERVED_SECRET_LABELS = frozenset(
    {
        "gitlab_webhook_secret",
        "webhook:gitlab_token",
    }
)
GLOBAL_RESERVED_SECRET_PREFIXES = ("chat-provider:",)


def is_global_reserved_secret_label(label: str) -> bool:
    return label in GLOBAL_RESERVED_SECRET_LABELS or label.startswith(
        GLOBAL_RESERVED_SECRET_PREFIXES
    )


async def resolve_org_secret(
    session: "AsyncSession",
    *,
    organization_id: uuid.UUID | None,
    secret_id: uuid.UUID,
    for_update: bool = False,
) -> Secret:
    """Return ``secret_id`` only for an exact, non-NULL organisation match."""

    if organization_id is None:
        raise SecretScopeNotFound
    stmt = select(Secret).where(
        Secret.id == secret_id,
        Secret.organization_id == organization_id,
    ).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    secret = (await session.execute(stmt)).scalar_one_or_none()
    if secret is None:
        raise SecretScopeNotFound
    return secret


async def resolve_project_secret(
    session: "AsyncSession",
    *,
    project_id: uuid.UUID,
    secret_id: uuid.UUID,
    for_update: bool = False,
) -> Secret:
    """Return a Secret only when it belongs to the project's exact org."""

    stmt = (
        select(Secret)
        .select_from(Secret)
        .join(
            Project,
            (Project.id == project_id)
            & (Project.organization_id.is_not(None))
            & (Project.organization_id == Secret.organization_id),
        )
        .where(
            Secret.id == secret_id,
            Secret.organization_id.is_not(None),
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update(of=Secret)
    secret = (await session.execute(stmt)).scalar_one_or_none()
    if secret is None:
        raise SecretScopeNotFound
    return secret
