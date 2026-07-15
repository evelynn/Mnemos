"""Org-scope ACL helpers.

Single-tenant deployments keep every user and project in the concrete
``default`` organisation. Access still requires that exact id; ``NULL`` is
never a wildcard because organisation deletion can produce org-less rows.

Retrofit status (Phase C-1 foundation):
- Organization model + FK columns: ✅
- ``resolve_project_org()`` + ``require_project_org()`` helpers: ✅
- Project-addressed endpoint retrofit: ✅ router dependencies or an inline
  resolver cover project/run/finding/Plan/Diff UUID routes.
- Exact non-NULL comparison: ✅ missing, foreign, and legacy NULL ownership
  fail closed without disclosing which case occurred.
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

    Both sides must carry the same concrete organisation id. ``NULL`` is not
    a legacy wildcard: organisation deletion can produce org-less users and
    projects, so accepting it would silently grant global project access.
    """
    return (
        project_org_id is not None
        and user.organization_id is not None
        and user.organization_id == project_org_id
    )


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


async def resolve_run_org(
    session: "AsyncSession", run_id: uuid.UUID
) -> tuple[bool, uuid.UUID | None]:
    """Resolve the organisation that owns an analysis run.

    Returns ``(found, org_id)``. ``found`` is False when no run with that
    id exists, so callers can return 404 without leaking whether the id
    is real-but-foreign vs. nonexistent.
    """
    from sqlalchemy import select

    from app.models.graph import AnalysisRun
    from app.models.projects import Project

    row = (
        await session.execute(
            select(Project.organization_id)
            .join(AnalysisRun, AnalysisRun.project_id == Project.id)
            .where(AnalysisRun.id == run_id)
        )
    ).one_or_none()
    if row is None:
        return False, None
    return True, row[0]


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


def require_run_org():
    """FastAPI dependency for endpoints addressed by ``run_id`` instead of
    ``project_id`` (the analysis-run read/cancel/SSE/stages routes).

    Resolves the run's owning organisation and 404s on a cross-tenant or
    unknown run — same "404 never leaks existence" contract as
    ``require_project_org``. Without this, a user in org A could read or
    cancel org B's runs just by knowing the run UUID (which appears in SSE
    URLs and audit logs).
    """
    from app.auth.deps import current_user
    from app.db import get_session

    async def _dep(
        run_id: uuid.UUID,
        user: "User" = Depends(current_user),
        db=Depends(get_session),
    ) -> "User":
        found, org_id = await resolve_run_org(db, run_id)
        if not found or not same_org(user, org_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return user

    return _dep
