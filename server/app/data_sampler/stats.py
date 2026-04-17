"""Lightweight column statistics computed from already-masked samples."""

from __future__ import annotations

from collections import Counter
from typing import Any


def compute_column_stats(
    columns: list[str], rows: list[list[Any]]
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    if not rows:
        return stats
    total = len(rows)
    for i, col in enumerate(columns):
        values = [row[i] for row in rows]
        non_null = [v for v in values if v is not None]
        counter = Counter(non_null)
        top = counter.most_common(10)
        numeric_vals: list[float] = []
        for v in non_null:
            try:
                numeric_vals.append(float(v))
            except (TypeError, ValueError):
                continue
        numeric_stats = None
        if numeric_vals:
            numeric_vals.sort()
            numeric_stats = {
                "min": numeric_vals[0],
                "max": numeric_vals[-1],
                "avg": sum(numeric_vals) / len(numeric_vals),
                "p50": numeric_vals[len(numeric_vals) // 2],
            }
        stats[col] = {
            "null_ratio": round(1 - (len(non_null) / total), 4) if total else 0.0,
            "distinct_count_estimate": len(counter),
            "value_distribution": [
                {"value": v, "frequency": c} for v, c in top
            ],
            "numeric_stats": numeric_stats,
        }
    return stats
