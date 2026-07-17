"""Strict, provider-neutral contract for the Gate-B second opinion.

The generic L1-L3 summary contract cannot describe review severities or
references into a submitted diff.  This module owns that boundary instead:
provider prose is normalized once, then every finding is grounded against an
exact changed line from the bounded diff evidence shown to the model.

No raw provider response or diff line is returned by the grounding step.
Persistable evidence contains only a project-relative path, side, line number,
and SHA-256 digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.extractor.schema import parse_json_object
from app.safety.review.types import Finding


SECOND_OPINION_CONTRACT = "mnemos.second_opinion.v1"
MAX_SECOND_OPINION_FINDINGS = 32
MAX_SECOND_OPINION_MESSAGE_CHARS = 400
MAX_EVIDENCE_PER_FINDING = 8
MAX_DIFF_EVIDENCE_LINES = 128
MAX_DIFF_LINE_CHARS = 1_000
MAX_DIFF_EVIDENCE_TEXT_CHARS = 12_000

SecondOpinionCategory = Literal[
    "security",
    "correctness",
    "data_loss",
    "contract",
    "performance",
    "test_gap",
]
_CATEGORIES = frozenset(
    {
        "security",
        "correctness",
        "data_loss",
        "contract",
        "performance",
        "test_gap",
    }
)
_SEVERITIES = frozenset({"info", "warning", "critical"})
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_GROUNDED_CATEGORY_MESSAGES = {
    "security": "Independent review flagged a security concern at cited changed lines.",
    "correctness": (
        "Independent review flagged a correctness concern at cited changed lines."
    ),
    "data_loss": (
        "Independent review flagged a data-loss concern at cited changed lines."
    ),
    "contract": (
        "Independent review flagged a contract concern at cited changed lines."
    ),
    "performance": (
        "Independent review flagged a performance concern at cited changed lines."
    ),
    "test_gap": (
        "Independent review flagged a test-coverage concern at cited changed lines."
    ),
}
_TOP_LEVEL_KEYS = frozenset({"contract", "findings"})
_FINDING_KEYS = frozenset({"severity", "category", "message", "evidence"})
_EVIDENCE_KEYS = frozenset(
    {"kind", "path", "side", "line", "content_sha256"}
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HUNK = re.compile(
    r"^@@\s+-(?P<old>\d+)(?:,(?P<old_count>\d+))?\s+"
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))?\s+@@"
)
_NO_NEWLINE_MARKER = r"\ No newline at end of file"
_TEXT_DIFF_METADATA_PREFIXES = (
    "index ",
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
)


@dataclass(frozen=True, slots=True)
class SecondOpinionContractIssue:
    """A diagnostic that never echoes provider or source text."""

    path: str
    rule: str
    expected: str
    actual: str
    hint: str

    def __str__(self) -> str:
        return (
            f"{self.path}: {self.rule}; expected {self.expected}, "
            f"got {self.actual}. {self.hint}"
        )


class SecondOpinionSchemaError(ValueError):
    def __init__(self, issues: list[SecondOpinionContractIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(" | ".join(str(issue) for issue in self.issues))


def _actual(value: Any) -> str:
    if isinstance(value, str):
        return f"str(len={len(value)})"
    if isinstance(value, (list, tuple, dict, set)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def _issue(
    issues: list[SecondOpinionContractIssue],
    *,
    path: str,
    rule: str,
    expected: str,
    actual: Any,
    hint: str,
) -> None:
    issues.append(
        SecondOpinionContractIssue(
            path=path,
            rule=rule,
            expected=expected,
            actual=_actual(actual),
            hint=hint,
        )
    )


def _unknown_keys(
    issues: list[SecondOpinionContractIssue],
    *,
    path: str,
    actual: Mapping[str, Any],
    allowed: frozenset[str],
) -> None:
    count = len(set(actual) - allowed)
    if count:
        issues.append(
            SecondOpinionContractIssue(
                path=path,
                rule="unknown field",
                expected="only the documented contract fields",
                actual=f"unknown_key_count={count}",
                hint="Remove unsupported fields.",
            )
        )


def _normalized_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip().replace("\\", "/")
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    if (
        not path
        or path == "/dev/null"
        or path.startswith("/")
        or len(path) > 300
    ):
        return None
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return path


def normalize_second_opinion_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the sole canonical ``mnemos.second_opinion.v1`` payload."""

    if not isinstance(payload, Mapping):
        raise SecondOpinionSchemaError(
            [
                SecondOpinionContractIssue(
                    path="$",
                    rule="top-level value has the wrong shape",
                    expected="object",
                    actual=_actual(payload),
                    hint="Return the requested contract object.",
                )
            ]
        )

    issues: list[SecondOpinionContractIssue] = []
    _unknown_keys(issues, path="$", actual=payload, allowed=_TOP_LEVEL_KEYS)
    if payload.get("contract") != SECOND_OPINION_CONTRACT:
        _issue(
            issues,
            path="$.contract",
            rule="unknown contract discriminator",
            expected=SECOND_OPINION_CONTRACT,
            actual=payload.get("contract"),
            hint="Copy the discriminator exactly.",
        )

    findings_raw = payload.get("findings")
    canonical_findings: list[dict[str, Any]] = []
    if not isinstance(findings_raw, list):
        _issue(
            issues,
            path="$.findings",
            rule="field has the wrong type",
            expected=f"list with at most {MAX_SECOND_OPINION_FINDINGS} items",
            actual=findings_raw,
            hint="Use [] when there are no additional concerns.",
        )
    elif len(findings_raw) > MAX_SECOND_OPINION_FINDINGS:
        _issue(
            issues,
            path="$.findings",
            rule="too many findings",
            expected=f"at most {MAX_SECOND_OPINION_FINDINGS} items",
            actual=findings_raw,
            hint="Keep only distinct, material concerns.",
        )
    else:
        seen_findings: set[tuple[str, str, str]] = set()
        for index, raw in enumerate(findings_raw):
            path = f"$.findings[{index}]"
            if not isinstance(raw, Mapping):
                _issue(
                    issues,
                    path=path,
                    rule="finding has the wrong shape",
                    expected="object",
                    actual=raw,
                    hint="Emit one finding object per item.",
                )
                continue
            _unknown_keys(issues, path=path, actual=raw, allowed=_FINDING_KEYS)

            severity = raw.get("severity")
            if severity not in _SEVERITIES:
                _issue(
                    issues,
                    path=f"{path}.severity",
                    rule="unknown severity",
                    expected="info|warning|critical",
                    actual=severity,
                    hint="Use an explicit enum; do not encode severity in prose.",
                )
            category = raw.get("category")
            if category not in _CATEGORIES:
                _issue(
                    issues,
                    path=f"{path}.category",
                    rule="unknown category",
                    expected="|".join(sorted(_CATEGORIES)),
                    actual=category,
                    hint="Choose the closest documented review category.",
                )

            message_raw = raw.get("message")
            message: str | None = None
            if not isinstance(message_raw, str):
                _issue(
                    issues,
                    path=f"{path}.message",
                    rule="message has the wrong type",
                    expected=(
                        f"non-empty string up to "
                        f"{MAX_SECOND_OPINION_MESSAGE_CHARS} chars"
                    ),
                    actual=message_raw,
                    hint="Explain the concern without pasting source code.",
                )
            else:
                message = " ".join(message_raw.split())
                if not message or len(message) > MAX_SECOND_OPINION_MESSAGE_CHARS:
                    _issue(
                        issues,
                        path=f"{path}.message",
                        rule="message is empty or too long",
                        expected=(
                            f"1..{MAX_SECOND_OPINION_MESSAGE_CHARS} chars"
                        ),
                        actual=message_raw,
                        hint="Provide one concise explanation.",
                    )
                    message = None

            evidence_raw = raw.get("evidence")
            canonical_evidence: list[dict[str, Any]] = []
            if (
                not isinstance(evidence_raw, list)
                or not evidence_raw
                or len(evidence_raw) > MAX_EVIDENCE_PER_FINDING
            ):
                _issue(
                    issues,
                    path=f"{path}.evidence",
                    rule="finding is not bounded and grounded",
                    expected=(
                        f"1..{MAX_EVIDENCE_PER_FINDING} diff-line references"
                    ),
                    actual=evidence_raw,
                    hint="Cite only changed lines supplied in the request.",
                )
            else:
                seen_evidence: set[tuple[str, str, int, str]] = set()
                for ev_index, ev_raw in enumerate(evidence_raw):
                    ev_path = f"{path}.evidence[{ev_index}]"
                    if not isinstance(ev_raw, Mapping):
                        _issue(
                            issues,
                            path=ev_path,
                            rule="evidence has the wrong shape",
                            expected="diff_line object",
                            actual=ev_raw,
                            hint="Copy one supplied diff-line reference.",
                        )
                        continue
                    _unknown_keys(
                        issues,
                        path=ev_path,
                        actual=ev_raw,
                        allowed=_EVIDENCE_KEYS,
                    )
                    if ev_raw.get("kind") != "diff_line":
                        _issue(
                            issues,
                            path=f"{ev_path}.kind",
                            rule="unknown evidence kind",
                            expected="diff_line",
                            actual=ev_raw.get("kind"),
                            hint="Use only supplied diff-line evidence.",
                        )
                    ev_file = _normalized_path(ev_raw.get("path"))
                    if ev_file is None:
                        _issue(
                            issues,
                            path=f"{ev_path}.path",
                            rule="invalid project-relative path",
                            expected="bounded project-relative path",
                            actual=ev_raw.get("path"),
                            hint="Copy the path exactly from supplied evidence.",
                        )
                    side = ev_raw.get("side")
                    if side not in {"old", "new"}:
                        _issue(
                            issues,
                            path=f"{ev_path}.side",
                            rule="unknown diff side",
                            expected="old|new",
                            actual=side,
                            hint="Copy the side exactly from supplied evidence.",
                        )
                    line = ev_raw.get("line")
                    if (
                        isinstance(line, bool)
                        or not isinstance(line, int)
                        or line < 1
                    ):
                        _issue(
                            issues,
                            path=f"{ev_path}.line",
                            rule="invalid line number",
                            expected="positive integer",
                            actual=line,
                            hint="Copy the line number exactly.",
                        )
                    digest = ev_raw.get("content_sha256")
                    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
                        _issue(
                            issues,
                            path=f"{ev_path}.content_sha256",
                            rule="invalid content digest",
                            expected="64 lowercase hexadecimal chars",
                            actual=digest,
                            hint="Copy the digest exactly from supplied evidence.",
                        )
                    if (
                        ev_file is not None
                        and side in {"old", "new"}
                        and isinstance(line, int)
                        and not isinstance(line, bool)
                        and line >= 1
                        and isinstance(digest, str)
                        and _HEX64.fullmatch(digest)
                    ):
                        key = (ev_file, side, line, digest)
                        if key in seen_evidence:
                            _issue(
                                issues,
                                path=ev_path,
                                rule="duplicate evidence reference",
                                expected="unique changed lines",
                                actual=ev_raw,
                                hint="Remove the duplicate reference.",
                            )
                        seen_evidence.add(key)
                        canonical_evidence.append(
                            {
                                "kind": "diff_line",
                                "path": ev_file,
                                "side": side,
                                "line": line,
                                "content_sha256": digest,
                            }
                        )

            if (
                severity in _SEVERITIES
                and category in _CATEGORIES
                and message is not None
                and canonical_evidence
            ):
                finding_key = (severity, category, message.casefold())
                if finding_key in seen_findings:
                    _issue(
                        issues,
                        path=path,
                        rule="duplicate finding",
                        expected="distinct concern",
                        actual=raw,
                        hint="Merge duplicate evidence into one finding.",
                    )
                seen_findings.add(finding_key)
                canonical_findings.append(
                    {
                        "severity": severity,
                        "category": category,
                        "message": message,
                        "evidence": canonical_evidence,
                    }
                )

    if issues:
        raise SecondOpinionSchemaError(issues)
    return {
        "contract": SECOND_OPINION_CONTRACT,
        "findings": canonical_findings,
    }


