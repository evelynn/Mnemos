"""Evidence validator pass.

Each prior finding's evidence IDs are checked against the current graph.
If a node/edge referenced as evidence does not exist, the finding is
downgraded (critical→warning, warning→info) and tagged ``unverified``.
This mirrors spec §10.4 and the summariser claim validator.
"""

from __future__ import annotations

import uuid

from app.extractor.validator import validate_claims
from app.safety.review.types import Finding
from sqlalchemy.ext.asyncio import AsyncSession


_DOWNGRADE = {"critical": "warning", "warning": "info", "info": "info"}


async def validate(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    findings: list[Finding],
) -> list[Finding]:
    # Validate each finding's evidence individually so one bad ID doesn't
    # kill a whole reviewer's output.
    out: list[Finding] = []
    for f in findings:
        if not f.evidence:
            out.append(f)
            continue
        claim = {"claim": f.rule, "evidence": f.evidence}
        accepted, _ = await validate_claims(
            session, project_id=project_id, claims=[claim]
        )
        if accepted:
            out.append(f)
        else:
            out.append(
                Finding(
                    pass_name=f.pass_name,
                    severity=_DOWNGRADE.get(f.severity, "info"),  # type: ignore[arg-type]
                    rule=f.rule + "_unverified",
                    location=f.location,
                    message=f.message + " (evidence not found in graph — downgraded)",
                    evidence=f.evidence,
                )
            )
    return out
