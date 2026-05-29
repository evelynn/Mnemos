"""4th-round audit quick fixes (PR-15)."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# data.html — entities table exposes component_id (4th-round Critical A2)
# ---------------------------------------------------------------------------


def test_entities_table_renders_component_id():
    body = _read(_TPL / "data.html")
    # The header column for component id must be present so the
    # operator can read the value the query runner needs.
    assert "Component" in body
    assert "r.component_id" in body


def test_entities_table_has_use_in_query_button():
    body = _read(_TPL / "data.html")
    assert "useComponent" in body
    assert "Use in query" in body


# ---------------------------------------------------------------------------
# settings.html — secret-empty hint + regex validation (Major A1, C2)
# ---------------------------------------------------------------------------


def test_secret_empty_hint_present():
    body = _read(_TPL / "settings.html")
    assert "No db_connection secret yet" in body
    assert 'id="pdb-secret-hint"' in body


def test_masking_regex_client_check_warns():
    body = _read(_TPL / "settings.html")
    assert "_checkRegexLines" in body
    # Pre-submit warning, not a block — server stays authoritative.
    assert "server would silently skip" in body


# ---------------------------------------------------------------------------
# api/data.py — structured sql_parse_failed detail (Major C3)
# ---------------------------------------------------------------------------


def test_sql_parse_failed_returns_structured_detail():
    body = _read(_APP / "api" / "data.py")
    assert '"code": "sql_parse_failed"' in body
    assert '"message"' in body
    assert '"parser_error"' in body


def test_data_html_renders_friendly_parse_error():
    body = _read(_TPL / "data.html")
    assert 'detail.code === "sql_parse_failed"' in body
    assert "SQL parse failed" in body


# ---------------------------------------------------------------------------
# masking.py — Korean column-name keywords (Major A5)
# ---------------------------------------------------------------------------


def test_partial_mask_columns_includes_korean_keywords():
    """A column literally named ``주민번호`` (or ``이름``, ``주소`` etc.)
    must be picked up by the partial-mask regex.
    """
    from app.data_sampler.masking import MaskingEngine

    eng = MaskingEngine()
    out, was = eng.mask_value("주민번호", "900101-1234567")
    assert was, "Korean RRN column name must trigger partial mask"
    out2, was2 = eng.mask_value("이름", "홍길동")
    assert was2, "Korean name column must trigger partial mask"
    out3, was3 = eng.mask_value("주소", "서울시 강남구 …")
    assert was3, "Korean address column must trigger partial mask"


def test_english_columns_still_match():
    """The regression check — Korean additions must not break English."""
    from app.data_sampler.masking import MaskingEngine

    eng = MaskingEngine()
    _, was = eng.mask_value("email", "alice@example.com")
    assert was
    _, was = eng.mask_value("PhoneNumber", "010-1234-5678")
    assert was
