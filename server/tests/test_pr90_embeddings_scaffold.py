"""Embedding math/schema scaffold with cloud execution fail-closed.

The Q&A reviewer's CRITICAL #2 was that ``search_symbols`` was BM25-
only — the vector half of the spec's "벡터 + BM25 앙상블 (RRF)" was
unimplemented and no scaffold existed. Per the user's instruction
to "preempt with the library / migration", this PR shipped the schema and
deterministic fusion helpers. The former direct cloud adapter is now disabled
until it owns durable project accounting and a hard price contract:

* ``app/mcp/embeddings.py`` — ``embed_query``/``embed_batch`` fail closed;
  ``rrf_fuse``/``cosine_similarity`` remain pure helpers.
* ``alembic 0022`` — opt-in: adds the ``nodes.embedding`` column +
  HNSW cosine index. Skips itself cleanly when ``pgvector`` isn't
  installed on the connecting Postgres.
* ``search_symbols`` — when ``MNEMOS_EMBEDDING_PROVIDER`` is set AND
  the column exists, the lexical and vector rankings are RRF-fused.
* ``pyproject.toml`` — ``[search]`` extra with ``pgvector``.

Cloud vector search cannot be activated by environment variables. Unit-tested
helpers and the no-dispatch boundary are below.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_rrf_fuse_combines_two_ranked_lists():
    from app.mcp.embeddings import rrf_fuse

    # The same id near the top of both lists wins.
    fused = rrf_fuse(["A", "B", "C"], ["A", "C", "B"])
    ids = [s for s, _ in fused]
    assert ids[0] == "A"
    # And both lists contributed — every id appears once.
    assert set(ids) == {"A", "B", "C"}
    # An id that appears only in one list still ranks.
    fused2 = rrf_fuse(["X"], ["Y"])
    assert {s for s, _ in fused2} == {"X", "Y"}


def test_cosine_similarity_handles_zero_and_normal():
    from app.mcp.embeddings import cosine_similarity

    assert cosine_similarity([1, 0, 0], [1, 0, 0]) == 1.0
    assert cosine_similarity([1, 0, 0], [0, 1, 0]) == 0.0
    # Empty vectors don't blow up.
    assert cosine_similarity([], [1, 2]) == 0.0


def test_embeddings_disabled_by_default(monkeypatch):
    from app.mcp import embeddings

    monkeypatch.delenv("MNEMOS_EMBEDDING_PROVIDER", raising=False)
    assert embeddings.is_enabled() is False


@pytest.mark.asyncio
async def test_configured_cloud_embeddings_stay_fail_closed(monkeypatch, caplog):
    from app.mcp import embeddings

    monkeypatch.setenv("MNEMOS_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    assert embeddings._load_config().provider == "voyage"
    assert embeddings.is_enabled() is False
    assert await embeddings.embed_batch(["bounded query"]) is None
    assert embeddings.CLOUD_EMBEDDING_DISABLED_REASON in caplog.text
    # The disabled module has no HTTP client left to accidentally call.
    assert not hasattr(embeddings, "httpx")


def test_pyproject_declares_search_extra():
    text = (
        Path(__file__).resolve().parents[1] / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert "[project.optional-dependencies]" in text
    assert "search = [" in text
    assert "pgvector" in text


def test_alembic_migration_present_and_opt_in():
    body = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "0022_node_embedding.py"
    ).read_text(encoding="utf-8")
    # Chains correctly.
    assert "0021_secret_organization_id" in body
    # Opt-in: skips when pgvector isn't installed, so the base alembic
    # chain doesn't break on a vanilla Postgres.
    assert "_has_pgvector" in body
    assert "CREATE EXTENSION IF NOT EXISTS vector" in body
    assert "vector_cosine_ops" in body


def test_search_symbols_wires_rrf_hook():
    body = (
        Path(__file__).resolve().parents[1] / "app" / "mcp" / "queries.py"
    ).read_text(encoding="utf-8")
    idx = body.find("async def search_symbols(")
    slab = body[idx:idx + 6000]
    assert "from app.mcp.embeddings import" in slab
    assert "rrf_fuse" in slab
    assert "is_enabled()" in slab
    # Vector lookup deferred behind try/except — graceful fallback.
    assert "ORDER BY embedding <=> " in slab
