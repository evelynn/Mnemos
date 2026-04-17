"""Safety unit tests (spec §14, §18.3)."""

from app.merge.contract_id import http_contract_id, normalize_http_path
from app.safety.self_review import review_diff
from app.sandbox.allowlist import is_allowed


def test_path_normalization_collapses_numeric_segments():
    assert normalize_http_path("/api/orders/123") == "/api/orders/{id}"
    assert normalize_http_path("/api/orders/:id") == "/api/orders/{id}"
    assert http_contract_id("get", "/api/orders/123?x=1") == "http.GET./api/orders/{id}"


def test_allowlist_blocks_arbitrary_commands():
    assert is_allowed("dotnet test") is True
    assert is_allowed("pytest -q tests") is True
    assert is_allowed("git status") is True
    assert is_allowed("rm -rf /") is False
    assert is_allowed("curl http://evil") is False
    assert is_allowed("git push") is False


def test_self_review_blocks_hardcoded_secret():
    diff = """+++ b/x.py
+password = \"super_secret_9999\"
"""
    result = review_diff(diff)
    assert any(f.severity == "error" and f.rule == "hardcoded_secret" for f in result.findings)


def test_self_review_blocks_drop_table():
    diff = """+++ b/x.sql
+DROP TABLE orders;
"""
    result = review_diff(diff)
    assert any(f.rule == "forbidden_sql" for f in result.findings)


def test_self_review_warns_on_todo():
    diff = """+++ b/x.py
+# TODO: fix this later
"""
    result = review_diff(diff)
    assert any(f.rule == "todo_added" and f.severity == "warning" for f in result.findings)
