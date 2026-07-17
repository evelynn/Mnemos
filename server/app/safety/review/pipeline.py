"""Ultrareview-style pipeline runner.

Executes each pass in turn, reports per-pass stats, runs the evidence
validator, and returns a :class:`ReviewReport` with verdict. Designed to
slot into the existing StageTracker so the Diffs tab shows a pipeline
card identical in shape to the analysis monitor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.review_revision import CanonicalReviewRevision
from app.safety.review import (
    contracts,
    data_access,
    impact,
    rules,
    second_opinion,
    validator,
)
from app.safety.review.types import Finding, Verdict, compute_verdict


Coverage = Literal["complete", "incomplete"]


@dataclass
class PassResult:
    name: str
    position: int
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    coverage: Coverage = "complete"

    def __post_init__(self) -> None:
        if self.coverage not in {"complete", "incomplete"}:
            raise ValueError("review pass coverage must be complete or incomplete")


@dataclass
class ReviewReport:
    verdict: Verdict
    passes: list[PassResult]
    findings: list[Finding]

    @property
    def coverage(self) -> Coverage:
        return (
            "complete"
            if all(result.coverage == "complete" for result in self.passes)
            else "incomplete"
        )

    def as_jsonable(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "coverage": self.coverage,
            "passes": [
                {
                    "name": p.name,
                    "position": p.position,
                    "findings": [f.as_jsonable() for f in p.findings],
                    "error": p.error,
                    "coverage": p.coverage,
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
    review_revision: CanonicalReviewRevision | None = None,
) -> ReviewReport:
    pass_results: list[PassResult] = []
    deterministic_findings: list[Finding] = []

    for i, (name, reviewer) in enumerate(_PASSES, start=1):
        pr = PassResult(name=name, position=i)
        try:
            pr.findings = await reviewer(
                session,
                project_id=project_id,
                plan_id=plan_id,
                diff=diff,
            )
        except Exception:  # noqa: BLE001
            # A pass exception may contain diff/provider fragments. Persist a
            # static code only; the exception object is never report data.
            # These deterministic passes are required Gate-B controls, so an
            # unavailable pass is itself a blocking finding. The optional LLM
            # pass below instead reports incomplete coverage without inventing
            # a model concern.
            pr.error = f"{name}_failed"
            pr.coverage = "incomplete"
            pr.findings = [
                Finding(
                    pass_name=name,
                    severity="critical",
                    rule=f"{name}_review_unavailable",
                    location="-",
                    message="Required deterministic review pass failed.",
                    evidence=[],
                )
            ]
        deterministic_findings.extend(pr.findings)
        pass_results.append(pr)
        if progress_cb is not None:
            await progress_cb(pr)

    # Second-opinion pass needs prior findings — run after rule-based passes.
    pr = PassResult(name="second_opinion", position=len(_PASSES) + 1)
    try:
        review = await second_opinion.run(
            session,
            project_id=project_id,
            plan_id=plan_id,
            diff=diff,
            prior_findings=deterministic_findings,
            review_revision=review_revision,
        )
        pr.findings = list(review.findings)
        pr.coverage = review.coverage
        pr.error = review.failure_code
    except Exception:  # noqa: BLE001
        pr.error = "second_opinion_failed"
        pr.coverage = "incomplete"
    pass_results.append(pr)
    if progress_cb is not None:
        await progress_cb(pr)

    # Deterministic passes retain the legacy graph-evidence downgrade policy.
    # The model pass is different: second_opinion.run has already required an
    # exact diff-line path/side/line/hash and drops every ungrounded model
    # finding. Passing those references through the node/edge validator would
    # corrupt that contract and let invalid LLM claims influence the verdict.
    try:
        validated_deterministic = await validator.validate(
            session,
            project_id=project_id,
            findings=deterministic_findings,
        )
    except Exception:  # noqa: BLE001
        raise RuntimeError("review_validator_failed") from None
    validator_pr = PassResult(
        name="validator",
        position=len(_PASSES) + 2,
        findings=[
            finding
            for finding in validated_deterministic
            if finding.rule.endswith("_unverified")
        ],
    )
    pass_results.append(validator_pr)
    if progress_cb is not None:
        await progress_cb(validator_pr)

    validated = validated_deterministic + pr.findings
    # Coverage is authority, not presentation metadata. A clean subset of an
    # incomplete required Gate-B review cannot authorize submission or a
    # break-glass grant.
    complete = all(result.coverage == "complete" for result in pass_results)
    return ReviewReport(
        verdict=(compute_verdict(validated) if complete else "blocked"),
        passes=pass_results,
        findings=validated,
    )
