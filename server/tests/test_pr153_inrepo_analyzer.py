"""PR-153 — run the in-repo pure-stdlib analyzer (ggoss-py) without Docker.

The basic (non-Docker) config ships the analyzer source under analyzers/.
ggoss-py needs only Python's ast, so when MNEMOS_INREPO_ANALYZERS is set
(serve_local does this) and the binary isn't on PATH, the runner invokes
``python <script> <verb> <path>``. A docker-free Python project then gets
*verified* deterministic extraction instead of the inferred Claude path.

Gated behind the env flag so the test suite (which doesn't set it) is
unaffected — these tests toggle the flag explicitly.
"""

from __future__ import annotations

import os

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr153")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

import pytest


@pytest.fixture
def _flag_on(monkeypatch):
    monkeypatch.setenv("MNEMOS_INREPO_ANALYZERS", "1")


def test_inrepo_script_gated_by_flag(monkeypatch):
    from app.analyzers.runner import inrepo_script

    monkeypatch.delenv("MNEMOS_INREPO_ANALYZERS", raising=False)
    assert inrepo_script("ggoss-py") is None, "off by default"

    monkeypatch.setenv("MNEMOS_INREPO_ANALYZERS", "1")
    p = inrepo_script("ggoss-py")
    assert p is not None and p.name == "ggoss_py.py" and p.exists()
    # ggoss-ts also has an in-repo entrypoint (PR-181), gated on ``node``
    # being installed since it runs under node, not Python.
    import shutil

    ts = inrepo_script("ggoss-ts")
    if shutil.which("node"):
        assert ts is not None and ts.name == "index.mjs" and ts.exists()
    else:
        assert ts is None
    # analyzers with no in-repo source stay on the Claude fallback path.
    assert inrepo_script("ggoss-csharp") is None


def test_analyzer_available_uses_inrepo_when_flagged(_flag_on):
    import importlib

    import app.analyzers.registry as reg

    importlib.reload(reg)
    # ggoss-py binary isn't installed here, but the in-repo source is → the
    # python analyzer is "available" (verified), so the Claude fallback for
    # python must not fire in the basic config.
    assert reg.analyzer_available("python") is True
    # languages with no in-repo entrypoint stay on the Claude fallback path
    # (cpp gained an in-repo analyzer in PR-191, so it no longer qualifies)
    assert reg.analyzer_available("ruby") is False
    importlib.reload(reg)  # restore module state for other tests


def test_serve_local_enables_inrepo_analyzers():
    import inspect

    from app import serve_local

    src = inspect.getsource(serve_local)
    assert "MNEMOS_INREPO_ANALYZERS" in src, "serve_local must enable in-repo analyzers"
