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

from app.data_sampler.maintenance import is_within_windows

if TYPE_CHECKING:  # avoid hard sqlalchemy dep for pure-function consumers
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.projects import ProjectDB


class PolicyViolation(Exception):
    """Raised when a DB policy check fails.

    Carries a machine-readable ``code`` that API routes translate into
    an appropriate HTTP status (403 for consent, 423 for scheduling).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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

_AWR_TOKENS = ("awr", "dba_hist_", "v$", "gv$")


def requires_awr(sql: str) -> bool:
    """Cheap detection of AWR/DMV-ish references inside a SQL string.

    Matches the patterns spec §7.4 lists as requiring explicit consent:
    ``DBA_HIST_*`` views, ``V$``/``GV$`` dynamic views, and the literal
    ``AWR`` token (used in e.g. ``DBMS_WORKLOAD_REPOSITORY.AWR_*`` calls).
    """
    text = sql.lower()
    return any(tok in text for tok in _AWR_TOKENS)


def enforce_policy(
    pdb: "ProjectDB | None",
    *,
    awr_required: bool = False,
    sensitive: list[str] | str | None = None,
    sql: str | None = None,
) -> None:
    """Raise ``PolicyViolation`` when a DB access violates operator policy.

    ``pdb`` being None is not fatal — that only means the project has
    not opted into per-DB policies yet; the global defaults still apply.
    """
    if pdb is None:
        return

    if awr_required and not pdb.allow_awr:
        raise PolicyViolation(
            "awr_not_consented",
            "AWR/DMV queries require allow_awr=true on this project_db.",
        )

    if not is_within_windows(list(pdb.maintenance_windows or [])):
        raise PolicyViolation(
            "outside_maintenance_window",
            "current time is outside the configured maintenance_windows.",
        )

    if sql is not None and pdb.sensitive_tables:
        hit = sensitive_tables_hit(sql, list(pdb.sensitive_tables))
        if hit is not None:
            raise PolicyViolation(
                "sensitive_table_blocked", f"sensitive table referenced: {hit}"
            )


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
