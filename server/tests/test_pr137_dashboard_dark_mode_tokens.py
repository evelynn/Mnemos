"""PR-137 — dashboard.html must theme via CSS variables, not hex.

UX-audit finding (UX agent): ``dashboard.html:100-148`` carried ~13
hardcoded hex values (``#fff``, ``#fff7f7``, ``#7a1818``, ``#1f6feb``
…) inside its inline ``<style>`` block. Toggling the dark theme via
``data-theme="dark"`` left those rules untouched, so the triage card,
stat cards, and clickable stat-link outline all ended up "white on
white" or "red on red" — unreadable.

Fix: each colour was replaced with the matching design token
(``var(--border)``, ``var(--surface)``, ``var(--accent)``,
``var(--danger)`` etc.). The token sheet at ``app.css:84+`` inverts
those tokens under ``[data-theme=dark]``, so dark mode now renders
correctly without per-page overrides.

This test forward-guards the fix and locks the contract: the inline
``<style>`` of dashboard.html may not introduce raw hex colours
again.
"""

from __future__ import annotations

import re
from pathlib import Path

_TEMPLATES = (
    Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
)

# Match raw colour hex (#abc / #abcdef). Restricted to 3 or 6 hex
# digits to avoid hitting URL fragments / IDs.
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b")
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//.*?$", re.MULTILINE)


def _strip_comments_and_strings(css: str) -> str:
    """Remove CSS/JS comments + double-quoted strings before scanning
    for raw hex. A docstring like ``"the colour was #fff before"``
    must not flag."""
    css = _COMMENT_BLOCK_RE.sub("", css)
    css = _LINE_COMMENT_RE.sub("", css)
    # Drop double-quoted JS strings (avoids false positives inside
    # template literals / data-i18n placeholders).
    css = re.sub(r'"(?:[^"\\]|\\.)*"', "", css)
    return css


def _extract_inline_style_blocks(html: str) -> str:
    """Concatenate every ``<style>`` block's body in a template — only
    those are part of the rendered dashboard CSS. We deliberately do
    not scan inline ``style="…"`` attributes; those are SVG fills /
    one-offs already audited elsewhere (graph, report)."""
    return "\n".join(re.findall(
        r"<style[^>]*>(.*?)</style>", html, re.DOTALL | re.IGNORECASE,
    ))


def test_dashboard_html_inline_css_has_no_raw_hex():
    """The dashboard's inline ``<style>`` block must theme via CSS
    variables only — no raw hex. The token sheet at app.css inverts
    every variable under ``[data-theme=dark]``."""
    html = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    inline_css = _extract_inline_style_blocks(html)
    cleaned = _strip_comments_and_strings(inline_css)
    hits = _HEX_RE.findall(cleaned)
    assert not hits, (
        "dashboard.html inline <style> reintroduced raw hex "
        f"colour(s): {hits}. Use var(--token) instead so dark mode "
        "inverts correctly."
    )


def test_dashboard_html_uses_expected_tokens():
    """Make sure the fix actually wired the tokens — not just
    deleted the hex without replacement."""
    html = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    inline_css = _extract_inline_style_blocks(html)
    for token in (
        "var(--border)", "var(--surface)", "var(--text-muted)",
        "var(--success)", "var(--warn)", "var(--danger)",
        "var(--accent)",
    ):
        assert token in inline_css, (
            f"dashboard.html inline <style> should reference {token}"
        )


def test_app_css_defines_each_referenced_token():
    """Catch a typo'd ``var(--succes)`` before dark mode breaks
    silently. Every token the dashboard references must be defined
    in app.css ``:root``."""
    css = (
        _TEMPLATES.parent / "static" / "app.css"
    ).read_text(encoding="utf-8")
    for token in (
        "--border", "--surface", "--text-muted",
        "--success", "--warn", "--danger",
        "--accent", "--danger-bg", "--text-inverted",
        "--text", "--radius-md", "--radius-sm",
        "--transition-fast", "--shadow-hover",
    ):
        assert token + ":" in css, f"undefined CSS token: {token}"
