"""PR-143 — cross-tier flow / process analysis.

trace_flow hands FE/BE/DB source slices to Claude Code and returns a
structured end-to-end trace (steps, per-boundary signals, flag values +
meanings, rows touched), persisted as a level-4 Summary.

Deterministic tests (no live LLM): tier/language classification, slug,
flow normalisation, prompt assembly, and route registration. The live
Claude Code trace is exercised by the 3-tier demo, not CI.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr143")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


def test_classify_tiers_and_slug():
    from app.api.flow import _classify, _slug

    assert _classify(Path("/x/frontend/checkout.js")) == ("frontend", "javascript")
    assert _classify(Path("/x/backend/orders_handler.py")) == ("backend", "python")
    assert _classify(Path("/x/db/schema.sql")) == ("database", "sql")
    # extension-only fallbacks
    assert _classify(Path("/svc/Handler.cs"))[0] == "backend"
    assert _classify(Path("/q/migrations/001.sql")) == ("database", "sql")
    assert _slug("Place an order (checkout)!") == "place-an-order-checkout"


def test_flow_normalise_tolerates_missing_keys():
    from app.extractor.agent_flow import _normalise

    out = _normalise({"summary": "x"})
    assert out["summary"] == "x"
    assert out["steps"] == [] and out["flags"] == [] and out["data_touched"] == []
    assert out["open_questions"] == []
    # bad types coerced to empty lists
    out2 = _normalise({"steps": "not-a-list", "flags": [{"name": "f"}]})
    assert out2["steps"] == [] and out2["flags"] == [{"name": "f"}]


def test_flow_prompt_includes_all_tiers_and_entry():
    from app.extractor.agent_flow import _build_flow_prompt

    prompt = _build_flow_prompt(
        "place order",
        [
            {"tier": "frontend", "label": "a.js", "language": "javascript", "code": "FE"},
            {"tier": "backend", "label": "b.py", "language": "python", "code": "BE"},
            {"tier": "database", "label": "c.sql", "language": "sql", "code": "DDL"},
        ],
    )
    assert "place order" in prompt
    for marker in ("tier=frontend", "tier=backend", "tier=database", "FE", "BE", "DDL"):
        assert marker in prompt
    # asks for flags + meanings + data_touched — the user's requirement
    assert '"flags"' in prompt and '"meaning"' in prompt and '"data_touched"' in prompt


def test_trace_flow_route_registered():
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/v1/projects/{project_id}/trace_flow" in paths


def test_flow_level_is_above_l3():
    from app.extractor.agent_flow import FLOW_LEVEL

    assert FLOW_LEVEL == 4  # L1-L3 are symbol/module/component summaries
