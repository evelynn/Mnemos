"""Data-access pass.

Scans ``+``/``-`` lines for SQL-ish patterns that reference a known
DataEntity. Deletes against sensitive tables are critical; any write against
a table flagged ``is_sensitive`` is critical; newly-introduced SELECT * on
a large table is a warning.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph import Node
from app.safety.review.types import Finding

_SQL_LINE = re.compile(
    r"(?i)\b(select\s+(?P<cols>\*|[^\n]+?)\s+from|insert\s+into|update|delete\s+from)\s+"
    r"(?P<table>[A-Za-z_][\w\.]*)"
)
_WRITE_VERBS = re.compile(r"(?i)^(insert|update|delete)")


def _entity_ids_containing(table: str) -> list[str]:
    """Heuristic match: any DataEntity whose id ends with .<schema>.<table>."""
    table = table.lower().strip(";")
    return [table]  # downstream query does ilike on this fragment


async def run(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    plan_id: uuid.UUID,  # noqa: ARG001
    diff: str,
) -> list[Finding]:
    findings: list[Finding] = []
    current_file = "?"
    for idx, raw in enumerate(diff.splitlines(), start=1):
        if raw.startswith("+++ "):
            current_file = raw[4:].strip()
            continue
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        body = raw[1:]
        m = _SQL_LINE.search(body)
        if not m:
            continue
        verb = m.group(1).lower().split()[0]
        table = m.group("table").strip(",; ()`\"'[]")
        if not table:
            continue

        entity = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.kind == "DataEntity",
                    Node.valid_to.is_(None),
                    Node.id.ilike(f"%.{table.lower()}"),
                )
                .order_by(Node.id)
                .limit(1)
            )
        ).scalar_one_or_none()

        sensitive = bool((entity.data if entity else {}) and (entity.data or {}).get("is_sensitive"))
        evidence = [
            {"kind": "node", "node_id": entity.id, "certainty": entity.certainty}
        ] if entity else []

        if sensitive:
            findings.append(
                Finding(
                    pass_name="data_access",
                    severity="critical",
                    rule="sensitive_entity_access",
                    location=f"{current_file}:{idx}",
                    message=(
                        f"Diff touches sensitive DataEntity {entity.id}. "
                        f"Sensitive data must be accessed through a reviewed path."
                    ),
                    evidence=evidence,
                )
            )
        elif _WRITE_VERBS.match(verb):
            findings.append(
                Finding(
                    pass_name="data_access",
                    severity="critical" if verb == "delete" else "warning",
                    rule=f"write_against_{verb}",
                    location=f"{current_file}:{idx}",
                    message=(
                        f"Diff issues {verb.upper()} against {table}. Confirm "
                        f"intent and that the surrounding code has a WHERE clause + tests."
                    ),
                    evidence=evidence,
                )
            )
        elif m.group("cols") and m.group("cols").strip() == "*":
            findings.append(
                Finding(
                    pass_name="data_access",
                    severity="info",
                    rule="select_star",
                    location=f"{current_file}:{idx}",
                    message=f"SELECT * from {table}; prefer explicit columns.",
                    evidence=evidence,
                )
            )
    return findings
