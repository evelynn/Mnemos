"""GitLab webhook receiver — Week 2 ships only signature verification.

Enqueueing analyses from push events lands in Week 3 alongside the first
real incremental analyzer outputs.
"""

import hmac
from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.audit.logger import record as audit_record
from app.db import get_session
from app.models.auth import PlatformSetting

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_SETTING_KEY = "gitlab_webhook_secret"


async def _secret(db: AsyncSession) -> str | None:
    row = (
        await db.execute(select(PlatformSetting).where(PlatformSetting.key == _SETTING_KEY))
    ).scalar_one_or_none()
    if row is None:
        return None
    value = row.value
    if isinstance(value, dict):
        return value.get("secret")
    return str(value) if value else None


@router.post("/gitlab")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
    x_gitlab_event: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    expected = await _secret(db)
    if expected:
        if not x_gitlab_token or not hmac.compare_digest(x_gitlab_token, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_webhook_token"
            )

    body = await request.json()
    await audit_record(
        actor="gitlab",
        action="webhook.received",
        target=x_gitlab_event or "unknown",
        details={
            "event": x_gitlab_event,
            "object_kind": body.get("object_kind"),
            "project": (body.get("project") or {}).get("path_with_namespace"),
        },
    )
    return {"status": "received"}
