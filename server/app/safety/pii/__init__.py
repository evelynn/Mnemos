"""Country-specific PII detectors layered on top of the regex baseline.

The default :class:`app.data_sampler.masking.MaskingEngine` already
catches RRN-shaped strings and Korean mobile numbers, but its checks
are pattern-only. This module adds the lightweight verification each
of the Korean numbering schemes actually defines (RRN checksum,
foreigner-registration checksum, credit-card Luhn, driver-license
format) so an audit log can tell a *real* RRN from a six-then-seven
digit string that happened to look like one.

No new third-party dependencies — pure stdlib regex + arithmetic.
Presidio + spaCy KO NER stays a Phase-2 opt-in (see backlog).
"""

from app.safety.pii.regex_ko import (
    KoreanPIIRecognizer,
    is_valid_card_luhn,
    is_valid_foreigner_id,
    is_valid_korean_drivers_license,
    is_valid_rrn,
)

__all__ = [
    "KoreanPIIRecognizer",
    "is_valid_rrn",
    "is_valid_foreigner_id",
    "is_valid_card_luhn",
    "is_valid_korean_drivers_license",
]
