"""Rule-based pass: ports the existing regex rules into the new pipeline.

This is the fast, offline first line of defence — secrets, connection
strings, forbidden SQL, TODO noise. Graph-level concerns live in the
other passes.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.safety.review.types import Finding, Severity
from app.safety.self_review import review_diff


_SEVERITY_MAP: dict[str, Severity] = {
    "error": "critical",
    "warning": "warning",
}


async def run(
    session: AsyncSession,  # noqa: ARG001 — stateless
    *,
    project_id: uuid.UUID,  # noqa: ARG001
    plan_id: uuid.UUID,  # noqa: ARG001
    diff: str,
) -> list[Finding]:
    result = review_diff(diff)
    findings: list[Finding] = []
    for source in result.findings:
        severity = _SEVERITY_MAP.get(source.severity)
        if severity is None:
            raise ValueError("self-review emitted an unsupported severity")
        findings.append(
            Finding(
                pass_name="rules",
                severity=severity,
                rule=source.rule,
                location=source.location,
                message=source.message,
                evidence=[],
            )
        )
    return findings
