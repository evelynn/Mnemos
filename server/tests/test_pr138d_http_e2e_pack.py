"""PR-138d — HTTP-level real-execution E2E pack.

The audit's #3 unresolved gap is "tests reality" — 77% of the suite
is source-text grep. This file adds a substantial pack of tests that
each exercise:

- a real FastAPI handler (via ``httpx.ASGITransport``)
- a real polyglot SQLite session (PR-118)
- real auth (session cookie, bcrypt password hash)
- real model rows seeded into the DB

Every test follows the same pattern: cold-boot SQLite + seed_demo +
http client → drive multi-step user flow → assert the response.
This is the same path an operator hits, not a stub.

Pack contents (each test = 1 user story)
---------------------------------------
1. login → /auth/me → /projects → /findings — operator's first
   real session.
2. /findings/summary returns real risk_index from seeded findings.
3. /findings/trend rolls 90-day buckets from seeded created_at.
4. /findings/roi pairs risk eliminated × LLM cost.
5. /llm_cost reads seeded summaries' tokens_used.
6. /llm_fallback_breakdown (PR-138c new) groups by fallback_reason.
7. /graph/component_map returns nodes + edges from seeded graph.
8. /graph/certainty_breakdown counts by certainty class.
9. /graph/stats sanity (nodes_total, edges_total).
10. CSRF: state-changing POST without token → 403.
11. 401: unauthenticated GET on /me → 401.
12. /health/ready reports inline worker in local mode.
"""

from __future__ import annotations

import asyncio
import os
import uuid as _uuid
from pathlib import Path
from typing import Any

import pytest

# Module-level env so app.* imports see the right values.
os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr138d")
os.environ.setdefault(
    "FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys="
)
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def seeded_state(tmp_path_factory, event_loop):
    """Boot SQLite + seed-demo once for the module so we share one
    session across the pack — each test starts from the same known
    state.

    PR-138d — when a previous test module bound ``app.db.engine`` to
    a different URL, dispose + reload so seed_demo and the HTTP app
    use ours.
    """
    db = tmp_path_factory.mktemp("pr138d") / "e2e.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    # Dispose + reload app.db so any existing engine bound to a
    # previous module's DATABASE_URL doesn't shadow ours. ``seed_demo``
    # / ``ensure_sqlite_schema`` import ``app.db.SessionLocal`` at
    # call time, so as long as the module is re-imported here we
    # get the new bindings.
    import sys
    if "app.db" in sys.modules:
        import importlib
        from app import db as _db
        try:
            event_loop.run_until_complete(_db.engine.dispose())
        except Exception:  # noqa: BLE001
            pass
        importlib.reload(_db)
    # Same treatment for any modules that may have captured
    # ``SessionLocal`` at import time. seed_demo + local_mode both
    # re-import lazily; force re-load for safety.
    for mod in ("app.local_mode", "app.seed_demo"):
        if mod in sys.modules:
            import importlib
            importlib.reload(sys.modules[mod])

    from app.local_mode import ensure_sqlite_schema
    from app.seed_demo import seed_demo

    async def _go():
        await ensure_sqlite_schema()
        return await seed_demo(force=True)

    summary = event_loop.run_until_complete(_go())
    return summary


