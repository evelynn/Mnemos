"""PR-30 — close out the form-label i18n gap the 10th-round audit flagged.

The phrase book had Korean translations for ``Severity``, ``Status``,
``Project ID`` etc., but the templates' ``<label>X<input/></label>``
patterns didn't wear ``data-i18n`` markers, so the translations
never fired. The fix wraps the label text in a ``<span
data-i18n="…">`` so the runtime translator can swap the text node
without touching the nested input.
"""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Every label that had a phrase-book entry now wears the data-i18n marker.
# ---------------------------------------------------------------------------

# (template file, label string) pairs.
_TAGS = [
    ("findings.html",     "Project ID"),
    ("findings.html",     "Severity"),
    ("findings.html",     "Status"),
    ("analysis.html",     "Project ID"),
    ("analysis.html",     "Source path"),
    ("analysis.html",     "Git SHA"),
    ("analysis.html",     "Scope"),
    ("analysis.html",     "Run ID"),
    ("data.html",         "Project ID"),
    ("plans.html",        "Project ID"),
    ("diffs.html",        "Plan ID"),
    ("audit.html",        "Actor filter"),
    ("audit.html",        "Action filter"),
    ("audit.html",        "Limit"),
    ("projects.html",     "Name"),
    ("projects.html",     "GitLab project ID"),
    ("projects.html",     "GitLab URL"),
    ("projects.html",     "Default branch"),
    ("projects.html",     "Languages"),
    ("settings.html",     "Label"),
    ("settings.html",     "Kind"),
    ("settings.html",     "Value"),
]


def test_every_label_has_span_with_data_i18n():
    for fname, label in _TAGS:
        body = _read(_TPL / fname)
        marker = f'<span data-i18n="{label}">{label}</span>'
        assert marker in body, (
            f"{fname}: form label {label!r} must be wrapped in "
            f"<span data-i18n>; otherwise the phrase-book entry is dead"
        )


def test_phrase_book_covers_every_tagged_label():
    """No dead entries the other way either — every label we just
    tagged must have a Korean translation."""
    body = _read(_STATIC / "ui.js")
    seen = set()
    for _, label in _TAGS:
        if label in seen:
            continue
        seen.add(label)
        assert f'"{label}"' in body, (
            f"phrase book missing translation for {label!r}"
        )


def test_input_still_nested_in_label():
    """Regression guard — the ``<label><span>X</span><input/></label>``
    rewrite must not have lifted the input out of the label, otherwise
    click-the-label-to-focus-the-input stops working."""
    for fname, label in _TAGS:
        body = _read(_TPL / fname)
        # The span and an <input>/<select> share the same <label> tag.
        marker = f'<span data-i18n="{label}">{label}</span>'
        idx = body.find(marker)
        assert idx > 0
        # Find the enclosing <label> (preceding ``<label`` and the
        # next ``</label>``); ``<input``/``<select`` must appear
        # between them.
        label_open = body.rfind("<label", 0, idx)
        label_close = body.find("</label>", idx)
        assert label_open >= 0 and label_close > idx
        between = body[label_open:label_close]
        assert "<input" in between or "<select" in between, (
            f"{fname} / {label!r}: label rewrite must keep an <input> "
            f"or <select> inside the same <label>"
        )


# ---------------------------------------------------------------------------
# Korean translations of the new keys exist
# ---------------------------------------------------------------------------


def test_korean_translations_for_new_labels():
    body = _read(_STATIC / "ui.js")
    expected_kr = {
        "이름": "Name",
        "GitLab 프로젝트 ID": "GitLab project ID",
        "기본 브랜치": "Default branch",
        "행위자 필터": "Actor filter",
        "행동 필터": "Action filter",
        "최대 개수": "Limit",
        "실행 ID": "Run ID",
        "심각도": "Severity",
        "상태": "Status",
    }
    for kr, en in expected_kr.items():
        assert kr in body, (
            f"phrase book missing Korean translation {kr!r} (for {en!r})"
        )
