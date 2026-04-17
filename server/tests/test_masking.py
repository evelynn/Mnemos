"""PII masking smoke tests (spec §8.3 / §14.5)."""

from app.data_sampler.masking import MaskingEngine, mask_rows


def test_full_mask_columns_are_replaced():
    eng = MaskingEngine()
    val, was = eng.mask_value("password", "hunter2")
    assert val == "***" and was


def test_partial_mask_columns_keep_first_three_chars():
    eng = MaskingEngine()
    val, was = eng.mask_value("email", "alice@example.com")
    assert val == "ali***" and was


def test_email_values_get_replaced():
    rows, _, any_masked = mask_rows(["note"], [["contact a@b.com if urgent"]])
    assert any_masked
    assert "[EMAIL]" in rows[0][0]


def test_rrn_and_phone_are_masked():
    rows, _, any_masked = mask_rows(["note"], [["call 010-1234-5678 or 900101-1234567"]])
    assert any_masked
    text = rows[0][0]
    assert "[PHONE]" in text
    assert "[RRN]" in text
