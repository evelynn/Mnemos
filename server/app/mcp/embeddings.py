"""Embedding provider adapter (spec §11.3, §15).

The Q&A reviewer flagged that ``search_symbols`` was BM25-only — the
vector half of the spec's "벡터 + BM25 앙상블 (RRF)" was unimplemented.
This module is the scaffold a deployment turns on: set
``MNEMOS_EMBEDDING_PROVIDER`` to ``voyage`` (spec default —
``voyage-code-3``) or ``openai``, plus the matching API key, and
``embed_query`` / ``embed_batch`` start returning real vectors. The
search layer's RRF fusion is the next hook (PR-91).

When no provider is configured the adapters return ``None``, so
``search_symbols`` keeps its current pure-lexical behaviour and the
existing tests stay green — there is no regression risk in shipping
the scaffold ahead of an embedding budget.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Sequence

import httpx

log = logging.getLogger(__name__)

# Output dimensionality the platform pins. Voyage ``voyage-code-3`` is
# 1024 dims; OpenAI ``text-embedding-3-small`` is 1536. Provider
# implementations truncate / pad to this when configured to.
EMBEDDING_DIM = 1024


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
    log.warning("unknown MNEMOS_EMBEDDING_PROVIDER=%r — embeddings disabled", provider)
    return None


def is_enabled() -> bool:
    cfg = _load_config()
    return bool(cfg and cfg.api_key)


async def embed_batch(texts: Sequence[str]) -> list[list[float]] | None:
    """Return one vector per input text, or None if no provider is
    configured. Each adapter swallows network / API errors and returns
    None — embeddings are an enhancement, never a hard dependency."""
    cfg = _load_config()
    if cfg is None or not cfg.api_key or not texts:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if cfg.provider == "voyage":
                payload = {"input": list(texts), "model": cfg.model}
            else:  # openai-compatible
                payload = {"input": list(texts), "model": cfg.model}
            r = await client.post(
                cfg.base_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
        # Both providers return ``{data: [{embedding: [...]}, ...]}``.
        return [item["embedding"] for item in data.get("data", [])]
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding call failed (%s): %s", cfg.provider, exc)
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
