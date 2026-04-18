"""Org-scope ACL helpers.

Single-tenant deployments keep every user (and every project) in the
``default`` organisation, so ``same_org()`` trivially returns True until
the operator explicitly carves the system into multiple organisations.

Retrofit status (Phase C-1 foundation):
- Organization model + FK columns: ✅
- ``resolve_project_org()`` + ``require_project_org()`` helpers: ✅
- Endpoint retrofit (every project/secret/finding route adds
  ``require_project_org`` dep): **TODO** (Phase C-1b).
  Until retrofit completes, a compromised ``operator`` in org A can
  still read project UUIDs in org B. The foundation here lets the
  retrofit be mechanical — add one dep per route.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, status

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.auth import User


def same_org(user: "User", project_org_id: uuid.UUID | None) -> bool:
    """Return True when the user shares the project's organisation.

    Returns True when either side is None (single-tenant legacy row) so
    existing deployments keep working.
    """
    if project_org_id is None or user.organization_id is None:
        return True
    return user.organization_id == project_org_id


async def resolve_project_org(
    session: "AsyncSession", project_id: uuid.UUID
) -> uuid.UUID | None:
    from sqlalchemy import select

    from app.models.projects import Project

    row = (
        await session.execute(
            select(Project.organization_id).where(Project.id == project_id)
        )
    ).scalar_one_or_none()
    return row


def require_project_org():
    """FastAPI dependency: 404 when the project sits in a different org.

    We choose 404 (not 403) so org boundaries don't leak project
    existence across tenants.
    """
    # Import deferred so this module stays redis-free for unit tests.
    from app.auth.deps import current_user
    from app.db import get_session

    async def _dep(
        project_id: uuid.UUID,
        user: "User" = Depends(current_user),
        db=Depends(get_session),
    ) -> "User":
        org_id = await resolve_project_org(db, project_id)
        if not same_org(user, org_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return user

    return _dep
