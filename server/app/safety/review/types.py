"""Shared types for the review pipeline.

Keeping them in one place so reviewers, validator, and API all agree on
the shape of a ``Finding`` and on the ``Verdict`` computed from a batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["info", "warning", "critical"]
Verdict = Literal["clean", "warn", "blocked"]


@dataclass
class Finding:
    pass_name: str
    severity: Severity
    rule: str
    location: str
    message: str
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # ``Literal`` is static-only.  Gate-B must fail closed at runtime
        # instead of silently treating a foreign severity (historically
        # self-review's ``error``) as a clean result.
        if self.severity not in {"info", "warning", "critical"}:
            raise ValueError("finding severity must be info, warning, or critical")

    def as_jsonable(self) -> dict[str, Any]:
        return {
            "pass": self.pass_name,
            "severity": self.severity,
            "rule": self.rule,
            "location": self.location,
            "message": self.message,
            "evidence": self.evidence,
        }


def compute_verdict(findings: list[Finding]) -> Verdict:
    if any(
        finding.severity not in {"info", "warning", "critical"}
        for finding in findings
    ):
        raise ValueError("cannot compute a verdict with an unknown severity")
    if any(f.severity == "critical" for f in findings):
        return "blocked"
    if any(f.severity == "warning" for f in findings):
        return "warn"
    return "clean"
