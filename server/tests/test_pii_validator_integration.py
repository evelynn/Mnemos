"""End-to-end check that the Korean PII validators actually run in
``mask_value`` (Team B 3rd-round must-fix #3).
"""

from __future__ import annotations

from app.data_sampler.masking import MaskingEngine


def _valid_rrn() -> str:
    # Compute a checksum-valid RRN at runtime so the constant doesn't
    # rot if the algorithm ever changes.
    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    prefix = "900101123456"
    check = (11 - sum(int(d) * w for d, w in zip(prefix, weights)) % 11) % 10
    return prefix + str(check)


def test_valid_rrn_masked_as_verified():
    eng = MaskingEngine()
    out, was = eng.mask_value("memo", f"id is {_valid_rrn()} please")
    assert was
    assert "[RRN]" in str(out)


def test_invalid_rrn_shape_masked_as_unverified():
    """Bogus 13-digit string must NOT leak in the clear."""
    eng = MaskingEngine()
    out, was = eng.mask_value("memo", "id 1234567890123 here")
    assert was, "an unverified id-shape match must still be masked"
    text = str(out)
    assert "1234567890123" not in text
    assert "[UNVERIFIED_NUMERIC_ID]" in text


def test_valid_card_masked_as_verified():
    eng = MaskingEngine()
    # Visa test PAN.
    out, was = eng.mask_value("memo", "card 4111-1111-1111-1111")
    assert was
    assert "[CARD]" in str(out)


def test_invalid_card_masked_as_unverified():
    """A 16-digit string failing Luhn should not leak even though it's
    still PII-shaped."""
    eng = MaskingEngine()
    out, was = eng.mask_value("memo", "card 4111-1111-1111-1112")
    assert was
    text = str(out)
    assert "4111-1111-1111-1112" not in text
    assert "[UNVERIFIED_CARD]" in text


def test_recognizer_scan_tries_rrn_then_foreigner():
    """Both validators reject the value → no detection (Team B's
    "dead branch" fix — the previous elif structure never reached
    FOREIGNER_ID because RRN matched the same regex first)."""
    from app.safety.pii import KoreanPIIRecognizer

    rec = KoreanPIIRecognizer()
    # 13 digits that fail both checksums → 0 detections (but the
    # masking engine still tags it as UNVERIFIED above).
    assert rec.scan("9999999999999") == []
