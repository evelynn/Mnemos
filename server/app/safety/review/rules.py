"""Rule-based pass: ports the existing regex rules into the new pipeline.

This is the fast, offline first line of defence — secrets, connection
strings, forbidden SQL, TODO noise. Graph-level concerns live in the
other passes.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.safety.review.types import Finding
from app.safety.self_review import review_diff


async def run(
    session: AsyncSession,  # noqa: ARG001 — stateless
    *,
    project_id: uuid.UUID,  # noqa: ARG001
    plan_id: uuid.UUID,  # noqa: ARG001
    diff: str,
) -> list[Finding]:
    result = review_diff(diff)
    return [
        Finding(
            pass_name="rules",
            severity=f.severity,  # type: ignore[arg-type]
            rule=f.rule,
            location=f.location,
            message=f.message,
            evidence=[],
        )
        for f in result.findings
    ]
