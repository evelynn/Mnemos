"""Independent second-opinion pass.

Uses the same :class:`app.extractor.agent.Extractor` as Week-6 summarisers —
so we automatically reuse the Anthropic SDK wiring *and* the stub fallback
when no API key is available. The prompt asks for *new* findings only
(things the rule-based + graph-grounded passes missed), each with explicit
evidence IDs so the validator can vet them.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.agent import Extractor
from app.safety.review.types import Finding


async def run(
    session: AsyncSession,  # noqa: ARG001
    *,
    project_id: uuid.UUID,  # noqa: ARG001
    plan_id: uuid.UUID,  # noqa: ARG001
    diff: str,
    prior_findings: list[Finding],
) -> list[Finding]:
    """Invoked with ``prior_findings`` so the LLM can focus on new issues."""
    extractor = Extractor()
    evidence = [
        {"kind": "prior_finding", "node_id": f.rule, "data": f.as_jsonable()}
        for f in prior_findings[:50]
    ]
    evidence.append({"kind": "diff", "node_id": "diff", "data": diff[:4000]})

    try:
        result = await extractor.summarize(0, "review:second_opinion", evidence)
    except Exception as exc:  # noqa: BLE001
        return [
            Finding(
                pass_name="second_opinion",
                severity="info",
                rule="second_opinion_unavailable",
                location="-",
                message=f"LLM second opinion skipped: {exc}",
                evidence=[],
            )
        ]

    # The extractor's schema puts findings into ``claims``; parse severity
    # out of the claim string. Structure: {claim, evidence}.
    out: list[Finding] = []
    for c in result.claims or []:
        if not isinstance(c, dict):
            continue
        text = str(c.get("claim", ""))
        severity = "info"
        for token in ("critical", "warning", "info"):
            if token in text.lower():
                severity = token
                break
        ev: list[dict[str, Any]] = []
        for e in c.get("evidence", []) or []:
            if isinstance(e, dict):
                ev.append(e)
        out.append(
            Finding(
                pass_name="second_opinion",
                severity=severity,  # type: ignore[arg-type]
                rule="llm_concern",
                location="diff",
                message=text[:400],
                evidence=ev,
            )
        )
    return out
