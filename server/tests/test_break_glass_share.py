"""Break-glass share-context flow (PR-14).

Tokens still travel out-of-band; the share URL only carries the
context the operator needs to find the right submission. These tests
pin the response shape and the static parts of the dashboard handler.
"""

from __future__ import annotations

import uuid
from pathlib import Path


def test_share_context_excludes_token():
    """The serialised model must never include the raw token."""
    from app.api.break_glass import BreakGlassResponse, ShareContext

    sc = ShareContext(
        submission_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        grant_id_short="abc123",
    )
    response = BreakGlassResponse(
        grant_id=uuid.uuid4(),
        token="oob-only-do-not-leak",
        expires_at="2025-01-01T00:15:00+00:00",  # type: ignore[arg-type]
        rerun_verdict="warn",
        server_now="2025-01-01T00:00:00+00:00",  # type: ignore[arg-type]
        issued_at="2025-01-01T00:00:00+00:00",  # type: ignore[arg-type]
        share_context=sc,
    )

    share_json = response.share_context.model_dump_json()
    assert "oob-only-do-not-leak" not in share_json
    assert "token" not in share_json


def test_share_context_uses_short_grant_fingerprint():
    """The fingerprint is short enough to be operator-friendly while
    long enough that an attacker can't trivially guess it."""
    from app.api.break_glass import ShareContext

    sc = ShareContext(
        submission_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        grant_id_short="a" * 6,
    )
    assert len(sc.grant_id_short) == 6


def test_diffs_html_builds_share_url_from_payload():
    body = (
        Path(__file__).resolve().parents[1]
        / "app" / "dashboard" / "templates" / "diffs.html"
    ).read_text(encoding="utf-8")
    # The URL builder must read share_context and write a hash, not
    # a query string (so it never reaches the server access log).
    assert "share_context" in body
    assert "u.hash" in body
    assert "approve=" in body


def test_diffs_html_has_hash_router():
    body = (
        Path(__file__).resolve().parents[1]
        / "app" / "dashboard" / "templates" / "diffs.html"
    ).read_text(encoding="utf-8")
    assert "maybeAutoLoadFromHash" in body
    assert 'params.get("approve")' in body
    assert 'params.get("plan")' in body


def test_share_link_block_has_copy_button():
    body = (
        Path(__file__).resolve().parents[1]
        / "app" / "dashboard" / "templates" / "diffs.html"
    ).read_text(encoding="utf-8")
    assert "copyShareUrl" in body
    assert 'id="share-url"' in body


def test_share_link_does_not_carry_token_to_server():
    """Belt-and-braces: the share URL must use the hash, never a query."""
    body = (
        Path(__file__).resolve().parents[1]
        / "app" / "dashboard" / "templates" / "diffs.html"
    ).read_text(encoding="utf-8")
    # `u.search = ""` is the explicit strip; if a future refactor sets
    # query params instead this assertion catches it.
    assert 'u.search = ""' in body
