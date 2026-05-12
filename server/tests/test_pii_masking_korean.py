"""Korean PII baseline coverage for the masking engine (spec §2.8).

These pin the regex catalogue down so a future masking refactor does
not silently drop the country-specific patterns that Phase 1 promises:
RRN, Korean mobile prefixes (010/011/016/017/018/019), and credit card
numbers in both hyphen and space-separated forms.
"""

from __future__ import annotations

from app.data_sampler.masking import MaskingEngine


def _valid_rrn() -> str:
    """Compute a checksum-valid RRN so the test doesn't rely on a
    constant that happens to be invalid. PR-11 made the [RRN] label
    contingent on ``is_valid_rrn`` passing; the previous fixed value
    900101-1234567 fails the weighted-sum check and now gets the
    UNVERIFIED label instead.
    """
    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    prefix = "900101123456"
    check = (11 - sum(int(d) * w for d, w in zip(prefix, weights)) % 11) % 10
    return prefix[:6] + "-" + prefix[6:] + str(check)


def test_korean_rrn_masked():
    eng = MaskingEngine()
    rrn = _valid_rrn()
    out, _ = eng.mask_value("memo", f"주민번호: {rrn}")
    assert "[RRN]" in str(out)
    assert rrn.split("-")[1] not in str(out)


def test_korean_rrn_without_hyphen_masked():
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", _valid_rrn().replace("-", ""))
    assert "[RRN]" in str(out)


def test_invalid_rrn_shape_falls_back_to_unverified():
    """A 13-digit string that fails the checksum must still be
    redacted (Team B 3rd-round must-fix #3 — bogus PII-shaped values
    cannot leak in the clear)."""
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", "leaked: 1234567890123")
    text = str(out)
    assert "1234567890123" not in text
    assert "[UNVERIFIED_NUMERIC_ID]" in text


def test_korean_mobile_010_masked_as_phone_kr():
    """The 010 prefix should hit the country-specific tag, not the
    generic PHONE one."""
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", "Contact: 010-1234-5678")
    assert "[PHONE_KR]" in str(out)


def test_korean_mobile_016_masked():
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", "old: 016-987-6543")
    assert "[PHONE_KR]" in str(out)


def test_non_korean_phone_falls_back_to_generic():
    """A US-style 02-xxx-xxxx is not a Korean mobile, but the generic
    phone catcher still masks it."""
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", "ext: 02-555-1234")
    assert "[PHONE]" in str(out)


def test_credit_card_hyphen_separated():
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", "card: 4111-1111-1111-1111")
    assert "[CARD]" in str(out)
    assert "1111-1111" not in str(out)


def test_credit_card_space_separated():
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", "card: 4111 1111 1111 1111")
    assert "[CARD]" in str(out)


def test_email_masked():
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", "Send to alice@example.co.kr please")
    assert "[EMAIL]" in str(out)
    assert "alice" not in str(out)


def test_column_named_password_is_fully_masked():
    """Column-level full-mask trumps any value heuristics.

    Note: the regex uses ``\\b`` word boundaries, and ``_`` is a word
    char in Python regex (``\\w == [A-Za-z0-9_]``), so ``user_password``
    does not match because there's no boundary between ``user`` and
    ``password``. Plain ``password`` is the canonical column name that
    triggers full masking. Operators who name their column
    ``user_password`` should add a ``masking_rules.full`` regex on the
    project_db row.
    """
    eng = MaskingEngine()
    out, was_masked = eng.mask_value("password", "ignored")
    assert out == "***"
    assert was_masked is True
