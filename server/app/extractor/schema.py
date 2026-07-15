"""Canonical, bounded schema for evidence-bearing L1-L3 summaries.

The provider envelopes differ (Anthropic ``Message.content`` versus Claude
Agent SDK text blocks), but nothing downstream should depend on those
dialects.  Provider adapters parse their text and call
``normalize_extractor_payload`` exactly once before constructing an
``ExtractorResult`` or persisting claims.

This module intentionally has no provider or database dependencies.  The
normalizer is pure, non-mutating, and idempotent; graph-reference existence is
checked separately by :mod:`app.extractor.validator`.

L4 flow narration is deliberately a different discriminated contract defined
in :mod:`app.extractor.agent_flow`.  Its ``{contract, section, data}`` records
must never be passed through this module as if they were generic
``{claim, evidence}`` rows; the shared validator exposes the two views
separately.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


MAX_SUMMARY_CHARS = 4_000
MAX_DETAILED_CHARS = 12_000
MAX_CLAIMS = 64
MAX_CLAIM_CHARS = 2_000
MAX_EVIDENCE_PER_CLAIM = 32
MAX_EVIDENCE_ID_CHARS = 1_024
MAX_OPEN_QUESTIONS = 32
MAX_QUESTION_CHARS = 1_000

_TOP_LEVEL_KEYS = frozenset(
    {"summary", "detailed", "claims", "open_questions"}
)
_CLAIM_KEYS = frozenset({"claim", "evidence"})
_EVIDENCE_KEYS = frozenset({"kind", "node_id", "edge_id", "certainty"})
_CERTAINTIES = frozenset({"verified", "asserted", "inferred"})


@dataclass(frozen=True)
class ContractIssue:
    """One safe, actionable contract diagnostic.

    ``actual`` deliberately reports only a type/size description.  Model
    output can contain source code or secrets, so validation errors must not
    echo raw values into logs.
    """

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


class ExtractorSchemaError(ValueError):
    """Raised when parsed model output cannot become the canonical schema."""

    def __init__(self, issues: list[ContractIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(" | ".join(str(issue) for issue in self.issues))


def _actual(value: Any) -> str:
    if isinstance(value, str):
        return f"str(len={len(value)})"
    if isinstance(value, (list, tuple, dict, set)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def _issue(
    issues: list[ContractIssue],
    *,
    path: str,
    rule: str,
    expected: str,
    actual: Any,
    hint: str,
) -> None:
    issues.append(
        ContractIssue(
            path=path,
            rule=rule,
            expected=expected,
            actual=_actual(actual),
            hint=hint,
        )
    )


def _unknown_keys_issue(
    issues: list[ContractIssue],
    *,
    path: str,
    rule: str,
    expected: str,
    count: int,
) -> None:
    """Report only a static object path and count, never provider key names."""

    issues.append(
        ContractIssue(
            path=path,
            rule=rule,
            expected=expected,
            actual=f"unknown_key_count={count}",
            hint=f"Remove the {count} unsupported field(s).",
        )
    )


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a provider text reply into one JSON object.

    The supported producer dialects are strict JSON, a single Markdown JSON
    fence, and short leading/trailing prose around one object.  This mirrors
    common responses from both supported providers without attempting unsafe
    JSON repair or accepting multiple competing objects.
    """

    if not isinstance(text, str):
        raise ExtractorSchemaError(
            [
                ContractIssue(
                    path="$",
                    rule="provider payload must be text",
                    expected="str containing one JSON object",
                    actual=_actual(text),
                    hint="Pass the provider text block, not its envelope.",
                )
            ]
        )

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    start = candidate.find("{")
    if start < 0:
        raise ExtractorSchemaError(
            [
                ContractIssue(
                    path="$",
                    rule="response has no JSON object",
                    expected="one JSON object",
                    actual=_actual(candidate),
                    hint="Return the requested object without commentary.",
                )
            ]
        )
    leading = candidate[:start].strip()
    # Commentary is tolerated, but JSON container punctuation outside the
    # decoded object means the object is nested in (or adjacent to) another
    # JSON value.  In particular, ``[{...}]`` must not be laundered into the
    # inner object merely because ``raw_decode`` starts at the first ``{``.
    if any(token in leading for token in "[]{}"):
        raise ExtractorSchemaError(
            [
                ContractIssue(
                    path="$",
                    rule="top-level JSON container is not an object",
                    expected="exactly one JSON object",
                    actual=_actual(leading),
                    hint="Remove enclosing arrays or competing JSON values.",
                )
            ]
        )
    try:
        parsed, end = decoder.raw_decode(candidate[start:])
    except json.JSONDecodeError as exc:
        raise ExtractorSchemaError(
            [
                ContractIssue(
                    path=f"$@char{start + exc.pos}",
                    rule="invalid JSON",
                    expected="valid JSON object syntax",
                    actual=_actual(candidate),
                    hint="Remove trailing commas and close every string/container.",
                )
            ]
        ) from exc

    # Only whitespace, a closing Markdown fence, or prose may follow.  A
    # second object is ambiguous and must not be selected silently.
    trailing = candidate[start + end :].strip()
    if trailing.startswith("```"):
        trailing = trailing[3:].strip()
    if any(token in trailing for token in "[]{}"):
        raise ExtractorSchemaError(
            [
                ContractIssue(
                    path="$",
                    rule="multiple JSON values are ambiguous",
                    expected="exactly one JSON object",
                    actual=_actual(trailing),
                    hint="Return only the final summary object.",
                )
            ]
        )
    if not isinstance(parsed, dict):
        raise ExtractorSchemaError(
            [
                ContractIssue(
                    path="$",
                    rule="top-level value has the wrong shape",
                    expected="object",
                    actual=_actual(parsed),
                    hint="Wrap the four summary fields in a JSON object.",
                )
            ]
        )
    return parsed


