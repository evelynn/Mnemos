"""Single source of truth for supported DB dialects (spec §2.5).

Everything that touches dialect strings — the analyzer registry, the
ProjectDB enum, the SQLGlot rewriter, the dashboard — must import from
here. The startup assertion below catches drift between this module
and ``app.analyzers.registry`` so a future "we added MySQL on one side
but forgot the other" lands as a boot-time failure instead of a silent
422 in production.
"""

from __future__ import annotations

from typing import Literal

Dialect = Literal["mssql", "oracle"]

# `frozenset` so callers cannot mutate the canonical set by accident.
SUPPORTED_DIALECTS: frozenset[Dialect] = frozenset({"mssql", "oracle"})


def is_supported(value: str | None) -> bool:
    return value in SUPPORTED_DIALECTS


def assert_registry_aligned() -> None:
    """Boot-time guard against drift with the analyzer registry."""
    from app.analyzers.registry import _BINARIES

    registry_dialects = {
        kind for kind in _BINARIES.keys() if kind in ("mssql", "oracle")
    }
    if registry_dialects != set(SUPPORTED_DIALECTS):
        raise RuntimeError(
            "dialects/registry drift: "
            f"db.dialects.SUPPORTED_DIALECTS={set(SUPPORTED_DIALECTS)} "
            f"vs analyzers.registry={registry_dialects}. "
            "Update both when adding or removing a DB dialect."
        )


__all__ = ["Dialect", "SUPPORTED_DIALECTS", "is_supported", "assert_registry_aligned"]
