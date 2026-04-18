"""CRUD for organisations.

Admin-only. Phase C-1 foundation — subsequent work retrofits every
project/finding/secret endpoint to enforce the tenancy boundary via
``require_project_org`` from ``app.auth.org_scope``.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.rbac import require_admin
from app.db import get_session
from app.models.auth import User
from app.models.organization import Organization

router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"])

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


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
    result = await db.execute(select(Organization).order_by(Organization.created_at.desc()))
    return [_to_out(o) for o in result.scalars().all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrgCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> OrgOut:
    if not _SLUG.match(body.slug):
        raise HTTPException(status_code=422, detail="invalid_slug")
    org = Organization(slug=body.slug, display_name=body.display_name)
    db.add(org)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="slug_taken")
    await db.refresh(org)
    await audit_record(
        actor=f"user:{user.id}",
        action="organization.create",
        target=str(org.id),
        details={"slug": org.slug},
    )
    return _to_out(org)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization(
    org_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> None:
    # Refuse to delete the bootstrap ``default`` org since existing
    # single-tenant data still lives there.
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="not_found")
    if org.slug == "default":
        raise HTTPException(status_code=409, detail="cannot_delete_default_org")
    await db.execute(delete(Organization).where(Organization.id == org_id))
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="organization.delete",
        target=str(org_id),
    )
