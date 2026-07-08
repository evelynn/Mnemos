"""PR-197 — ggoss-ts graceful degradation when node_modules isn't built.

ggoss-ts is the one docker-free analyzer with a non-stdlib dependency (the
``typescript`` npm package). Without ``node_modules`` (``npm ci`` not run),
``node`` crashed with an opaque ERR_MODULE_NOT_FOUND mid-stream. Availability
now gates on the dependency being present, so a TS project cleanly falls back
to agent extraction (recorded reason) instead of a crash — and the analyzer
is honest about needing setup. Stdlib analyzers are unaffected.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TS_TYPESCRIPT = _ROOT / "analyzers" / "ggoss-ts" / "node_modules" / "typescript"


@pytest.fixture()
def _flag_on(monkeypatch):
    monkeypatch.setenv("MNEMOS_INREPO_ANALYZERS", "1")
    import app.analyzers.runner as runner
    import app.analyzers.registry as reg
    importlib.reload(runner)
    importlib.reload(reg)
    yield reg, runner
    importlib.reload(runner)
    importlib.reload(reg)


def test_deps_ok_only_gates_ggoss_ts(_flag_on):
    _reg, runner = _flag_on
    # stdlib analyzers have no external deps → always ok.
    for b in ("ggoss-py", "ggoss-cpp", "ggoss-java", "ggoss-kotlin", "ggoss-web"):
        script = runner._INREPO_ANALYZERS[b][1]
        assert runner._inrepo_deps_ok(b, script) is True


def test_ts_availability_tracks_node_modules(_flag_on):
    reg, runner = _flag_on
    ts_script = runner._INREPO_ANALYZERS["ggoss-ts"][1]
    have_typescript = _TS_TYPESCRIPT.exists()
    # deps_ok mirrors whether the typescript package is installed.
    assert runner._inrepo_deps_ok("ggoss-ts", ts_script) is have_typescript
    # And that flows through to inrepo_script / availability: absent →
    # not runnable in-repo → falls back (no crash); present → runnable.
    if not have_typescript:
        assert runner.inrepo_script("ggoss-ts") is None
        assert reg.analyzer_available("typescript") is False
    else:
        assert runner.inrepo_script("ggoss-ts") is not None


def test_stdlib_analyzers_stay_available(_flag_on):
    reg, _runner = _flag_on
    # The ts gate must not affect the pure-stdlib analyzers.
    assert reg.analyzer_available("python") is True
    assert reg.analyzer_available("cpp") is True
    assert reg.analyzer_available("java") is True
    assert reg.analyzer_available("kotlin") is True
