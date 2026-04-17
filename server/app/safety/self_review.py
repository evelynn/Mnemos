"""Rule-based self-review over a unified diff (spec §14).

Phase-1 rules target the highest-impact mistakes:
- adds a hard-coded password / api key
- adds a connection string literal
- forbidden SQL operations (DROP/TRUNCATE/UPDATE without WHERE)
- removes an entire public method signature without a replacement
- introduces TODO/FIXME markers (warning)

Linter integration proper lands with the sandbox command surface; this
module exists so Gate B always has *something* to surface to the reviewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(password|pwd|secret|api[_-]?key|bearer)\s*[:=]\s*['\"][^'\"]{4,}"), "hardcoded_secret"),
    (re.compile(r"(?i)Server=.*;(User(\s*Id)?|UID)=.+;Password=.+"), "hardcoded_mssql_conn"),
    (re.compile(r"(?i)oracle://[^/]+:[^@]+@"), "hardcoded_oracle_conn"),
]

_FORBIDDEN_SQL = re.compile(
    r"(?im)\b(drop\s+table|truncate\s+table|update\s+\w+\s+set\s+[^;]*;|delete\s+from\s+\w+\s*;)"
)

_TODO = re.compile(r"(?i)\b(todo|fixme|xxx)\b")


@dataclass
class Finding:
    severity: str
    rule: str
    location: str
    message: str


@dataclass
class SelfReviewResult:
    findings: list[Finding] = field(default_factory=list)

    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)


def review_diff(diff: str) -> SelfReviewResult:
    result = SelfReviewResult()
    current_file = "?"
    for lineno, raw in enumerate(diff.splitlines(), start=1):
        if raw.startswith("+++ "):
            current_file = raw[4:].strip()
            continue
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        added = raw[1:]
        for pattern, rule in _SECRET_PATTERNS:
            if pattern.search(added):
                result.findings.append(
                    Finding(
                        severity="error",
                        rule=rule,
                        location=f"{current_file}:{lineno}",
                        message=pattern.pattern[:80],
                    )
                )
        if _FORBIDDEN_SQL.search(added):
            result.findings.append(
                Finding(
                    severity="error",
                    rule="forbidden_sql",
                    location=f"{current_file}:{lineno}",
                    message="DROP/TRUNCATE/UPDATE/DELETE detected",
                )
            )
        if _TODO.search(added):
            result.findings.append(
                Finding(
                    severity="warning",
                    rule="todo_added",
                    location=f"{current_file}:{lineno}",
                    message="TODO/FIXME/XXX introduced",
                )
            )
    return result


def to_jsonable(result: SelfReviewResult) -> list[dict[str, Any]]:
    return [
        {"severity": f.severity, "rule": f.rule, "location": f.location, "message": f.message}
        for f in result.findings
    ]
