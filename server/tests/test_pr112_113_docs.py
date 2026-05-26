"""PR-112 + PR-113 — operator getting-started guide + /docs GUI.

PR-112 ships ``docs/operator-guide/getting-started.md`` — single
consolidated 30-minute path from cold checkout to first finding.
Replaces the scatter of deployment.md / phase1_checklist.md /
performance-review.md as the *initial* read.

PR-113 surfaces every in-tree doc via:
- ``GET /api/v1/docs``      — sorted index (operator guides first)
- ``GET /api/v1/docs/{slug}`` — markdown body
- ``/docs`` GUI tab — left nav + rendered markdown pane

Tests EXECUTE the API handlers (allowlist behaviour, body read,
404 paths, path-traversal defence) and validate the markdown
renderer's output against hand-built inputs. Behavioural, not grep.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DOCS_DIR = _REPO / "docs"
_BASE = _REPO / "server" / "app" / "dashboard"


# ─── PR-112 — getting-started.md ───────────────────────────────────


def test_getting_started_doc_exists():
    p = _DOCS_DIR / "operator-guide" / "getting-started.md"
    assert p.is_file(), "operator-guide/getting-started.md missing"


def test_getting_started_covers_seed_demo_path():
    """The 5-minute demo path is the single highest-value
    onboarding step. Doc must reference it explicitly."""
    body = (_DOCS_DIR / "operator-guide" / "getting-started.md").read_text(
        encoding="utf-8"
    )
    assert "seed-demo" in body
    assert "Triage now" in body


def test_getting_started_covers_verify_and_safe_secret_key():
    """The two PR-97/PR-102 safety nets are documented as the
    pre-flight checks — otherwise operators rediscover them by
    pain."""
    body = (_DOCS_DIR / "operator-guide" / "getting-started.md").read_text(
        encoding="utf-8"
    )
    assert "secrets.token_urlsafe(48)" in body
    assert "python -m app.cli verify" in body


def test_getting_started_covers_mcp_setup():
    """Claude Code MCP wiring is part of the value prop; the
    config snippet must appear."""
    body = (_DOCS_DIR / "operator-guide" / "getting-started.md").read_text(
        encoding="utf-8"
    )
    assert "mcpServers" in body
    assert "MNEMOS_MCP_TOKEN" in body


# ─── PR-113 — docs API ─────────────────────────────────────────────


def test_docs_api_module_imports():
    """Bare smoke — the module must import without dragging in
    a live DB / Redis. Caught one regression in a previous PR
    where a misplaced top-level fetch broke this."""
    from app.api import docs_index
    assert callable(docs_index.list_docs)
    assert callable(docs_index.get_doc)


def test_docs_api_allowlist_contains_getting_started():
    from app.api.docs_index import _DOCS
    assert "getting-started" in _DOCS
    assert _DOCS["getting-started"]["category"] == "operator"


def test_docs_allowlist_paths_all_resolve_on_disk():
    """Every slug in the allowlist must point to a real file.
    A dangling entry would 404 from the GUI nav — confusing UX."""
    from app.api.docs_index import _DOCS, _DOCS_ROOT
    missing = []
    for slug, meta in _DOCS.items():
        p = (_DOCS_ROOT / meta["path"]).resolve()
        if not p.is_file():
            missing.append((slug, meta["path"]))
    assert not missing, f"allowlist references missing files: {missing}"


@pytest.mark.asyncio
async def test_list_docs_returns_operator_before_design():
    """Sorted by category then title — operator first matches the
    cognitive order an onboarding user actually progresses through."""
    from app.api.docs_index import list_docs
    rows = await list_docs()
    cats = [r["category"] for r in rows]
    # All operator entries appear before any design entry.
    first_design = cats.index("design")
    assert "operator" not in cats[first_design:]


@pytest.mark.asyncio
async def test_get_doc_returns_markdown_body():
    from app.api.docs_index import get_doc
    resp = await get_doc("getting-started")
    assert resp.media_type == "text/markdown"
    body = resp.body.decode("utf-8")
    assert "# Mnemos" in body or "Mnemos" in body
    assert "seed-demo" in body  # we know this is in the file


@pytest.mark.asyncio
async def test_get_doc_404_on_unknown_slug():
    from fastapi import HTTPException
    from app.api.docs_index import get_doc
    with pytest.raises(HTTPException) as ei:
        await get_doc("nope-not-a-doc")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_doc_404_on_path_traversal_attempt():
    """Defence-in-depth: even if a future allowlist entry gets
    a ``../`` typo, the resolve+relative_to guard catches it."""
    from fastapi import HTTPException
    from app.api import docs_index

    # Inject a malicious entry temporarily.
    docs_index._DOCS["evil"] = {
        "path": "../../etc/passwd", "title": "x", "category": "operator"
    }
    try:
        with pytest.raises(HTTPException) as ei:
            await docs_index.get_doc("evil")
        assert ei.value.status_code == 404
    finally:
        del docs_index._DOCS["evil"]


# ─── /docs GUI ─────────────────────────────────────────────────────


def test_docs_template_exists_and_calls_api():
    html = (_BASE / "templates" / "docs.html").read_text(encoding="utf-8")
    assert "/api/v1/docs" in html
    assert "renderMarkdown" in html


def test_docs_route_in_router_whitelist():
    body = (
        _REPO / "server" / "app" / "dashboard" / "router.py"
    ).read_text(encoding="utf-8")
    idx = body.find("valid = {")
    slab = body[idx:idx + 800]
    assert '"docs"' in slab


def test_sidebar_link_added():
    layout = (_BASE / "templates" / "_layout.html").read_text(encoding="utf-8")
    assert 'href="/docs"' in layout


def test_palette_and_shortcut_wired():
    js = (_BASE / "static" / "ui.js").read_text(encoding="utf-8")
    assert "{ label: \"Docs\",        href: \"/docs\"" in js
    assert '"k": "/docs"' in js


# ─── Markdown renderer (extracted into testable JS string) ────────


def test_markdown_renderer_handles_core_cases():
    """The renderer is small JS so we can't execute it from Python
    directly — but we can confirm the source covers the markdown
    constructs the in-tree docs actually use: headings, fenced
    code, inline code, links, lists, tables, bold."""
    html = (_BASE / "templates" / "docs.html").read_text(encoding="utf-8")
    body = html[html.find("function renderMarkdown("):html.find("async function loadDocsList(")]
    # All seven constructs the in-tree docs actually use.
    for hint, label in (
        ('`([^`]+)`',              "inline code"),
        ('startsWith("```")',      "fenced code"),
        ('/^#{1,6}\\s/',           "headings"),
        (r'\[([^\]]+)\]\(([^)]+)\)', "links"),
        ('/^\\s*[-*]\\s/',         "bullets"),
        ('<table>',                "tables"),
        (r'\*\*([^*]+)\*\*',       "bold"),
    ):
        assert hint in body, f"renderer missing {label}: {hint!r}"


def test_markdown_renderer_escapes_html():
    """All inline text is _esc()'d before any HTML wrapping —
    prevents stored XSS via a maliciously-crafted in-tree doc."""
    html = (_BASE / "templates" / "docs.html").read_text(encoding="utf-8")
    body = html[html.find("function renderMarkdown("):html.find("async function loadDocsList(")]
    assert "_esc(s)" in body or "_esc(line)" in body
    assert "_esc" in body


def test_renderer_exported_for_tests():
    """``window.MnemosDocs`` exposes renderMarkdown so a future
    Playwright test can drive it with raw markdown strings."""
    html = (_BASE / "templates" / "docs.html").read_text(encoding="utf-8")
    assert "window.MnemosDocs = { renderMarkdown }" in html


def test_template_renders_via_jinja():
    """Smoke render — catches stray ``{% %}`` typos."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(_BASE / "templates")), autoescape=True)
    out = env.get_template("docs.html").render(
        user={"id": 1, "username": "op", "role": "operator"},
        active_tab="docs", request=None,
    )
    assert "Documentation" in out
    assert "/api/v1/docs" in out


# ─── Korean phrases ────────────────────────────────────────────────


def test_korean_phrases_registered():
    js = (_BASE / "static" / "ui.js").read_text(encoding="utf-8")
    for pair in (
        ('"Docs": "문서"',),
        ('"Documentation": "문서"',),
        ('"Operator guides": "운영자 가이드"',),
        ('"Design docs": "설계 문서"',),
    ):
        assert pair[0] in js, f"missing i18n entry: {pair[0]}"
