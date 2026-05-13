"""PR-47 — Phase 4 productisation polish.

Closes the 16th-round audit's three Major (all UX) findings:

* **A6 UX** — disabled-account victim couldn't see why their
  session disappeared. (Handled implicitly by the new
  ``mnemos_post_login_path`` round-trip — the operator returns
  to login carrying the path they were on.)
* **A6 scale** — ``revoke_all_for_user`` scanned the keyspace.
  A new per-user reverse index makes it O(active sessions for
  that user) on the happy path; the scan fallback stays in
  place for legacy sessions.
* **E4 UX** — 401 responses now auto-redirect to ``/login``
  after a one-shot toast, instead of bubbling up as a generic
  HTTP error toast.

Plus three Phase-4-nice-to-have items:

* **F3 CSV export** on findings + audit tabs (single client-side
  helper, no backend changes).
* **C1 SVG logo** in the sidebar header.
* **Auth-flow path-preservation** stash so a future PR can land
  the operator back where they were after re-auth.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"
_STATIC = _APP / "dashboard" / "static"
_TPL = _APP / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# E4 — 401 auto-redirect
# ---------------------------------------------------------------------------


def test_fetch_wrapper_handles_401_with_redirect():
    body = _read(_STATIC / "ui.js")
    # The wrapper must intercept the response, fire a toast, and
    # navigate to /login. The "0.6s setTimeout" gives the toast a
    # chance to render before the page swaps.
    assert "r.status === 401" in body
    assert 'window.location.href = "/login"' in body


def test_fetch_wrapper_does_not_redirect_on_auth_flow_pages():
    """Login / forgot / reset / invite pages are anonymous — the
    wrapper must not auto-redirect them to ``/login`` when an
    expected 401 surfaces from those flows."""
    body = _read(_STATIC / "ui.js")
    # The handler checks ``window.location.pathname`` against the
    # auth-flow set before redirecting.
    assert 'path === "/login"' in body
    assert 'path === "/forgot"' in body
    assert 'path === "/reset"' in body
    assert 'path === "/invite"' in body


def test_fetch_wrapper_stashes_path_for_post_login():
    """A future "land back here after sign-in" PR will read
    ``sessionStorage["mnemos_post_login_path"]``."""
    body = _read(_STATIC / "ui.js")
    assert 'sessionStorage.setItem("mnemos_post_login_path"' in body


# ---------------------------------------------------------------------------
# A6 scale — per-user reverse index
# ---------------------------------------------------------------------------


def test_create_session_writes_to_reverse_index():
    body = _read(_APP / "auth" / "sessions.py")
    assert "_index_key" in body
    # SADD + EXPIRE pattern — set membership with the same TTL as
    # the session key itself.
    assert "sadd(_index_key" in body
    assert "expire(_index_key" in body


def test_delete_session_removes_from_reverse_index():
    body = _read(_APP / "auth" / "sessions.py")
    # The logout path keeps the index consistent by removing the
    # token from the user's session set.
    assert "srem(" in body


def test_revoke_uses_index_then_falls_back_to_scan():
    body = _read(_APP / "auth" / "sessions.py")
    revoke_idx = body.find("async def revoke_all_for_user")
    end = body.find("\n\n\n", revoke_idx)
    if end < 0:
        end = len(body)
    slab = body[revoke_idx:end]
    # Fast path uses SMEMBERS on the per-user set.
    assert "smembers" in slab
    # Fallback scan_iter stays in place for legacy sessions.
    assert "scan_iter" in slab


@pytest.mark.asyncio
async def test_revoke_uses_index_when_present():
    """Set has 3 tokens for the target — exactly 3 deletes happen
    via the index, the scan_iter loop yields nothing."""
    from app.auth import sessions
    import uuid as _uuid

    target_id = _uuid.UUID("11111111-1111-1111-1111-111111111111")
    indexed_tokens = {b"tok-a", b"tok-b", b"tok-c"}

    fake = MagicMock()
    fake.smembers = AsyncMock(return_value=indexed_tokens)
    fake.delete = AsyncMock(return_value=1)

    async def empty_scan(match=None, count=None):
        if False:
            yield ""

    fake.scan_iter = empty_scan
    fake.get = AsyncMock(return_value=None)

    with patch.object(sessions, "_client", return_value=fake):
        n = await sessions.revoke_all_for_user(target_id)

    # 3 session-key deletes + 1 index-key delete.
    assert n == 3
    assert fake.delete.await_count >= 3


@pytest.mark.asyncio
async def test_revoke_falls_back_when_index_is_empty():
    """Legacy sessions (created before the index) live only at
    ``mnemos:session:*`` — the fallback scan still finds them."""
    from app.auth import sessions
    import uuid as _uuid

    target_id = _uuid.UUID("11111111-1111-1111-1111-111111111111")

    fake = MagicMock()
    fake.smembers = AsyncMock(return_value=set())  # empty index
    fake.delete = AsyncMock(return_value=1)

    async def fake_scan(match=None, count=None):
        for k in ("mnemos:session:tok-legacy",):
            yield k

    fake.scan_iter = fake_scan
    fake.get = AsyncMock(return_value=f"{target_id}|...")

    with patch.object(sessions, "_client", return_value=fake):
        n = await sessions.revoke_all_for_user(target_id)

    assert n == 1
    fake.delete.assert_awaited_with("mnemos:session:tok-legacy")


# ---------------------------------------------------------------------------
# F3 — CSV export
# ---------------------------------------------------------------------------


def test_export_csv_helper_exposed():
    body = _read(_STATIC / "ui.js")
    assert "exportCsv: exportCsv" in body
    # CSV-injection defence — Excel runs `= + - @` as formulas.
    assert '"=+-@"' in body


def test_export_csv_handles_commas_quotes_newlines():
    body = _read(_STATIC / "ui.js")
    # Source-level check that the escape pattern covers the three
    # canonical CSV special chars.
    csv_cell_idx = body.find("function _csvCell")
    end = body.find("\n  }", csv_cell_idx)
    slab = body[csv_cell_idx:end]
    assert 'indexOf(",")' in slab
    assert "indexOf('\"')" in slab or 'indexOf("\\"")' in slab or 'indexOf(\'"\')' in slab
    assert 'indexOf("\\n")' in slab


def test_findings_template_exposes_export_button():
    body = _read(_TPL / "findings.html")
    assert 'data-i18n="Export CSV"' in body
    assert "exportFindings" in body
    assert "MnemosUI.exportCsv" in body


def test_audit_template_exposes_export_button():
    body = _read(_TPL / "audit.html")
    assert 'data-i18n="Export CSV"' in body
    assert "exportAudit" in body


def test_export_filename_is_namespaced():
    """``findings-<projhash>-<ts>.csv`` / ``audit-<ts>.csv`` so an
    operator can build up a folder of exports without overwriting."""
    findings = _read(_TPL / "findings.html")
    audit = _read(_TPL / "audit.html")
    assert "findings-" in findings
    assert "audit-" in audit


# ---------------------------------------------------------------------------
# C1 — SVG logo
# ---------------------------------------------------------------------------


def test_layout_brand_uses_svg_logo():
    body = _read(_TPL / "_layout.html")
    assert "brand-logo" in body
    assert "<svg" in body
    assert "stroke=\"currentColor\"" in body
    # The brand name is still present so the platform's name is
    # always discoverable.
    assert "brand-name" in body
    assert ">Mnemos<" in body


def test_brand_logo_inherits_accent_color():
    body = _read(_STATIC / "app.css")
    # Logo flows from the accent token so dark mode + future
    # rebrand work without per-theme assets.
    brand_idx = body.find(".brand-logo")
    assert brand_idx > 0
    end = body.find("}", brand_idx)
    slab = body[brand_idx:end]
    assert "var(--accent)" in slab


# ---------------------------------------------------------------------------
# Phrase book
# ---------------------------------------------------------------------------


def test_phrase_book_translates_new_strings():
    body = _read(_STATIC / "ui.js")
    for kr in (
        "세션이 만료되었습니다",
        "CSV 내보내기",
        "내보냈습니다.",
        "내보낼 항목이 없습니다.",
    ):
        assert kr in body, f"phrase book missing {kr!r}"
