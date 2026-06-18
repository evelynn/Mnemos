"""PR-187 — the Ask answer visualizes tabular results.

LLM answers frequently come back as GitHub-flavoured markdown: tables
(``| col | col |`` over a ``|---|`` separator) and ``##`` headings. The
old ``mdRender`` only handled code/bold/inline/bullets, so a table was
shown as raw pipe text inside the ``.llm-prose`` pre-wrap block — the
result was unreadable (the user saw literal ``| … |`` rows). ``mdRender``
now parses GFM tables into real ``<table>`` elements and headings into
``<hN>``, breaking out of the prose wrapper exactly like code blocks do.
"""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def test_ask_renders_markdown_tables_and_headings():
    html = (_TPL / "ask.html").read_text(encoding="utf-8")
    # a GFM table becomes a real <table class="md-table"> (not raw pipes)
    assert "md-table" in html
    assert "<thead>" in html and "<tbody>" in html
    # the separator row (|---|) is what distinguishes a table from prose
    assert "isSep" in html
    # ## headings render as real heading elements, not literal "## "
    assert "md-h" in html


def test_ask_table_styles_are_theme_tokenised():
    # table/heading CSS reads theme tokens so it works in both Nord Light and
    # One Dark Pro — no hard-coded colours.
    html = (_TPL / "ask.html").read_text(encoding="utf-8")
    idx = html.find(".md-table")
    slab = html[idx:idx + 700]
    assert "var(--border)" in slab
    assert "var(--surface-alt)" in slab


def test_ask_template_still_parses():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(_TPL)),
        autoescape=select_autoescape(["html"]),
    )
    env.parse((_TPL / "ask.html").read_text(encoding="utf-8"))
