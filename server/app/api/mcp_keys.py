"""Project-scoped MCP API-key lifecycle.

The raw credential is generated with 256 bits of entropy and returned exactly
once.  Only its SHA-256 digest is persisted.  All reads and revocations join
the key back through its concrete owner and project organisation so legacy
NULL/orphaned or cross-tenant rows never become management capabilities.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import check_role, require_operator
from app.db import get_session
from app.models.auth import ApiKey, User
from app.models.projects import Project
from app.safety.tokens import hash_token

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/mcp-keys",
    tags=["mcp_keys"],
    dependencies=[
        Depends(require_project_org()),
        Depends(require_operator),
    ],
)


class MCPKeyCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=120)


class MCPKeyMetadata(BaseModel):
    id: uuid.UUID
    label: str | None
    created_at: datetime
    revoked_at: datetime | None


class MCPKeyCreated(MCPKeyMetadata):
    # Deliberately absent from MCPKeyMetadata: no later read can recover it.
    token: str


async def _authorize_project_operator(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    user: User,
    for_update: bool = False,
) -> User:
    """Defence-in-depth for direct handler calls as well as router requests."""

    statement = (
        select(User)
        .join(
            Project,
            Project.organization_id == User.organization_id,
        )
        .where(
            User.id == user.id,
            User.organization_id.is_not(None),
            User.disabled_at.is_(None),
            Project.id == project_id,
            Project.organization_id.is_not(None),
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        statement = statement.with_for_update()
    resolved_user = (await db.execute(statement)).scalar_one_or_none()
    if resolved_user is None:
        raise HTTPException(status_code=404, detail="not_found")
    if not check_role(resolved_user.role, "operator"):
        raise HTTPException(status_code=403, detail="insufficient_role")
    return resolved_user


def _owned_key_query(
    *,
    project_id: uuid.UUID,
    organization_id: uuid.UUID,
    key_id: uuid.UUID | None = None,
    for_update: bool = False,
):
    statement = (
        select(ApiKey)
        .join(User, ApiKey.user_id == User.id)
        .join(Project, ApiKey.project_id == Project.id)
        .where(
            ApiKey.project_id == project_id,
            ApiKey.project_id.is_not(None),
            ApiKey.user_id.is_not(None),
            Project.id == project_id,
            Project.organization_id == organization_id,
            Project.organization_id.is_not(None),
            User.organization_id == organization_id,
            User.organization_id.is_not(None),
            User.organization_id == Project.organization_id,
        )
        .execution_options(populate_existing=True)
    )
    if key_id is not None:
        statement = statement.where(ApiKey.id == key_id)
    if for_update:
        statement = statement.with_for_update()
    return statement


def _metadata(key: ApiKey) -> MCPKeyMetadata:
    return MCPKeyMetadata(
        id=key.id,
        label=key.label,
        created_at=key.created_at,
        revoked_at=key.revoked_at,
    )


@router.post("", response_model=MCPKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_mcp_key(
    project_id: uuid.UUID,
    body: MCPKeyCreate,
    response: Response,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> MCPKeyCreated:
    owner = await _authorize_project_operator(
        db,
        project_id=project_id,
        user=user,
        for_update=True,
    )

    raw_token = secrets.token_urlsafe(32)
    key = ApiKey(
        id=uuid.uuid4(),
        user_id=owner.id,
        project_id=project_id,
        token_hash=hash_token(raw_token),
        label=body.label,
    )
    db.add(key)
    try:
        await db.flush()
        await audit_record(
            actor=f"user:{owner.id}",
            action="mcp_key.create",
            target=str(key.id),
            project_id=project_id,
            details={"label": key.label},
            session=db,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # A digest collision is cryptographically negligible, but returning no
        # credential is safer than exposing a token whose row did not commit.
        raise HTTPException(status_code=409, detail="mcp_key_conflict_retry") from exc
    await db.refresh(key)

    # The response contains a bearer credential.  Prevent browser/proxy caches
    # from retaining the only cleartext copy.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    metadata = _metadata(key)
    return MCPKeyCreated(**metadata.model_dump(), token=raw_token)


@router.get("", response_model=list[MCPKeyMetadata])
async def list_mcp_keys(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[MCPKeyMetadata]:
    operator = await _authorize_project_operator(
        db,
        project_id=project_id,
        user=user,
    )
    assert operator.organization_id is not None
    keys = (
        await db.execute(
            _owned_key_query(
                project_id=project_id,
                organization_id=operator.organization_id,
            ).order_by(ApiKey.created_at.desc(), ApiKey.id)
        )
    ).scalars().all()
    return [_metadata(key) for key in keys]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_mcp_key(
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> None:
    """Atomically and idempotently revoke one exact project-owned key.

    A second DELETE of the same visible key is a successful no-op and retains
    the original ``revoked_at``.  Missing, foreign, NULL-scoped, and orphaned
    rows all return the same 404 without mutating anything.
    """

    operator = await _authorize_project_operator(
        db,
        project_id=project_id,
        user=user,
        for_update=True,
    )
    assert operator.organization_id is not None
    key = (
        await db.execute(
            _owned_key_query(
                project_id=project_id,
                organization_id=operator.organization_id,
                key_id=key_id,
                for_update=True,
            )
        )
    ).scalar_one_or_none()
    if key is None:
        await db.rollback()
        raise HTTPException(status_code=404, detail="not_found")
    if key.revoked_at is not None:
        await db.rollback()
        return

    key.revoked_at = datetime.now(timezone.utc)
    await audit_record(
        actor=f"user:{operator.id}",
        action="mcp_key.revoke",
        target=str(key.id),
        project_id=project_id,
        details={"label": key.label},
        session=db,
    )
    await db.commit()


__all__ = [
    "MCPKeyCreate",
    "MCPKeyCreated",
    "MCPKeyMetadata",
    "create_mcp_key",
    "list_mcp_keys",
    "revoke_mcp_key",
    "router",
]
