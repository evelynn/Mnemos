"""Fail-closed organisation visibility for tenant administrators.

Mnemos has no platform-admin role. An admin may read only their own concrete
organisation; tenant creation and deletion remain disabled until a separate
platform ownership workflow exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import require_admin
from app.db import get_session
from app.models.auth import User
from app.models.organization import Organization

router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"])

class OrgCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)


class OrgOut(BaseModel):
    id: uuid.UUID
    slug: str
    display_name: str
    created_at: datetime


def _to_out(o: Organization) -> OrgOut:
    return OrgOut(
        id=o.id, slug=o.slug, display_name=o.display_name, created_at=o.created_at
    )


@router.get("")
async def list_organizations(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> list[OrgOut]:
    if user.organization_id is None:
        return []
    result = await db.execute(
        select(Organization).where(Organization.id == user.organization_id)
    )
    return [_to_out(o) for o in result.scalars().all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrgCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> OrgOut:
    # There is no platform-admin role. A tenant admin must not mint a new
    # tenant and implicitly become a global organisation administrator.
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="platform_admin_required",
    )


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization(
    org_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> None:
    if user.organization_id is None:
        raise HTTPException(status_code=404, detail="not_found")
    org = (
        await db.execute(
            select(Organization).where(
                Organization.id == org_id,
                Organization.id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="not_found")
    # Tenant deletion needs a platform-level ownership transfer workflow.
    # No such role/workflow exists, so even self-org deletion is disabled.
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="organization_deletion_disabled",
    )
