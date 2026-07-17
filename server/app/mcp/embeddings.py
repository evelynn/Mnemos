"""Deterministic embedding helpers and a fail-closed cloud boundary.

The RRF/cosine helpers and pgvector schema remain useful, but the former
Voyage/OpenAI HTTP adapter had no project identity, durable physical-attempt
ledger, provider usage settlement, or immutable worst-price reservation.
Environment opt-in therefore cannot authorize a cloud request.  Until that
full contract exists, every search remains lexical and ``embed_*`` returns
``None`` without opening a network client.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Sequence

log = logging.getLogger(__name__)

# Output dimensionality the platform pins. Voyage ``voyage-code-3`` is
# 1024 dims; OpenAI ``text-embedding-3-small`` is 1536. Provider
# implementations truncate / pad to this when configured to.
EMBEDDING_DIM = 1024
CLOUD_EMBEDDING_EXECUTION_ENABLED = False
CLOUD_EMBEDDING_DISABLED_REASON = (
    "project_scoped_embedding_accounting_contract_unavailable"
)


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str  # "voyage" / "openai" / "" (disabled)
    api_key: str
    model: str
    base_url: str
    dim: int = EMBEDDING_DIM


def _load_config() -> EmbeddingConfig | None:
    """Return None when no provider is configured — embeddings disabled."""
    provider = (os.environ.get("MNEMOS_EMBEDDING_PROVIDER") or "").lower()
    if not provider:
        return None
    if provider == "voyage":
        return EmbeddingConfig(
            provider="voyage",
            api_key=os.environ.get("VOYAGE_API_KEY", ""),
            model=os.environ.get("MNEMOS_EMBEDDING_MODEL", "voyage-code-3"),
            base_url="https://api.voyageai.com/v1/embeddings",
        )
    if provider == "openai":
        return EmbeddingConfig(
            provider="openai",
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            model=os.environ.get(
                "MNEMOS_EMBEDDING_MODEL", "text-embedding-3-small"
            ),
            base_url="https://api.openai.com/v1/embeddings",
        )
    log.warning(
        "unknown embedding provider configured; embeddings disabled "
        "failure_code=embedding_provider_unsupported"
    )
    return None


def is_enabled() -> bool:
    """Cloud embeddings cannot be enabled by environment configuration."""

    return False


async def embed_batch(texts: Sequence[str]) -> list[list[float]] | None:
    """Return no cloud vectors until durable project accounting exists.

    A configured provider is logged explicitly for direct callers.  Normal
    MCP search checks :func:`is_enabled` and stays on deterministic lexical
    ranking, so it never reaches this boundary or emits one warning per query.
    """

    cfg = _load_config()
    if cfg is not None and cfg.api_key and texts:
        log.warning(
            "cloud embedding dispatch refused (%s): %s",
            cfg.provider,
            CLOUD_EMBEDDING_DISABLED_REASON,
        )
    return None


async def embed_query(text: str) -> list[float] | None:
    """Convenience wrapper for the single-string search-time case."""
    out = await embed_batch([text])
    return out[0] if out else None


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Pure-Python cosine — falls back when pgvector isn't available."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def rrf_fuse(
    lexical_ranked: Sequence[str],
    vector_ranked: Sequence[str],
    k: int = 60,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion (spec §11.3) — combine two ranked lists
    of symbol ids by ``score = sum 1/(k + rank)``. Returns
    ``[(id, fused_score)]`` sorted desc. Pure function, unit-testable
    without an embedding API."""
    scores: dict[str, float] = {}
    for rank, sid in enumerate(lexical_ranked, start=1):
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank)
    for rank, sid in enumerate(vector_ranked, start=1):
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
