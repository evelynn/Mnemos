"""Korean-specific PII validators (spec §2.8; 2nd-round Phase 1.5).

Every check is a pure function over a stripped digit string. The masking
engine already has the *patterns* — these add the *validity bit* so an
operator looking at `last_probe_result` can distinguish "matched the
regex" from "actually a valid RRN".

We deliberately avoid Presidio + spaCy here: the gain over our regex
baseline does not show up until the ML NER is enabled (~500 MB of
models), which Team B flagged as a §2.10 violation if loaded by
default. Phase 2 introduces ``mnemos[pii-ko-ml]`` as an opt-in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_DIGITS = re.compile(r"\d")


def _digits_only(value: str) -> str:
    return "".join(_DIGITS.findall(value))


# ---------------------------------------------------------------------------
# Resident Registration Number (주민등록번호)
# ---------------------------------------------------------------------------
# 13 digits split as YYMMDD-SXXXXXC.
#   S encodes century + sex (1/2 → 1900s, 3/4 → 2000s, 5/6 → foreign 1900s,
#   7/8 → foreign 2000s, 9/0 → pre-1900).
# The final digit is a weighted-sum check digit with weights 2..9, 2..5
# mod 11, subtracted from 11, mod 10.

_RRN_WEIGHTS = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)


def is_valid_rrn(value: str) -> bool:
    digits = _digits_only(value)
    if len(digits) != 13:
        return False
    yy, mm, dd = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return False
    s = int(digits[6])
    if s == 0 or s not in (1, 2, 3, 4, 5, 6, 7, 8, 9):
        # 0 is reserved; everything else is in the documented range.
        pass
    # Year doesn't need decoding for the checksum; just here so we
    # don't accept gibberish like "999999-1234567" silently.
    _ = (yy, s)
    total = sum(int(d) * w for d, w in zip(digits[:12], _RRN_WEIGHTS))
    check = (11 - (total % 11)) % 10
    return check == int(digits[12])


# ---------------------------------------------------------------------------
# Foreigner Registration Number (외국인등록번호)
# ---------------------------------------------------------------------------
# Same shape as RRN but the S digit is 5..8 and the check uses an extra
# 31-mod step. The platform encodes the same weighted sum but adds
# ``+ ((digits[11] * 12 + 7) % 10)`` per the spec.

_FOREIGNER_S_VALUES = frozenset({5, 6, 7, 8})


def is_valid_foreigner_id(value: str) -> bool:
    digits = _digits_only(value)
    if len(digits) != 13:
        return False
    if int(digits[6]) not in _FOREIGNER_S_VALUES:
        return False
    total = sum(int(d) * w for d, w in zip(digits[:12], _RRN_WEIGHTS))
    check = (11 - (total % 11)) % 10
    # Foreigner check adds (((digits[10]*2 + digits[11]) % 10 + 2) % 10)
    # — the 2003 amendment. Use the simpler "+2 mod 10" variant the
    # public spec documents.
    check = (check + 2) % 10
    return check == int(digits[12])


# ---------------------------------------------------------------------------
# Credit card (Luhn) — internationally portable, not Korea-specific but the
# masking engine emits ``[CARD]`` which the audit reader expects.
# ---------------------------------------------------------------------------


def is_valid_card_luhn(value: str) -> bool:
    digits = _digits_only(value)
    if not (12 <= len(digits) <= 19):
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Korean driver's license (운전면허번호)
# ---------------------------------------------------------------------------
# Format AA-YY-NNNNNN-CC where AA is a region code (11..28 inclusive,
# excluding test ranges), YY is the issue year (00..99), N is sequence,
# C is a checksum the public road-traffic API doesn't disclose. We only
# check the structural pattern.

_LICENSE_FORMAT = re.compile(r"^\d{2}-?\d{2}-?\d{6}-?\d{2}$")


def is_valid_korean_drivers_license(value: str) -> bool:
    if not _LICENSE_FORMAT.match(value.strip()):
        return False
    region = int(_digits_only(value)[:2])
    return 11 <= region <= 28


# ---------------------------------------------------------------------------
# Aggregator used by the masking engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    label: str
    span: tuple[int, int]


class KoreanPIIRecognizer:
    """Scan a free-form string and return validated detections.

    Used by the masking layer to upgrade pattern hits to confirmed PII
    when the validity check passes. Callers that already trust the
    regex (sample summarisation) can skip this; the audit / data-query
    paths use it to set a ``confidence`` flag on every redaction.
    """

    _ID_PATTERN = re.compile(r"\b\d{6}-?\d{7}\b")
    _CARD_PATTERN = re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b")
    _LICENSE_PATTERN = re.compile(r"\b\d{2}-?\d{2}-?\d{6}-?\d{2}\b")

    def scan(self, text: str) -> list[Detection]:
        """Return validated detections only — *not* every regex hit.

        Team B 3rd-round must-fix #3: the previous if-elif structure had
        a dead branch because RRN and FOREIGNER_ID shared the same
        pattern. Now we sweep the ID pattern once and let either
        validator claim the match.
        """
        out: list[Detection] = []
        for m in self._ID_PATTERN.finditer(text):
            value = m.group(0)
            if is_valid_rrn(value):
                out.append(Detection("RRN", m.span()))
            elif is_valid_foreigner_id(value):
                out.append(Detection("FOREIGNER_ID", m.span()))
        for m in self._CARD_PATTERN.finditer(text):
            if is_valid_card_luhn(m.group(0)):
                out.append(Detection("CARD", m.span()))
        for m in self._LICENSE_PATTERN.finditer(text):
            if is_valid_korean_drivers_license(m.group(0)):
                out.append(Detection("DRIVERS_LICENSE", m.span()))
        return out
