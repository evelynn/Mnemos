"""Per-project-DB masking overrides (spec §12.2 masking_rules)."""

from app.data_sampler.masking import MaskingEngine


def test_from_project_db_adds_full_mask_column():
    eng = MaskingEngine.from_project_db({"full": ["acct_no"]})
    val, was = eng.mask_value("acct_no", "1234567890")
    assert val == "***"
    assert was


def test_from_project_db_adds_partial_mask_column():
    eng = MaskingEngine.from_project_db({"partial": ["cust_name"]})
    val, was = eng.mask_value("cust_name", "Evelyn Q")
    assert was
    assert val == "Eve***"


def test_defaults_still_apply_with_overrides():
    eng = MaskingEngine.from_project_db({"full": ["acct_no"]})
    val, was = eng.mask_value("password", "hunter2")
    assert val == "***" and was


def test_invalid_regex_is_skipped_not_raised():
    eng = MaskingEngine.from_project_db({"full": ["[unterminated"]})
    val, was = eng.mask_value("password", "hunter2")
    assert val == "***" and was


def test_empty_rules_are_backward_compatible():
    eng = MaskingEngine.from_project_db(None)
    val, was = eng.mask_value("other_col", "plain")
    assert val == "plain"
    assert not was
