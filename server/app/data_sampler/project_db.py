"""Policy-lookup helpers for per-project database bindings (spec §12.2).

Separated from ``masking.py`` because resolving a ``ProjectDB`` row touches
the database, while the masking engine itself is pure. Consumers in
``api/data.py`` use these helpers to gate queries and to pass per-DB rules
into the masking engine.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid hard sqlalchemy dep for pure-function consumers
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.projects import ProjectDB


async def resolve_project_db(
    session: "AsyncSession",
    project_id: uuid.UUID,
    component_id: str,
) -> "ProjectDB | None":
    """Return the ProjectDB row for ``(project_id, component_id)`` or None.

    Absence is allowed so that data paths stay backward-compatible: callers
    fall back to the platform-wide masking defaults when no row exists.
    """
    from sqlalchemy import select

    from app.models.projects import ProjectDB

    result = await session.execute(
        select(ProjectDB).where(
            ProjectDB.project_id == project_id,
            ProjectDB.component_id == component_id,
        )
    )
    return result.scalar_one_or_none()


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def sensitive_tables_hit(sql: str, sensitive: list[str] | None) -> str | None:
    """Return the first sensitive table name referenced by ``sql``.

    Uses a simple case-insensitive identifier scan; schema-qualified names
    like ``dbo.Users`` are matched by the trailing ``Users`` token. This is
    deliberately liberal so we err on the side of blocking — the query
    landing zone is the last defence, not the only one.
    """
    if not sensitive:
        return None
    tokens = {m.group(0).lower() for m in _IDENT.finditer(sql)}
    for name in sensitive:
        if name and name.lower() in tokens:
            return name
    return None
