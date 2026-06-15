"""PR-149 — gap-driven Q&A deepening.

When the graph can't confidently answer a question, /ask ranks candidate
files by the question terms and agent-extracts them before retrying. These
deterministic guards cover the term tokeniser, the confidence test, and the
candidate-file ranker (the live deepen path is exercised end-to-end against
a running server, not CI).
"""

from __future__ import annotations

import os

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr149")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


def test_ask_route_registered():
    from app.main import app

    assert "/api/v1/projects/{project_id}/ask" in {getattr(r, "path", "") for r in app.routes}


def test_terms_and_confidence():
    from app.api.ask import _is_confident, _terms

    terms = _terms("Where is handle_create_order and what DB does it write?")
    assert "handle_create_order" in terms or "handle" in terms
    # confident only when a code-symbol hit clears the search-score
    # threshold (real search_symbols hits always carry a "score").
    hits = [{"symbol_id": "py:backend/orders.py::handle_create_order",
             "name": "handle_create_order", "score": 9.0}]
    assert _is_confident(hits, terms) is True
    # a weakly-scored hit is not a confident answer
    assert _is_confident([{"symbol_id": "py:x::zzz", "name": "zzz", "score": 0.5}], terms) is False
    assert _is_confident([], terms) is False
    # a DataEntity table lexically hitting "order" must NOT count as a
    # confident answer — even at a high score — → forces deepen
    assert _is_confident([{"symbol_id": "data:orders", "name": "orders", "score": 9.0}], terms) is False


def test_candidate_ranker_prefers_matching_file(tmp_path):
    from app.extractor.agent_extract import find_candidate_files

    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "orders_handler.py").write_text(
        "def handle_create_order(req, db):\n    db.execute('INSERT INTO orders ...')\n"
    )
    (tmp_path / "backend" / "unrelated.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "schema.sql").write_text("CREATE TABLE orders(id int);\n")

    ranked = find_candidate_files(str(tmp_path), ["handle_create_order", "order"], limit=3)
    names = [p.name for p, _ in ranked]
    assert names, "should find candidates"
    assert names[0] == "orders_handler.py", f"best match should rank first, got {names}"
    assert "unrelated.py" not in names
    # language is classified for extraction
    langs = {p.name: lang for p, lang in ranked}
    assert langs["orders_handler.py"] == "python"