def normalize_second_opinion_candidate_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the privacy-minimized canonical payload stored as a candidate.

    Provider prose is useful only while validating the outer contract. The
    grounding algorithm owns user-visible wording, so the durable candidate
    retains severity/category/evidence and replaces free text with a bounded,
    deterministic digest placeholder. Structurally identical signals are
    merged because grounding would merge them as well.
    """

    canonical = normalize_second_opinion_payload(payload)
    minimized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in canonical["findings"]:
        evidence_json = json.dumps(
            finding["evidence"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        evidence_digest = hashlib.sha256(
            evidence_json.encode("utf-8")
        ).hexdigest()
        key = (
            finding["severity"],
            finding["category"],
            evidence_digest,
        )
        if key in seen:
            continue
        seen.add(key)
        minimized.append(
            {
                "severity": finding["severity"],
                "category": finding["category"],
                "message": (
                    "Mnemos candidate signal "
                    f"{finding['category']}:{evidence_digest[:16]}."
                ),
                "evidence": finding["evidence"],
            }
        )
    minimized_payload = {
        "contract": SECOND_OPINION_CONTRACT,
        "findings": minimized,
    }
    # Prove the produced dialect remains strict v1 and idempotent at replay.
    return normalize_second_opinion_payload(minimized_payload)


def parse_second_opinion_text(text: str) -> dict[str, Any]:
    """Parse one provider text response and normalize it exactly once."""

    try:
        parsed = parse_json_object(text)
    except Exception as exc:
        # The shared parser's diagnostics are already redacted, but expose one
        # contract-specific exception type to the provider adapter.
        raise SecondOpinionSchemaError(
            [
                SecondOpinionContractIssue(
                    path="$",
                    rule="invalid JSON response",
                    expected="one second-opinion object",
                    actual=_actual(text),
                    hint="Return strict JSON only.",
                )
            ]
        ) from exc
    return normalize_second_opinion_payload(parsed)


@dataclass(frozen=True, slots=True)
class DiffLineEvidence:
    path: str
    side: Literal["old", "new"]
    line: int
    content_sha256: str
    text: str

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (self.path, self.side, self.line, self.content_sha256)

    def prompt_value(self) -> dict[str, Any]:
        return {
            "kind": "diff_line",
            "path": self.path,
            "side": self.side,
            "line": self.line,
            "content_sha256": self.content_sha256,
            "text": self.text,
        }

    def reference_value(self) -> dict[str, Any]:
        value = self.prompt_value()
        value.pop("text")
        return value


@dataclass(frozen=True, slots=True)
class DiffEvidencePack:
    lines: tuple[DiffLineEvidence, ...]
    complete: bool


def _diff_header_path(raw: str) -> tuple[str | None, bool]:
    """Return a supported canonical path and whether the header is valid.

    Git's C-quoted pathname grammar is deliberately not guessed here.  An
    unsupported header must lower coverage instead of producing a plausible
    but wrong grounding coordinate.  ``/dev/null`` is valid only as the empty
    side of an add/delete hunk and therefore has no project-relative path.
    """

    value = raw.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None, True
    if value.startswith('"') or value.endswith('"'):
        return None, False
    normalized = _normalized_path(value)
    return normalized, normalized is not None


def collect_diff_line_evidence(diff: str) -> DiffEvidencePack:
    """Extract a bounded, exact set of changed unified-diff lines.

    Unsupported/oversized records are omitted and make ``complete`` false;
    the caller can surface that coverage fact without sending partial lines or
    raw host paths to the provider.
    """

    old_path: str | None = None
    new_path: str | None = None
    old_line: int | None = None
    new_line: int | None = None
    old_remaining = 0
    new_remaining = 0
    in_hunk = False
    saw_hunk = False
    old_header_seen = False
    new_header_seen = False
    unit_requires_hunk = False
    allow_no_newline_marker = False
    records: list[DiffLineEvidence] = []
    text_chars = 0
    complete = True

    for raw in diff.splitlines():
        hunk = _HUNK.match(raw)
        if hunk:
            # A new hunk cannot truncate the one before it.  Continue parsing
            # for bounded evidence, but never restore complete coverage.
            if in_hunk or old_remaining != 0 or new_remaining != 0:
                complete = False
            allow_no_newline_marker = False
            old_line = int(hunk.group("old"))
            new_line = int(hunk.group("new"))
            old_remaining = int(hunk.group("old_count") or "1")
            new_remaining = int(hunk.group("new_count") or "1")
            if (
                not old_header_seen
                or not new_header_seen
                or (old_remaining == 0 and new_remaining == 0)
                or (old_remaining > 0 and (old_line < 1 or old_path is None))
                or (new_remaining > 0 and (new_line < 1 or new_path is None))
            ):
                complete = False
            in_hunk = old_remaining > 0 or new_remaining > 0
            saw_hunk = True
            unit_requires_hunk = False
            continue

        if raw == _NO_NEWLINE_MARKER:
            if not allow_no_newline_marker:
                complete = False
            allow_no_newline_marker = False
            continue
        if raw.startswith("\\"):
            # Only Git's exact no-newline marker is non-consuming syntax.
            complete = False
            allow_no_newline_marker = False
            continue

        if not in_hunk:
            allow_no_newline_marker = False
            if raw.startswith("diff --git "):
                if unit_requires_hunk:
                    complete = False
                old_path = None
                new_path = None
                old_header_seen = False
                new_header_seen = False
                unit_requires_hunk = True
                continue
            if raw.startswith("--- "):
                # ``---`` begins a new unified-diff file header when the
                # previous hunk's declared counts are already exhausted.
                old_path, supported = _diff_header_path(raw[4:])
                if not supported:
                    complete = False
                old_header_seen = True
                new_header_seen = False
                new_path = None
                unit_requires_hunk = True
                old_line = None
                new_line = None
                continue
            if raw.startswith("+++ "):
                if not old_header_seen or new_header_seen:
                    complete = False
                new_path, supported = _diff_header_path(raw[4:])
                if not supported:
                    complete = False
                new_header_seen = True
                continue
            if raw.startswith(_TEXT_DIFF_METADATA_PREFIXES):
                continue
            # Binary/combined/submodule/unknown patch forms are not source
            # evidence.  Extra +/-/context rows after a declared hunk is
            # exhausted land here and must make coverage incomplete.
            complete = False
            continue

        side: Literal["old", "new"] | None = None
        line: int | None = None
        path: str | None = None
        text = ""
        if raw.startswith("+"):
            if new_remaining <= 0:
                complete = False
                allow_no_newline_marker = False
                continue
            side = "new"
            line = new_line
            path = new_path
            text = raw[1:]
            new_line += 1
            new_remaining -= 1
        elif raw.startswith("-"):
            if old_remaining <= 0:
                complete = False
                allow_no_newline_marker = False
                continue
            side = "old"
            line = old_line
            path = old_path
            text = raw[1:]
            old_line += 1
            old_remaining -= 1
        elif raw.startswith(" "):
            if old_remaining <= 0 or new_remaining <= 0:
                complete = False
                allow_no_newline_marker = False
                continue
            old_line += 1
            new_line += 1
            old_remaining -= 1
            new_remaining -= 1
        else:
            complete = False
            allow_no_newline_marker = False
            continue

        allow_no_newline_marker = True
        if old_remaining == 0 and new_remaining == 0:
            in_hunk = False

        if side is None:
            continue

        if (
            path is None
            or len(text) > MAX_DIFF_LINE_CHARS
            or len(records) >= MAX_DIFF_EVIDENCE_LINES
            or text_chars + len(text) > MAX_DIFF_EVIDENCE_TEXT_CHARS
        ):
            complete = False
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        records.append(
            DiffLineEvidence(
                path=path,
                side=side,
                line=line,
                content_sha256=digest,
                text=text,
            )
        )
        text_chars += len(text)

    if (
        in_hunk
        or old_remaining != 0
        or new_remaining != 0
        or unit_requires_hunk
        or old_header_seen != new_header_seen
        or (diff.strip() and not saw_hunk)
    ):
        complete = False
    return DiffEvidencePack(lines=tuple(records), complete=complete)


@dataclass(frozen=True, slots=True)
class GroundedSecondOpinion:
    findings: tuple[Finding, ...]
    rejected_findings: int
    canonical_payload: dict[str, Any]


def ground_second_opinion_payload(
    payload: Mapping[str, Any],
    *,
    evidence: DiffEvidencePack,
) -> GroundedSecondOpinion:
    """Accept only findings citing exact lines shown to the provider."""

    canonical = normalize_second_opinion_payload(payload)
    allowed = {line.key: line for line in evidence.lines}
    grounded_rows: list[dict[str, Any]] = []
    rejected = 0
    for raw in canonical["findings"]:
        references: list[dict[str, Any]] = []
        for item in raw["evidence"]:
            key = (
                item["path"],
                item["side"],
                item["line"],
                item["content_sha256"],
            )
            matched = allowed.get(key)
            if matched is None:
                break
            references.append(matched.reference_value())
        else:
            grounded_rows.append(
                {
                    "severity": raw["severity"],
                    "category": raw["category"],
                    "evidence": references,
                }
            )
            continue
        rejected += 1

    # Provider prose is transient and never becomes a durable review product.
    # Even a correctly grounded model can copy a changed secret/source line
    # into its free-form message.  Build the user-visible text solely from the
    # validated enum and evidence shape, then deterministically merge identical
    # structured signals and number same-category signals when necessary.
    unique_rows: dict[
        tuple[str, str, tuple[tuple[str, str, int, str], ...]],
        dict[str, Any],
    ] = {}
    for row in grounded_rows:
        evidence_key = tuple(
            (
                item["path"],
                item["side"],
                item["line"],
                item["content_sha256"],
            )
            for item in row["evidence"]
        )
        unique_rows[(row["severity"], row["category"], evidence_key)] = row
    ordered = sorted(
        unique_rows.values(),
        key=lambda row: (
            _SEVERITY_ORDER[row["severity"]],
            row["category"],
            tuple(
                (
                    item["path"],
                    item["side"],
                    item["line"],
                    item["content_sha256"],
                )
                for item in row["evidence"]
            ),
        ),
    )
    group_totals: dict[tuple[str, str], int] = {}
    for row in ordered:
        group = (row["severity"], row["category"])
        group_totals[group] = group_totals.get(group, 0) + 1
    group_positions: dict[tuple[str, str], int] = {}
    findings: list[Finding] = []
    canonical_findings: list[dict[str, Any]] = []
    for row in ordered:
        group = (row["severity"], row["category"])
        position = group_positions.get(group, 0) + 1
        group_positions[group] = position
        message = _GROUNDED_CATEGORY_MESSAGES[row["category"]]
        if group_totals[group] > 1:
            message = (
                f"{message[:-1]} (signal {position}/{group_totals[group]})."
            )
        first = row["evidence"][0]
        findings.append(
            Finding(
                pass_name="second_opinion",
                severity=row["severity"],
                rule=f"llm_{row['category']}",
                location=f"{first['path']}:{first['line']}",
                message=message,
                evidence=row["evidence"],
            )
        )
        canonical_findings.append(
            {
                "severity": row["severity"],
                "category": row["category"],
                "message": message,
                "evidence": row["evidence"],
            }
        )
    return GroundedSecondOpinion(
        tuple(findings),
        rejected,
        {
            "contract": SECOND_OPINION_CONTRACT,
            "findings": canonical_findings,
        },
    )


__all__ = [
    "DiffEvidencePack",
    "DiffLineEvidence",
    "GroundedSecondOpinion",
    "SECOND_OPINION_CONTRACT",
    "SecondOpinionSchemaError",
    "collect_diff_line_evidence",
    "ground_second_opinion_payload",
    "normalize_second_opinion_candidate_payload",
    "normalize_second_opinion_payload",
    "parse_second_opinion_text",
]
