"""Ultrareview-style pipeline runner.

Executes each pass in turn, reports per-pass stats, runs the evidence
validator, and returns a :class:`ReviewReport` with verdict. Designed to
slot into the existing StageTracker so the Diffs tab shows a pipeline
card identical in shape to the analysis monitor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.safety.review import (
    contracts,
    data_access,
    impact,
    rules,
    second_opinion,
    validator,
)
from app.safety.review.types import Finding, Verdict, compute_verdict


@dataclass
class PassResult:
    name: str
    position: int
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None


@dataclass
class ReviewReport:
    verdict: Verdict
    passes: list[PassResult]
    findings: list[Finding]

    def as_jsonable(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "passes": [
                {
                    "name": p.name,
                    "position": p.position,
                    "findings": [f.as_jsonable() for f in p.findings],
                    "error": p.error,
                }
                for p in self.passes
            ],
            "findings": [f.as_jsonable() for f in self.findings],
        }


# Signature: session, project_id=, plan_id=, diff= [, prior_findings=]
Reviewer = Callable[..., Awaitable[list[Finding]]]

_PASSES: list[tuple[str, Reviewer]] = [
    ("rules", rules.run),
    ("contracts", contracts.run),
    ("data_access", data_access.run),
    ("impact", impact.run),
]


async def run_pipeline(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    diff: str,
    progress_cb: Callable[[PassResult], Awaitable[None]] | None = None,
) -> ReviewReport:
    pass_results: list[PassResult] = []
    aggregated: list[Finding] = []

    for i, (name, reviewer) in enumerate(_PASSES, start=1):
        pr = PassResult(name=name, position=i)
        try:
            pr.findings = await reviewer(
                session,
                project_id=project_id,
                plan_id=plan_id,
                diff=diff,
            )
        except Exception as exc:  # noqa: BLE001
            pr.error = f"{type(exc).__name__}: {exc}"
        aggregated.extend(pr.findings)
        pass_results.append(pr)
        if progress_cb is not None:
            await progress_cb(pr)

    # Second-opinion pass needs prior findings — run after rule-based passes.
    pr = PassResult(name="second_opinion", position=len(_PASSES) + 1)
    try:
        pr.findings = await second_opinion.run(
            session,
            project_id=project_id,
            plan_id=plan_id,
            diff=diff,
            prior_findings=aggregated,
        )
    except Exception as exc:  # noqa: BLE001
        pr.error = f"{type(exc).__name__}: {exc}"
    aggregated.extend(pr.findings)
    pass_results.append(pr)
    if progress_cb is not None:
        await progress_cb(pr)

    # Validator pass is implicit: gates every prior finding through the graph.
    validated = await validator.validate(
        session, project_id=project_id, findings=aggregated
    )
    validator_pr = PassResult(
        name="validator",
        position=len(_PASSES) + 2,
        findings=[f for f in validated if f.rule.endswith("_unverified")],
    )
    pass_results.append(validator_pr)
    if progress_cb is not None:
        await progress_cb(validator_pr)

    return ReviewReport(
        verdict=compute_verdict(validated),
        passes=pass_results,
        findings=validated,
    )
