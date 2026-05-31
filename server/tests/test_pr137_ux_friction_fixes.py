"""PR-137 — UX-friction fixes from the cold audit.

Two changes:

1. ``MnemosUI.mountProjectPicker(host, opts)`` replaces every
   project-ID ``<input type=text placeholder=uuid>`` with a ``<select>``
   populated from ``/api/v1/projects``. The picker pre-fills from
   ``?project=`` and optionally auto-submits the parent form.

2. Raw HTTP body dumps (``textContent = HTTP {status}\n{body}``) are
   replaced with ``MnemosUI.showError`` (toast + structured detail)
   on failure paths in projects/analysis/gdpr/organizations/settings.

3. Empty-state CTA on projects.html so a fresh-org operator doesn't
   land in a dead end.
"""

from __future__ import annotations

import re
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1] / "app" / "dashboard"
_TEMPLATES = _BASE / "templates"
_UIJS = _BASE / "static" / "ui.js"


# ─── 1. Picker exposed + wired everywhere it's needed ─────────────


def test_ui_js_exports_mount_project_picker():
    src = _UIJS.read_text(encoding="utf-8")
    assert "mountProjectPicker: mountProjectPicker" in src
    assert "currentProjectFromUrl: currentProjectFromUrl" in src


def test_picker_fetches_projects_once_and_caches():
    """One round trip per page even when several pages embed a
    picker (e.g. dashboards with multiple project-scoped widgets)."""
    src = _UIJS.read_text(encoding="utf-8")
    assert "_projectsPromise" in src
    assert 'fetch("/api/v1/projects"' in src


def test_picker_replaces_input_in_place_preserving_attrs():
    """``replaceChild`` preserves the input's id / name so existing
    ``document.getElementById('pid')`` references in template
    handlers keep working."""
    src = _UIJS.read_text(encoding="utf-8")
    assert "replaceChild(sel, host)" in src
    assert 'getAttribute("name")' in src


def test_picker_supports_unknown_project_fallback():
    """If ``?project=<id>`` points at a project the operator can't
    list (deleted, cross-org), the picker keeps the value selected
    instead of silently dropping it."""
    src = _UIJS.read_text(encoding="utf-8")
    assert "Unknown project" in src


_PICKER_HOSTS = {
    "findings.html": "#pid",
    "plans.html":    "#pid",
    "data.html":     "#pid",
    "analysis.html": "#rpid",
    "graph.html":    "#gpid",
    "report.html":   "#rpid",
}


def test_every_project_scoped_page_mounts_the_picker():
    for name, sel in _PICKER_HOSTS.items():
        src = (_TEMPLATES / name).read_text(encoding="utf-8")
        assert f'MnemosUI.mountProjectPicker("{sel}"' in src, (
            f"{name} does not mount the project picker on {sel}"
        )


# ─── 2. Raw HTTP body dumps removed ───────────────────────────────


_DUMP_PATTERN = re.compile(
    r'textContent\s*=\s*`HTTP \$\{[^}]+\}\\n\$\{await [^`]+\}`'
)


def test_no_more_raw_http_body_dumps():
    """Forward-guard: no template may go back to ``textContent =
    `HTTP ${r.status}\\n${await r.text()}` `` — it's the
    audit-flagged "looks like a server crash" UX pattern."""
    for tpl in _TEMPLATES.glob("*.html"):
        src = tpl.read_text(encoding="utf-8")
        for m in _DUMP_PATTERN.finditer(src):
            raise AssertionError(
                f"{tpl.name}:{src[:m.start()].count(chr(10)) + 1} reintroduced "
                "the raw HTTP body dump. Use MnemosUI.showError() instead."
            )


def test_failure_paths_route_through_showError():
    """The five templates the audit cited route every failure
    through ``MnemosUI.showError`` (toast + structured detail)."""
    for name in (
        "projects.html", "analysis.html", "gdpr.html",
        "organizations.html", "settings.html",
    ):
        src = (_TEMPLATES / name).read_text(encoding="utf-8")
        assert "MnemosUI.showError" in src, (
            f"{name} has a failure path not routed through showError"
        )


# ─── 3. Empty-state CTA on projects.html ──────────────────────────


def test_projects_empty_state_has_cta():
    """A fresh org operator must not land in a dead end. The empty
    state surfaces a "Register your first project →" link."""
    src = (_TEMPLATES / "projects.html").read_text(encoding="utf-8")
    assert "Register your first project" in src
    assert "scrollIntoView" in src or 'href="#register-project"' in src


# ─── 4. Picker auto-submit gated by ``?project=`` ────────────────


def test_picker_autosubmit_uses_requestSubmit_or_submit_event():
    """Auto-submitting via ``form.submit()`` skips the onsubmit
    handler — must use ``requestSubmit()`` or dispatch a ``submit``
    Event so the handler still runs."""
    src = _UIJS.read_text(encoding="utf-8")
    assert "requestSubmit" in src
    assert 'new Event("submit"' in src