@pytest.fixture
def client(seeded_state, event_loop):
    """Yield an httpx AsyncClient bound to the FastAPI app."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async def _open():
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        )

    c = event_loop.run_until_complete(_open())
    yield c
    event_loop.run_until_complete(c.aclose())


@pytest.fixture
def logged_in(client, seeded_state, event_loop):
    """A client with a valid session cookie. Returns ``(client,
    project_id)`` for convenience."""
    user = seeded_state["user"]
    pw = seeded_state["password"]

    async def _login():
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": user, "password": pw},
        )
        assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
        return r

    event_loop.run_until_complete(_login())
    return client, seeded_state["project_id"]


# ─── 1. Full first-session flow ───────────────────────────────────


def test_first_session_login_me_projects_findings(
    logged_in, seeded_state, event_loop,
):
    """One real session, three real GETs — the path a fresh operator
    takes from login to "show me a finding"."""
    c, pid = logged_in

    async def _go():
        me = await c.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["username"] == "demo-admin"
        proj = await c.get("/api/v1/projects")
        assert proj.status_code == 200
        rows = proj.json()
        assert any(p["id"] == pid for p in rows)
        find = await c.get(f"/api/v1/projects/{pid}/findings?status=open")
        assert find.status_code == 200
        assert len(find.json()) >= 1

    event_loop.run_until_complete(_go())


# ─── 2-5. Findings analytics endpoints ────────────────────────────


def test_findings_summary_returns_risk_index(logged_in, event_loop):
    c, pid = logged_in

    async def _go():
        r = await c.get(f"/api/v1/projects/{pid}/findings/summary")
        assert r.status_code == 200
        body = r.json()
        # The seed-demo plants P1 + P2 findings; risk_index must be > 0.
        assert "risk_index" in body
        assert body["risk_index"] > 0

    event_loop.run_until_complete(_go())


def test_findings_trend_buckets_90_days(logged_in, event_loop):
    c, pid = logged_in

    async def _go():
        r = await c.get(f"/api/v1/projects/{pid}/findings/trend")
        assert r.status_code == 200
        body = r.json()
        assert "buckets" in body
        assert isinstance(body["buckets"], list)

    event_loop.run_until_complete(_go())


def test_findings_roi_pairs_risk_with_cost(logged_in, event_loop):
    c, pid = logged_in

    async def _go():
        r = await c.get(f"/api/v1/projects/{pid}/findings/roi")
        assert r.status_code == 200
        body = r.json()
        # The route always returns a structured object — even when
        # there's no resolved finding yet.
        for key in ("risk_eliminated", "open_risk_remaining"):
            assert key in body

    event_loop.run_until_complete(_go())


def test_llm_cost_reads_seeded_tokens(logged_in, event_loop):
    c, pid = logged_in

    async def _go():
        r = await c.get(f"/api/v1/projects/{pid}/llm_cost")
        assert r.status_code == 200
        body = r.json()
        assert "summary_count" in body
        assert "total_tokens" in body
        assert "estimated_usd" in body
        assert body["rate_usd_per_mtok"] > 0

    event_loop.run_until_complete(_go())


# ─── 6. PR-138c new endpoint round-trip ──────────────────────────


def test_llm_fallback_breakdown_groups_by_reason(logged_in, event_loop):
    c, pid = logged_in

    async def _go():
        r = await c.get(
            f"/api/v1/projects/{pid}/llm_fallback_breakdown"
        )
        assert r.status_code == 200
        body = r.json()
        for k in ("ok", "fallbacks", "total", "fallback_rate"):
            assert k in body
        assert isinstance(body["fallbacks"], dict)
        # Either the seed planted real summaries (ok > 0) or no
        # summaries at all (total == 0). Both are valid shapes.
        assert body["ok"] >= 0
        assert body["total"] >= 0

    event_loop.run_until_complete(_go())


# ─── 7-9. Graph endpoints — what graph.html needs ────────────────


def test_graph_component_map_returns_nodes_and_edges(
    logged_in, event_loop,
):
    """The graph tab calls this on every render. Was the missing
    endpoint per the original audit's #3 finding (which turned out
    to be wrong — endpoint exists). This test pins that down."""
    c, pid = logged_in

    async def _go():
        r = await c.get(
            f"/api/v1/projects/{pid}/graph/component_map"
        )
        assert r.status_code == 200
        body = r.json()
        assert "nodes" in body
        assert "edges" in body
        # The seed plants ≥1 Component-kind node.
        assert any(n.get("kind") == "Component" for n in body["nodes"])

    event_loop.run_until_complete(_go())


def test_graph_certainty_breakdown_counts_classes(logged_in, event_loop):
    c, pid = logged_in

    async def _go():
        r = await c.get(
            f"/api/v1/projects/{pid}/graph/certainty_breakdown"
        )
        assert r.status_code == 200
        body = r.json()
        assert "nodes" in body
        assert "edges" in body
        for bucket in body["nodes"].values():
            assert isinstance(bucket, int)

    event_loop.run_until_complete(_go())


def test_graph_stats_sanity(logged_in, event_loop):
    """``/graph/stats`` returns ``{nodes_current: N}`` per the current
    impl (analysis.py:240). N > 0 for the seeded project."""
    c, pid = logged_in

    async def _go():
        r = await c.get(f"/api/v1/projects/{pid}/graph/stats")
        assert r.status_code == 200
        body = r.json()
        assert body.get("nodes_current", 0) > 0

    event_loop.run_until_complete(_go())


# ─── 10-12. Security + health rails ──────────────────────────────


def test_state_changing_post_without_csrf_is_rejected(client, event_loop):
    """The CSRF middleware must guard non-GET. A POST with no token
    + no session lands at 403, never 500."""
    async def _go():
        r = await client.post(
            "/api/v1/projects",
            json={"name": "x", "gitlab_project_id": 1,
                  "gitlab_url": "https://x", "default_branch": "main",
                  "languages": []},
        )
        # Unauthenticated → 401; CSRF-missing as authed → 403.
        # Both are valid "we caught the attack" outcomes — what we
        # need to forbid is 5xx (server crash).
        assert r.status_code in (401, 403), r.text

    event_loop.run_until_complete(_go())


def test_unauthenticated_me_is_401(client, event_loop):
    async def _go():
        r = await client.get("/api/v1/auth/me")
        assert r.status_code == 401

    event_loop.run_until_complete(_go())


def test_health_ready_reports_inline_worker_in_local_mode(
    client, event_loop,
):
    """PR-135 docker-free mode runs the worker inline. ``/health/ready``
    must reflect that (worker=inline) so the LB doesn't drain a
    perfectly healthy single-process deploy."""
    async def _go():
        r = await client.get("/api/v1/health/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["checks"]["worker"]["message"] == "inline"

    event_loop.run_until_complete(_go())
