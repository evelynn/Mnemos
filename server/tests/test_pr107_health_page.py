"""PR-107 — GUI Health page.

PR-99 deepened ``/api/v1/health/ready`` (added crypto + analyzer
checks). PR-102 added a CLI ``verify`` for boot-time validation.
But spec §2.9 says "all features in the GUI" — and an operator
who wants to know if Redis is healthy still has to ``curl`` the
JSON endpoint or open a terminal.

This PR adds a dedicated ``/health`` tab that polls the existing
``/health/ready`` + ``/health/metrics_summary`` endpoints every
15 seconds, surfacing each of the five subsystem checks as its
own status block, plus the four 24-hour operational counts.

Five blocks: database, redis, worker, crypto, analyzers. The
first three flip overall to "degraded" (503) on failure. Crypto
failing is treated as hard-fail (every secret decrypt will
throw). Analyzers missing is advisory — ok=true, but the message
text starts with ``missing:`` so the page renders that block as
a warn rather than ok.
"""

from __future__ import annotations

from pathlib import Path

_BASE = Path(__file__).resolve().parents[1] / "app" / "dashboard"


def _read(rel: str) -> str:
    return (_BASE / rel).read_text(encoding="utf-8")


def test_health_template_exists():
    assert (_BASE / "templates" / "health.html").exists()


def test_health_route_registered():
    """The tab_page router whitelist must include ``health`` so a
    direct hit on /health doesn't 404."""
    body = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "dashboard"
        / "router.py"
    ).read_text(encoding="utf-8")
    idx = body.find("valid = {")
    slab = body[idx:idx + 800]
    assert '"health"' in slab


def test_template_polls_real_endpoints():
    html = _read("templates/health.html")
    # The page hits the two server-side health endpoints, not a
    # newly-invented one — keeps the API surface stable.
    assert "/api/v1/health/ready" in html
    assert "/api/v1/health/metrics_summary" in html


def test_template_renders_five_subsystem_blocks():
    html = _read("templates/health.html")
    # Each of the five deep-checks gets its own block — operators
    # need to see WHICH dependency is broken, not just "degraded".
    for subsystem in ("hb-database", "hb-redis", "hb-worker", "hb-crypto", "hb-analyzers"):
        assert subsystem in html, f"missing block: {subsystem}"


def test_template_renders_four_metric_counts():
    html = _read("templates/health.html")
    for slot in ("m-dbs", "m-bg", "m-runs", "m-wh"):
        assert slot in html, f"missing metric slot: {slot}"


def test_analyzers_block_renders_as_warn_when_missing():
    """The analyzers check returns ok=true with message="missing: a,b"
    when binaries aren't installed. The page must downgrade that
    block to "warn" (not "ok") so an operator notices."""
    html = _read("templates/health.html")
    idx = html.find("_setStatus(\"hb-analyzers\"")
    slab = html[idx:idx + 300]
    # Either the analyzers block has explicit "advisory" handling…
    assert "aMsg" in html or "missing" in slab
    # …and the rendering function distinguishes ok+advisory from
    # ok+clean (giving warn styling to the former).
    assert 'status.className = "status warn"' in html


def test_template_refreshes_periodically():
    html = _read("templates/health.html")
    # 15 s is the trade-off: fast enough that a watching operator
    # sees a Redis crash within the live-incident window, slow
    # enough to not hammer the server.
    assert "setInterval(reload, 15000)" in html


def test_overall_status_uses_response_status_not_payload_status():
    """Status code drives the overall verdict — even if a buggy
    handler returns {"status":"ok"} with a 503, we trust the 503."""
    html = _read("templates/health.html")
    idx = html.find("hb-overall-value")
    slab = html[idx:html.find("metrics_summary")]
    # The overall block branches on ``r.ok`` (the response 2xx-ness),
    # not on body.status.
    assert "if (r.ok)" in slab
    assert "READY" in slab
    assert "DEGRADED" in slab


def test_sidebar_link_added():
    """A page no operator can find is a page that doesn't exist —
    the nav must surface /health."""
    layout = _read("templates/_layout.html")
    assert 'href="/health"' in layout
    assert "active_tab == 'health'" in layout


def test_palette_and_shortcut_wired():
    js = _read("static/ui.js")
    # Command-palette entry so a power user can reach it via cmd-k.
    assert "{ label: \"Health\",      href: \"/health\"" in js
    # Vim-style "g h" shortcut so it's consistent with the others.
    assert '"h": "/health"' in js


def test_korean_phrases_registered():
    js = _read("static/ui.js")
    assert '"Platform health": "플랫폼 상태"' in js
    assert '"Health": "상태"' in js


def test_template_renders_via_jinja():
    """Smoke render — catches stray ``{% endblock %}`` typos that
    the grep tests above wouldn't notice."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(_BASE / "templates")),
        autoescape=True,
    )
    out = env.get_template("health.html").render(
        user={"id": 1, "username": "op", "role": "operator"},
        active_tab="health",
        request=None,
    )
    assert "Platform health" in out
    assert 'id="hb-database"' in out
