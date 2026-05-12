"""Deterministic PII masking used for samples and live query results.

Phase-1 patterns match spec §8.3:
- full masking: password/token/secret/api_key/ssn/rrn columns
- partial masking: email/phone/name/address columns
- regex value matches: email, phone, RRN, credit card, IP

The engine is intentionally simple and synchronous so it can be reused
from both the sampler and the query executor.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FULL_MASK_COLUMNS = re.compile(
    r"(?i)\b(password|token|secret|api[_-]?key|ssn|rrn)\b"
)
PARTIAL_MASK_COLUMNS = re.compile(r"(?i)\b(email|phone|name|address|mobile)\b")

_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    # Korean mobile prefixes (010/011/016/017/018/019). Matched before
    # the more permissive PHONE pattern so the audit log shows that
    # PII identification was specific, not generic.
    (re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b"), "[PHONE_KR]"),
    (re.compile(r"\b\d{2,3}-?\d{3,4}-?\d{4}\b"), "[PHONE]"),
    # Korean RRN (Resident Registration Number): YYMMDD-NNNNNNN.
    (re.compile(r"\b\d{6}-?\d{7}\b"), "[RRN]"),
    (re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"), "[CARD]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
]


@dataclass
class MaskingEngine:
    full_mask_columns: re.Pattern[str] = FULL_MASK_COLUMNS
    partial_mask_columns: re.Pattern[str] = PARTIAL_MASK_COLUMNS
    # Per-project-DB overrides (spec §12.2 masking_rules). Checked before the
    # defaults so a project can add columns that platform-wide regex misses.
    extra_full_mask: tuple[re.Pattern[str], ...] = field(default_factory=tuple)
    extra_partial_mask: tuple[re.Pattern[str], ...] = field(default_factory=tuple)

    @classmethod
    def from_project_db(cls, masking_rules: dict | None) -> "MaskingEngine":
        """Build an engine that layers per-DB rules on top of the defaults.

        ``masking_rules`` shape: ``{"full": [regex, ...], "partial": [regex, ...]}``.
        Invalid regex entries are logged and skipped so a bad config cannot
        break the data path.
        """
        if not masking_rules:
            return cls()
        extra_full = tuple(_compile_patterns(masking_rules.get("full") or []))
        extra_partial = tuple(_compile_patterns(masking_rules.get("partial") or []))
        return cls(extra_full_mask=extra_full, extra_partial_mask=extra_partial)

    def mask_value(self, column: str, value: object) -> tuple[object, bool]:
        """Return (masked_value, was_masked)."""
        if value is None:
            return None, False
        if any(p.search(column) for p in self.extra_full_mask):
            return "***", True
        if self.full_mask_columns.search(column):
            return "***", True
        if any(p.search(column) for p in self.extra_partial_mask):
            s = str(value)
            if len(s) <= 3:
                return "***", True
            return s[:3] + "***", True
        if self.partial_mask_columns.search(column):
            s = str(value)
            if len(s) <= 3:
                return "***", True
            return s[:3] + "***", True

        text = str(value)
        masked = text
        changed = False
        for pattern, replacement in _VALUE_PATTERNS:
            new = pattern.sub(replacement, masked)
            if new != masked:
                changed = True
                masked = new
        return (masked if changed else value), changed


def _compile_patterns(raw: list[str]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for entry in raw:
        try:
            compiled.append(re.compile(entry, re.IGNORECASE))
        except re.error as exc:
            logger.warning("masking_rules: invalid regex %r (%s) - skipped", entry, exc)
    return compiled


def mask_rows(
    columns: list[str],
    rows: list[list[object]],
    engine: MaskingEngine | None = None,
) -> tuple[list[list[object]], list[bool], bool]:
    """Mask a result set column-wise. Returns (rows, per-column mask flags, any_masked)."""
    engine = engine or MaskingEngine()
    any_masked = False
    col_flags = [False] * len(columns)
    out_rows: list[list[object]] = []
    for row in rows:
        new_row: list[object] = []
        for i, value in enumerate(row):
            masked, was = engine.mask_value(columns[i], value)
            new_row.append(masked)
            if was:
                col_flags[i] = True
                any_masked = True
        out_rows.append(new_row)
    return out_rows, col_flags, any_masked
