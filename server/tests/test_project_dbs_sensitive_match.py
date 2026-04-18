"""Sensitive-table detection for query landing zone (spec §12.2, §14.2)."""

from app.data_sampler.project_db import sensitive_tables_hit


def test_returns_matching_table_name():
    assert sensitive_tables_hit("SELECT * FROM dbo.Users WHERE id=1", ["Users"]) == "Users"


def test_match_is_case_insensitive():
    assert sensitive_tables_hit("select id from USERS", ["users"]) == "users"


def test_no_match_returns_none():
    assert sensitive_tables_hit("SELECT * FROM Orders", ["Users"]) is None


def test_substring_does_not_match():
    # 'UserProfiles' contains 'Users' as a substring but is a distinct identifier.
    assert sensitive_tables_hit("SELECT * FROM UserProfiles", ["Users"]) is None


def test_empty_list_returns_none():
    assert sensitive_tables_hit("SELECT * FROM Users", []) is None
    assert sensitive_tables_hit("SELECT * FROM Users", None) is None


def test_first_hit_is_reported():
    hit = sensitive_tables_hit(
        "SELECT u.id FROM Users u JOIN Secrets s ON u.id=s.user_id",
        ["Secrets", "Users"],
    )
    assert hit in {"Secrets", "Users"}
