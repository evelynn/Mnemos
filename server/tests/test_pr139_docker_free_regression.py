"""PR-139 — regression guard for the docker-free defects found in the
PR-138h build test.

Three real defects shipped on the ``serve_local`` (no-Docker) path and
no test exercised them, which is why they reached a "production-capable"
self-assessment. Each test here fails on the pre-fix code and passes
after the fix, so the gap can't silently reopen:

A. ``ensure_sqlite_schema`` imported only 8 of 12 table-bearing model
   modules → comments / user_invites / password_reset_tokens /
   data_samples / data_query_log / runtime_observations were never
   created and those features 500'd ("no such table").
B. The greedy ``/{entity_id:path}`` detail route was registered before
   ``/{entity_id:path}/sample``, so every sample GET resolved to the
   detail handler and the masked-sample view (spec §8) was unreachable.
C. ``ProgressBus`` dialed real Redis unconditionally, so the first
   ``publish`` in an inline analysis job raised ConnectionRefused and
   the run hung in "running" forever.

These are static / in-process checks — no analyzer toolchain, real
Redis, or LLM needed — so they stay deterministic in CI.
"""

from __future__ import annotations

import os

# Module-level so app.* imports see local-mode config.
os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr139")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


def test_fix_a_local_schema_imports_cover_every_model_table():
    """A: every app/models module that declares a __tablename__ must be
    imported by ensure_sqlite_schema, else create_all skips its table."""
    import inspect
    import pathlib
    import re

    from app import local_mode

    src = inspect.getsource(local_mode.ensure_sqlite_schema)
    imported = set(re.findall(r"from app\.models import (\w+)", src))

    models_dir = pathlib.Path(local_mode.__file__).parent / "models"
    needs_import = set()
    for f in models_dir.glob("*.py"):
        if f.stem in ("__init__", "base", "all_models"):
            continue
        if "__tablename__" in f.read_text(encoding="utf-8"):
            needs_import.add(f.stem)

    missing = needs_import - imported
    assert not missing, (
        f"ensure_sqlite_schema misses table-bearing model modules: {sorted(missing)} "
        f"— their tables will not be created in docker-free mode"
    )


def test_fix_b_sample_route_not_shadowed_by_detail_route():
    """B: GET .../data_entities/<id>/sample must resolve to get_sample,
    not the greedy /{entity_id:path} detail route."""
    import uuid

    from starlette.routing import Match

    # FastAPI 0.135 keeps included routers as lazy ``_IncludedRouter``
    # wrappers on the top-level app.  Match against the owning router so this
    # regression test still checks the route precedence itself rather than a
    # framework-private flattening detail.
    from app.api.data import router

    pid = str(uuid.uuid4())
    path = f"/api/v1/projects/{pid}/data_entities/data:foo.bar.Orders/sample"
    scope = {"type": "http", "method": "GET", "path": path, "headers": []}

    matched = None
    for route in router.routes:
        try:
            m, _ = route.matches(scope)
        except Exception:  # noqa: BLE001
            continue
        if m == Match.FULL:
            matched = getattr(route, "name", None) or getattr(
                getattr(route, "endpoint", None), "__name__", None
            )
            break

    assert matched == "get_sample", (
        f"sample route resolved to {matched!r}; the greedy detail route is "
        f"shadowing it again"
    )


def test_fix_c_progress_bus_uses_fakeredis_in_local_mode():
    """C: in local mode ProgressBus must use the in-process fakeredis
    server (not dial real Redis), and publish must not raise."""
    import asyncio
    import uuid

    from app.local_mode import is_local_mode
    from app.orchestrator.progress import ProgressBus

    assert is_local_mode(), "test misconfigured: not in local mode"

    bus = ProgressBus()
    # Client resolution is deliberately lazy: constructing one bus per SSE
    # request must neither connect nor allocate a new Redis pool.
    assert bus._client is None

    async def _publish_round_trip() -> None:
        # Pre-fix this raised redis.exceptions.ConnectionError (Errno 111).
        await bus.publish(uuid.uuid4(), {"event": "run_started"})
        # The shared client must still come from fakeredis in local mode,
        # never from a newly-created real Redis connection.
        assert "fakeredis" in type(bus._client).__module__, (
            f"ProgressBus dialed {type(bus._client).__module__} instead of "
            "fakeredis in local mode"
        )

    asyncio.run(_publish_round_trip())
