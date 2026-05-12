"""Deterministic PII masking used for samples and live query results.

Phase-1 patterns match spec §8.3:
- full masking: password/token/secret/api_key/ssn/rrn columns
- partial masking: email/phone/name/address columns
- regex value matches: email, phone, RRN, credit card, IP

The engine is intentionally simple and synchronous so it can be reused
from both the sampler and the query executor.

3rd-round addition (Team B must-fix #2 + #3): the Korean PII validators
in :mod:`app.safety.pii` are now invoked alongside the regex matches.
A 13-digit string that matches the RRN shape but fails the checksum
is masked with the ``[UNVERIFIED_RRN]`` label rather than being left
in the clear — leaking gibberish that *looks* like an RRN is still a
data-exposure problem under §2.8.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.safety.pii import (
    is_valid_card_luhn,
    is_valid_foreigner_id,
    is_valid_korean_drivers_license,
    is_valid_rrn,
)

logger = logging.getLogger(__name__)

FULL_MASK_COLUMNS = re.compile(
    r"(?i)\b(password|token|secret|api[_-]?key|ssn|rrn)\b"
)
# English keywords use ``\b`` word boundaries; Korean keywords cannot
# rely on ``\b`` (which is ASCII-only in CPython's stock regex
# engine), so they are matched bare. The 4th-round audit (A5/B5)
# pointed out that a column literally called ``주민번호`` was slipping
# through because the previous pattern was English-only. The Korean
# additions cover the columns most operators name in production
# Korean DBs: 이름 (name), 주소 (address), 전화 (phone), 휴대폰 (mobile),
# 주민 (RRN-bearing), 이메일 (email).
PARTIAL_MASK_COLUMNS = re.compile(
    r"(?i)(?:\b(email|phone|name|address|mobile)\b"
    r"|이름|주소|전화|휴대폰|주민|이메일)"
)

_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    # Korean mobile prefixes (010/011/016/017/018/019). Matched before
    # the more permissive PHONE pattern so the audit log shows that
    # PII identification was specific, not generic.
    (re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b"), "[PHONE_KR]"),
    (re.compile(r"\b\d{2,3}-?\d{3,4}-?\d{4}\b"), "[PHONE]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
]

# Validated patterns — same regex as above, but a positive match is
# label-replaced only after the validator agrees. RRN/foreigner share
# the same shape (`\d{6}-\d{7}`) so we try RRN first, then foreigner;
# if both fail the value is masked with ``[UNVERIFIED_NUMERIC_ID]``
# instead of being left in the clear (Team B 3rd-round must-fix).
_KR_RRN_PATTERN = re.compile(r"\b\d{6}-?\d{7}\b")
_CARD_PATTERN = re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b")
_LICENSE_PATTERN = re.compile(r"\b\d{2}-?\d{2}-?\d{6}-?\d{2}\b")


def _label_for_id_match(value: str) -> str:
    if is_valid_rrn(value):
        return "[RRN]"
    if is_valid_foreigner_id(value):
        return "[FOREIGNER_ID]"
    return "[UNVERIFIED_NUMERIC_ID]"


def _label_for_card_match(value: str) -> str:
    return "[CARD]" if is_valid_card_luhn(value) else "[UNVERIFIED_CARD]"


def _label_for_license_match(value: str) -> str:
    return (
        "[DRIVERS_LICENSE]"
        if is_valid_korean_drivers_license(value)
        else "[UNVERIFIED_LICENSE]"
    )


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
        # Validator-backed labels — checksum decides between verified
        # tag and ``UNVERIFIED_*``. Either way the digits are
        # replaced, so a bogus-but-RRN-shaped value does not leak.
        new = _KR_RRN_PATTERN.sub(lambda m: _label_for_id_match(m.group(0)), masked)
        if new != masked:
            changed = True
            masked = new
        new = _CARD_PATTERN.sub(lambda m: _label_for_card_match(m.group(0)), masked)
        if new != masked:
            changed = True
            masked = new
        new = _LICENSE_PATTERN.sub(
            lambda m: _label_for_license_match(m.group(0)), masked
        )
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