def _bounded_text(
    value: Any,
    *,
    path: str,
    maximum: int,
    issues: list[ContractIssue],
) -> str | None:
    if not isinstance(value, str):
        _issue(
            issues,
            path=path,
            rule="field has the wrong type",
            expected=f"non-empty str up to {maximum} chars",
            actual=value,
            hint="Emit a JSON string.",
        )
        return None
    normalized = value.strip()
    if not normalized:
        _issue(
            issues,
            path=path,
            rule="empty text is not useful",
            expected=f"non-empty str up to {maximum} chars",
            actual=value,
            hint="State a concise, evidence-grounded value.",
        )
        return None
    if len(normalized) > maximum:
        _issue(
            issues,
            path=path,
            rule="text exceeds the bounded contract",
            expected=f"at most {maximum} chars",
            actual=value,
            hint="Condense the response; do not paste source code.",
        )
        return None
    return normalized


def normalize_extractor_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the sole canonical evidence-bearing summary representation.

    Supported legacy dialect: ``detailed``, ``claims`` and
    ``open_questions`` may be omitted; they normalize respectively to the
    summary text, ``[]`` and ``[]``.  No aliases are accepted, so conflicting
    names can never be resolved silently.
    """

    if not isinstance(payload, Mapping):
        raise ExtractorSchemaError(
            [
                ContractIssue(
                    path="$",
                    rule="top-level value has the wrong shape",
                    expected="object",
                    actual=_actual(payload),
                    hint="Return an object with summary, detailed, claims, and open_questions.",
                )
            ]
        )

    issues: list[ContractIssue] = []
    unknown_count = len(set(payload) - _TOP_LEVEL_KEYS)
    if unknown_count:
        _unknown_keys_issue(
            issues,
            path="$",
            rule="unknown top-level field",
            expected="summary|detailed|claims|open_questions",
            count=unknown_count,
        )

    summary = _bounded_text(
        payload.get("summary"),
        path="$.summary",
        maximum=MAX_SUMMARY_CHARS,
        issues=issues,
    )
    detailed_raw = payload.get("detailed", summary)
    detailed = _bounded_text(
        detailed_raw,
        path="$.detailed",
        maximum=MAX_DETAILED_CHARS,
        issues=issues,
    )

    claims_raw = payload.get("claims", [])
    canonical_claims: list[dict[str, Any]] = []
    if not isinstance(claims_raw, list):
        _issue(
            issues,
            path="$.claims",
            rule="field has the wrong type",
            expected=f"list with at most {MAX_CLAIMS} claims",
            actual=claims_raw,
            hint="Emit a JSON array; use [] when there are no grounded claims.",
        )
    elif len(claims_raw) > MAX_CLAIMS:
        _issue(
            issues,
            path="$.claims",
            rule="too many claims",
            expected=f"at most {MAX_CLAIMS} claims",
            actual=claims_raw,
            hint="Keep only the highest-value claims supported by supplied evidence.",
        )
    else:
        seen_claims: set[str] = set()
        for claim_index, claim_raw in enumerate(claims_raw):
            claim_path = f"$.claims[{claim_index}]"
            if not isinstance(claim_raw, Mapping):
                _issue(
                    issues,
                    path=claim_path,
                    rule="claim has the wrong shape",
                    expected="object with claim and evidence",
                    actual=claim_raw,
                    hint="Emit one claim object per array item.",
                )
                continue
            unknown_count = len(set(claim_raw) - _CLAIM_KEYS)
            if unknown_count:
                _unknown_keys_issue(
                    issues,
                    path=claim_path,
                    rule="unknown claim field",
                    expected="claim|evidence",
                    count=unknown_count,
                )
            claim_text = _bounded_text(
                claim_raw.get("claim"),
                path=f"{claim_path}.claim",
                maximum=MAX_CLAIM_CHARS,
                issues=issues,
            )
            if claim_text is not None:
                duplicate_key = " ".join(claim_text.casefold().split())
                if duplicate_key in seen_claims:
                    _issue(
                        issues,
                        path=f"{claim_path}.claim",
                        rule="duplicate claim",
                        expected="a unique evidence-grounded claim",
                        actual=claim_text,
                        hint="Merge duplicate claims and their evidence.",
                    )
                seen_claims.add(duplicate_key)

            evidence_raw = claim_raw.get("evidence")
            canonical_evidence: list[dict[str, str]] = []
            if not isinstance(evidence_raw, list) or not evidence_raw:
                _issue(
                    issues,
                    path=f"{claim_path}.evidence",
                    rule="claim is not grounded",
                    expected=(
                        "non-empty list with at most "
                        f"{MAX_EVIDENCE_PER_CLAIM} evidence references"
                    ),
                    actual=evidence_raw,
                    hint="Cite an input node_id or edge_id, or omit the claim.",
                )
            elif len(evidence_raw) > MAX_EVIDENCE_PER_CLAIM:
                _issue(
                    issues,
                    path=f"{claim_path}.evidence",
                    rule="too many evidence references",
                    expected=f"at most {MAX_EVIDENCE_PER_CLAIM} references",
                    actual=evidence_raw,
                    hint="Keep the smallest sufficient evidence set.",
                )
            else:
                seen_evidence: set[tuple[str, str]] = set()
                for evidence_index, evidence_item in enumerate(evidence_raw):
                    ev_path = f"{claim_path}.evidence[{evidence_index}]"
                    if not isinstance(evidence_item, Mapping):
                        _issue(
                            issues,
                            path=ev_path,
                            rule="evidence has the wrong shape",
                            expected="object with kind, matching id, and certainty",
                            actual=evidence_item,
                            hint="Emit one evidence object per array item.",
                        )
                        continue
                    unknown_count = len(set(evidence_item) - _EVIDENCE_KEYS)
                    if unknown_count:
                        _unknown_keys_issue(
                            issues,
                            path=ev_path,
                            rule="unknown evidence field",
                            expected="kind|node_id|edge_id|certainty",
                            count=unknown_count,
                        )

                    kind = evidence_item.get("kind")
                    expected_id_key = (
                        "node_id" if kind == "node" else
                        "edge_id" if kind == "edge" else None
                    )
                    if expected_id_key is None:
                        _issue(
                            issues,
                            path=f"{ev_path}.kind",
                            rule="unknown evidence kind",
                            expected="node|edge",
                            actual=kind,
                            hint="Use node for node_id or edge for edge_id.",
                        )
                        continue
                    conflicting_id_key = (
                        "edge_id" if expected_id_key == "node_id" else "node_id"
                    )
                    evidence_id = _bounded_text(
                        evidence_item.get(expected_id_key),
                        path=f"{ev_path}.{expected_id_key}",
                        maximum=MAX_EVIDENCE_ID_CHARS,
                        issues=issues,
                    )
                    if conflicting_id_key in evidence_item:
                        _issue(
                            issues,
                            path=f"{ev_path}.{conflicting_id_key}",
                            rule="conflicting evidence identifiers",
                            expected=f"only {expected_id_key} for kind={kind}",
                            actual=evidence_item[conflicting_id_key],
                            hint="Remove the identifier that does not match kind.",
                        )
                    certainty = evidence_item.get("certainty")
                    if certainty not in _CERTAINTIES:
                        _issue(
                            issues,
                            path=f"{ev_path}.certainty",
                            rule="unknown certainty",
                            expected="verified|asserted|inferred",
                            actual=certainty,
                            hint="Copy the certainty from supplied graph evidence.",
                        )
                    if evidence_id is not None and certainty in _CERTAINTIES:
                        evidence_key = (kind, evidence_id)
                        if evidence_key in seen_evidence:
                            _issue(
                                issues,
                                path=ev_path,
                                rule="duplicate evidence reference",
                                expected="a unique node or edge reference",
                                actual=evidence_item,
                                hint="Remove the duplicate reference.",
                            )
                        seen_evidence.add(evidence_key)
                        canonical_evidence.append(
                            {
                                "kind": kind,
                                expected_id_key: evidence_id,
                                "certainty": certainty,
                            }
                        )
            if claim_text is not None and canonical_evidence:
                canonical_claims.append(
                    {"claim": claim_text, "evidence": canonical_evidence}
                )

    questions_raw = payload.get("open_questions", [])
    canonical_questions: list[str] = []
    if not isinstance(questions_raw, list):
        _issue(
            issues,
            path="$.open_questions",
            rule="field has the wrong type",
            expected=f"list with at most {MAX_OPEN_QUESTIONS} strings",
            actual=questions_raw,
            hint="Emit a JSON array; use [] when there are no questions.",
        )
    elif len(questions_raw) > MAX_OPEN_QUESTIONS:
        _issue(
            issues,
            path="$.open_questions",
            rule="too many open questions",
            expected=f"at most {MAX_OPEN_QUESTIONS} questions",
            actual=questions_raw,
            hint="Keep only questions that materially change source analysis.",
        )
    else:
        seen_questions: set[str] = set()
        for question_index, question_raw in enumerate(questions_raw):
            question_path = f"$.open_questions[{question_index}]"
            question = _bounded_text(
                question_raw,
                path=question_path,
                maximum=MAX_QUESTION_CHARS,
                issues=issues,
            )
            if question is None:
                continue
            duplicate_key = " ".join(question.casefold().split())
            if duplicate_key in seen_questions:
                _issue(
                    issues,
                    path=question_path,
                    rule="duplicate open question",
                    expected="a unique unresolved question",
                    actual=question,
                    hint="Remove the duplicate question.",
                )
            seen_questions.add(duplicate_key)
            canonical_questions.append(question)

    if issues:
        raise ExtractorSchemaError(issues)

    # The checks above guarantee these are strings.  Keeping the assertion
    # local makes the canonical return type obvious to type checkers/readers.
    assert summary is not None and detailed is not None
    return {
        "summary": summary,
        "detailed": detailed,
        "claims": canonical_claims,
        "open_questions": canonical_questions,
    }


def parse_and_normalize_extractor_payload(text: str) -> dict[str, Any]:
    """Provider-boundary convenience: parse text, then canonicalize it."""

    return normalize_extractor_payload(parse_json_object(text))
