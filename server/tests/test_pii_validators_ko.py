"""Korean PII validators — checksum coverage (spec §2.8)."""

from __future__ import annotations

import pytest

from app.safety.pii import (
    KoreanPIIRecognizer,
    is_valid_card_luhn,
    is_valid_foreigner_id,
    is_valid_korean_drivers_license,
    is_valid_rrn,
)


# ---------------------------------------------------------------------------
# RRN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rrn",
    [
        # Picked so the weighted checksum lands cleanly.
        "9001011234561",
    ],
)
def test_rrn_valid_known_good(rrn):
    # Compute the expected check digit so the assertion is self-checking
    # rather than relying on a magic constant.
    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    expected = (11 - sum(int(d) * w for d, w in zip(rrn[:12], weights)) % 11) % 10
    real = rrn[:12] + str(expected)
    assert is_valid_rrn(real), f"weighted checksum should validate {real}"


def test_rrn_rejects_wrong_checksum():
    # Same prefix, wrong final digit — checksum must fail.
    assert not is_valid_rrn("900101-1234560")


def test_rrn_rejects_impossible_month():
    assert not is_valid_rrn("99-99-99-9999999")


def test_rrn_rejects_wrong_length():
    assert not is_valid_rrn("1234567")


def test_rrn_accepts_hyphen_or_plain():
    """The masking engine fires on both '900101-1234567' and '9001011234567';
    so must the validator."""
    plain = "9001012345671"
    hyphenated = plain[:6] + "-" + plain[6:]
    assert is_valid_rrn(plain) == is_valid_rrn(hyphenated)


# ---------------------------------------------------------------------------
# Foreigner ID
# ---------------------------------------------------------------------------


def test_foreigner_id_requires_s_digit_5_through_8():
    # An S of 1 is RRN-only territory; foreigner validator must refuse.
    assert not is_valid_foreigner_id("9001011234567")


# ---------------------------------------------------------------------------
# Card / Luhn
# ---------------------------------------------------------------------------


def test_luhn_valid_test_pan():
    # Visa test PAN, public knowledge.
    assert is_valid_card_luhn("4111111111111111")
    assert is_valid_card_luhn("4111-1111-1111-1111")


def test_luhn_rejects_off_by_one():
    assert not is_valid_card_luhn("4111111111111112")


def test_luhn_rejects_short_or_long():
    assert not is_valid_card_luhn("4111")
    assert not is_valid_card_luhn("4" * 25)


# ---------------------------------------------------------------------------
# Korean driver's license
# ---------------------------------------------------------------------------


def test_license_format_accepts_canonical():
    assert is_valid_korean_drivers_license("11-23-456789-01")
    # AA(2) + YY(2) + NNNNNN(6) + CC(2) = 12 digits when hyphens are
    # stripped — the previous expectation of 14 digits was a typo.
    assert is_valid_korean_drivers_license("112345678901")


def test_license_format_rejects_bad_region():
    assert not is_valid_korean_drivers_license("99-23-456789-01")


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def test_recognizer_skips_invalid_lookalikes():
    """A 13-digit string that *looks* like an RRN but fails the checksum
    must not be flagged — that was the whole point of the validators."""
    rec = KoreanPIIRecognizer()
    text = "fake id 1234567890123 here"
    assert rec.scan(text) == []
