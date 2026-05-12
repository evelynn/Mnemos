"""Korean PII baseline coverage for the masking engine (spec §2.8).

These pin the regex catalogue down so a future masking refactor does
not silently drop the country-specific patterns that Phase 1 promises:
RRN, Korean mobile prefixes (010/011/016/017/018/019), and credit card
numbers in both hyphen and space-separated forms.
"""

from __future__ import annotations

from app.data_sampler.masking import MaskingEngine


def test_korean_rrn_masked():
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", "주민번호: 900101-1234567")
    assert "[RRN]" in str(out)
    assert "1234567" not in str(out)


def test_korean_rrn_without_hyphen_masked():
    eng = MaskingEngine()
    out, _ = eng.mask_value("memo", "9001011234567")
    assert "[RRN]" in str(out)


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
    """Column-level full-mask trumps any value heuristics."""
    eng = MaskingEngine()
    out, was_masked = eng.mask_value("user_password", "ignored")
    assert out == "***"
    assert was_masked is True
