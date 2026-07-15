"""GDPR: right-to-access (export) and right-to-erasure (delete).

Two admin-only endpoints. Both are intentionally strict about auditing
because "I deleted user X's data" is exactly the kind of action a
supervisory authority will ask for proof of.

- ``GET /api/v1/gdpr/users/{user_id}/export``
  Returns a single JSON document that includes every row the platform
  has attached to that user (user record, sessions are redis-only, audit
  entries referencing the user, any API keys).

- ``DELETE /api/v1/gdpr/users/{user_id}``
  Deletes the ``users`` row and rewrites audit entries so the actor
  becomes ``redacted:<uuid>``. The rewritten ids stay linkable to the
  original entries for forensic integrity without retaining the
  username.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.rbac import require_admin
from app.db import get_session
from app.models.audit import AuditLog
from app.models.auth import ApiKey, User
from app.user_scope import UserScopeNotFound, resolve_managed_user

router = APIRouter(prefix="/api/v1/gdpr", tags=["gdpr"])


async def _gdpr_user_in_actor_org(
    db: AsyncSession,
    actor: User,
    user_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> User:
    try:
        return await resolve_managed_user(
            db,
            actor_organization_id=actor.organization_id,
            user_id=user_id,
            for_update=for_update,
        )
    except UserScopeNotFound as exc:
        raise HTTPException(status_code=404, detail="not_found") from exc


@router.get("/users/{user_id}/export")
async def export_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    actor: User = Depends(require_admin),
) -> dict[str, Any]:
    user = await _gdpr_user_in_actor_org(db, actor, user_id)

    api_keys = (
        await db.execute(select(ApiKey).where(ApiKey.user_id == user_id))
    ).scalars().all()
    audit_entries = (
        await db.execute(
            select(AuditLog).where(AuditLog.actor == f"user:{user_id}")
        )
    ).scalars().all()

    await audit_record(
        actor=f"user:{actor.id}",
        action="gdpr.export",
        target=str(user_id),
    )

    return {
        "user": {
            "id": str(user.id),
            "username": user.username,
            "role": user.role,
            "organization_id": str(user.organization_id) if user.organization_id else None,
            "created_at": user.created_at.isoformat(),
        },
        "api_keys": [
            {
                "id": str(k.id),
                "label": k.label,
                "created_at": k.created_at.isoformat(),
                "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
            }
            for k in api_keys
        ],
        "audit_entries": [
            {
                "id": str(a.id),
                "action": a.action,
                "target": a.target,
                "created_at": a.occurred_at.isoformat() if a.occurred_at else None,
                "details": a.details,
            }
            for a in audit_entries
        ],
    }


@router.delete("/users/{user_id}")
async def erase_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    actor: User = Depends(require_admin),
) -> dict[str, Any]:
    user = await _gdpr_user_in_actor_org(
        db,
        actor,
        user_id,
        for_update=True,
    )

    # Refuse to self-delete — an admin locking themselves out mid-session
    # is the kind of accident that should never happen in one click.
    if user.id == actor.id:
        raise HTTPException(status_code=409, detail="cannot_delete_self")

    redacted_actor = f"redacted:{user_id}"
    # Rewrite audit entries so the forensic chain survives but the
    # identifiable ``user:<uuid>`` form is replaced.
    await db.execute(
        update(AuditLog)
        .where(AuditLog.actor == f"user:{user_id}")
        .values(actor=redacted_actor, details=None)
    )
    await db.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
    await db.delete(user)
    await db.commit()

    await audit_record(
        actor=f"user:{actor.id}",
        action="gdpr.erase",
        target=str(user_id),
        details={"redacted_actor": redacted_actor},
    )
    return {"status": "erased", "redacted_actor": redacted_actor}
