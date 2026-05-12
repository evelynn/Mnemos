"""Dialect single-source-of-truth + registry alignment (spec §2.5)."""

from __future__ import annotations

import pytest


def test_supported_dialects_is_frozen():
    """Mutating the canonical set by accident must raise."""
    from app.safety.dialects import SUPPORTED_DIALECTS

    with pytest.raises((AttributeError, TypeError)):
        SUPPORTED_DIALECTS.add("postgres")  # type: ignore[attr-defined]


def test_is_supported_accepts_known_dialects():
    from app.safety.dialects import is_supported

    assert is_supported("mssql")
    assert is_supported("oracle")
    assert not is_supported("postgres")
    assert not is_supported("")
    assert not is_supported(None)


def test_registry_alignment_passes_in_default_state():
    """Whenever a dialect is added to one side, the other must match.

    This is the boot-time assertion that runs in `main.lifespan`. If a
    future PR adds postgres to ``SUPPORTED_DIALECTS`` without wiring the
    analyzer binary, this test will be the first to scream.
    """
    from app.safety.dialects import assert_registry_aligned

    # Should not raise with the shipped configuration.
    assert_registry_aligned()


def test_registry_alignment_detects_drift(monkeypatch):
    from app.analyzers import registry
    from app.safety import dialects

    monkeypatch.setattr(registry, "_BINARIES", {"mssql": "ggoss-sql-mssql"})

    with pytest.raises(RuntimeError, match="drift"):
        dialects.assert_registry_aligned()
