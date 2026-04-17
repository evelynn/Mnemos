"""Merge & reconcile engine.

Week 2 ships a trivial "insert-or-replace current row" merger. Week 3 layers
in multi-source merge + certainty ranking, Week 6 adds incremental runtime
correlation (spec §9).
"""

from app.merge.writer import upsert_edge, upsert_node  # noqa: F401
