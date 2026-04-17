import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models.audit import AuditLog


async def record(
    *,
    actor: str,
    action: str,
    target: str | None = None,
    project_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Append an audit log entry. Never raises into the caller path."""
    log = AuditLog(
        actor=actor,
        action=action,
        target=target,
        project_id=project_id,
        details=details,
    )
    if session is not None:
        session.add(log)
        await session.flush()
        return

    async with SessionLocal() as db:
        try:
            db.add(log)
            await db.commit()
        except Exception:
            await db.rollback()
