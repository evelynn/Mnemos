"""PR-138e — dashboard widget surfacing PR-138c's fallback breakdown.

Audit's #4 unresolved gap: counters exist but nothing displays them.
This PR wires a per-project "LLM pipeline health" card under the
dashboard's main grid; it calls the
``/api/v1/projects/{id}/llm_fallback_breakdown`` endpoint (PR-138c)
once per project and renders ok-vs-fallback bars + the failure-reason
breakdown so the operator sees, at a glance:

  - how many summaries used the real LLM vs the stub
  - which failure modes caused the fallbacks (agent_sdk_timeout,
    budget_exceeded, no_backend, …)

This file is mostly grep-tests against the inline template — the
backend half is exercised by test_pr138d_http_e2e_pack.py
(test_llm_fallback_breakdown_groups_by_reason).
"""

from __future__ import annotations

import re
from pathlib import Path

_TEMPLATES = (
    Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
)


def test_dashboard_html_renders_llm_health_section():
    src = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="llm-health-section"' in src
    assert 'id="llm-health-box"' in src
    # Hidden by default — only revealed when projects + summaries exist.
    assert 'id="llm-health-section" hidden' in src


def test_dashboard_html_calls_breakdown_endpoint():
    src = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    # Per-project breakdown fetch uses the PR-138c endpoint.
    assert "/llm_fallback_breakdown" in src
    # Iteration cap so a 100-project org doesn't fan-out 100 fetches.
    assert "projects.slice(0, 8)" in src


def test_dashboard_html_invokes_loadLlmHealth_in_boot():
    src = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert "async function loadLlmHealth" in src
    assert re.search(r"loadLlmHealth\(\);", src), (
        "loadLlmHealth must be invoked at the bottom of the boot script"
    )


def test_dashboard_html_widget_uses_design_tokens_not_hex():
    """PR-137c locked the dashboard's inline <style> to design tokens.
    The new widget styles must follow the same rule."""
    src = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", src, re.DOTALL)
    assert style is not None
    css = style.group(1)
    # Find the section the new widget styles live in.
    idx = css.find(".llm-health-row")
    assert idx >= 0, "llm-health-row styles missing"
    widget_css = css[idx:idx + 1500]
    # Each colour comes from a token.
    for token in ("var(--border)", "var(--surface)", "var(--success)",
                  "var(--warn)", "var(--surface-alt)"):
        assert token in widget_css, f"widget CSS missing token: {token}"
    # No raw hex.
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", widget_css), (
        f"widget CSS reintroduced raw hex: {widget_css}"
    )


def test_dashboard_html_widget_has_a11y_label():
    src = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    # The progress bar exposes the breakdown to screen readers via
    # an aria-label like "85% real LLM, 15% fallback".
    assert 'role="img"' in src
    assert "real LLM" in src and "fallback" in src
