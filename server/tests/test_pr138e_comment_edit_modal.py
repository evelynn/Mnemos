"""PR-138e — comment edit uses a real modal, not window.prompt().

UX-audit roughness #9: ``_mnemosEditComment`` opened a bare
``window.prompt("Edit comment:")`` — unstyled, no validation, no
dark-mode coverage, no pre-fill, and famously bad for screen readers.

This PR replaces it with a Promise-based ``<dialog>`` modal that:

- Pre-fills the textarea with the current comment body (fetched
  via ``GET /api/v1/comments/{id}``).
- Honours the design tokens via ``class="modal"`` (PR-137c rules).
- Wires Cmd/Ctrl+Enter for Save (GitHub-equivalent).
- Esc → Cancel via dialog's native ``cancel`` event.
- Returns ``null`` on Cancel so the caller short-circuits — same
  contract as ``window.prompt``.
"""

from __future__ import annotations

import re
from pathlib import Path

_UIJS = (
    Path(__file__).resolve().parents[1]
    / "app" / "dashboard" / "static" / "ui.js"
)


def test_ui_js_no_longer_calls_window_prompt_for_edit_comment():
    src = _UIJS.read_text(encoding="utf-8")
    # Strip comment blocks before searching so a "we used to do
    # ``prompt("Edit comment:")``" rationale comment doesn't trip
    # the guard.
    no_comments = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    no_comments = re.sub(r"//.*?$", "", no_comments, flags=re.MULTILINE)
    assert 'prompt("Edit comment' not in no_comments, (
        "comment edit must not use window.prompt"
    )


def test_ui_js_defines_edit_comment_dialog_helper():
    src = _UIJS.read_text(encoding="utf-8")
    assert "function _editCommentDialog" in src
    # Must use a real <dialog>, not a synthetic div.
    assert 'createElement("dialog")' in src
    assert "showModal" in src


def test_edit_comment_dialog_returns_a_promise():
    """``await _editCommentDialog(...)`` must produce ``null`` on
    Cancel and the trimmed string on Save — same contract as
    ``window.prompt`` so the caller keeps working."""
    src = _UIJS.read_text(encoding="utf-8")
    assert "return new Promise" in src
    # Cancel / Esc paths resolve null; Save path resolves the body.
    assert "resolve(null)" in src or "_close(null)" in src
    assert "_close(value)" in src


def test_edit_comment_caller_prefills_from_get():
    """Operator's complaint: prompt() was empty so I had to retype
    the whole comment. The modal pre-fills via a GET round-trip."""
    src = _UIJS.read_text(encoding="utf-8")
    edit_block = src[src.find("_mnemosEditComment ="):]
    assert 'fetch("/api/v1/comments/" + commentId)' in edit_block[:1500]


def test_edit_comment_dialog_wires_ctrl_enter_save():
    src = _UIJS.read_text(encoding="utf-8")
    # GitHub-style: Cmd/Ctrl+Enter posts the textarea.
    assert "(ev.ctrlKey || ev.metaKey)" in src
    assert 'key === "Enter"' in src
