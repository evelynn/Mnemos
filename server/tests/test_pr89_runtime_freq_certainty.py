"""PR-89 — find_runtime_path frequency + get_module_summary certainty.

The Q&A review found two §11.3 fields hardcoded / missing:

* ``find_runtime_path`` always returned ``frequency: 1`` — yet PR-25's
  reconcile increments ``Edge.data.hit_count`` per observation, so
  the real frequency was queryable.
* ``get_module_summary`` returned no ``certainty_breakdown``, even
  though every claim's evidence carries a ``certainty`` field.
"""

from __future__ import annotations

from pathlib import Path

_QUERIES = (
    Path(__file__).resolve().parents[1] / "app" / "mcp" / "queries.py"
)


def _read() -> str:
    return _QUERIES.read_text(encoding="utf-8")


def test_runtime_path_frequency_is_real():
    body = _read()
    idx = body.find("async def find_runtime_path(")
    slab = body[idx:idx + 3000]
    assert "hit_count" in slab
    # The "frequency: 1" stub is gone.
    assert "min(hit_counts)" in slab
    assert "frequency" in slab and '"chain"' in slab


def test_module_summary_certainty_breakdown():
    body = _read()
    idx = body.find("async def get_module_summary(")
    slab = body[idx:idx + 1800]
    assert '"certainty_breakdown"' in slab
    assert '"verified"' in slab and '"asserted"' in slab and '"inferred"' in slab
