"""Canonical test data for direct-ORM graph publication fixtures."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from app.graph_publication import GraphPublicationReceipt, PromotionCounts


def published_run_fields(
    *,
    generation: int,
    published_at: datetime,
    authoritative_sources: Iterable[str] = (),
    stats: dict[str, Any] | None = None,
    coverage_sealed_at: datetime | None = None,
) -> dict[str, Any]:
    """Return the persisted run fields required by one valid ready head."""

    sealed_at = coverage_sealed_at or published_at
    sources = tuple(sorted(set(authoritative_sources)))
    receipt = GraphPublicationReceipt(
        generation=generation,
        base_generation=generation - 1,
        published_at=published_at,
        authoritative_sources=sources,
        coverage_sealed_at=sealed_at,
        counts=PromotionCounts(),
    ).to_payload()
    return {
        "graph_base_generation": generation - 1,
        "graph_authoritative_sources": list(sources),
        "graph_coverage_sealed_at": sealed_at,
        "stats": {**(stats or {}), "graph_publication": receipt},
    }
