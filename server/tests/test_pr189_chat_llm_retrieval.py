"""PR-189 — LLM-grounded query rewrite for weak-recall (Korean) chat queries.

The deterministic tokenizer drops Korean entirely, so a concept question like
"도구 호출 차단" tokenises to nothing and the lexical ranker returns score-0
noise — validated offline: 0 recall for `beforeToolCall`, while the exact name
and an English paraphrase rank #1–#12. The chat retrieval now (a) pulls a broad
candidate pool so the composing LLM can pick past English homonyms, and (b) when
recall is weak, asks the LLM for project-grounded English search terms, then
re-searches and merges.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr189")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.api.chat import _has_korean, _weak_recall


def test_has_korean():
    assert _has_korean("도구 호출 차단")
    assert _has_korean("how to 차단 this")
    assert not _has_korean("beforeToolCall approval gate")
    assert not _has_korean("")


def test_weak_recall_fires_on_korean():
    # Korean → always weak (the tokenizer can't bridge it), even if some
    # high-scoring-but-wrong homonym hit exists.
    assert _weak_recall("동기 스레드 코루틴 스케줄", [{"score": 50.0}])


def test_weak_recall_fires_on_low_score_or_empty():
    assert _weak_recall("some obscure ask", [{"score": 3.0}])
    assert _weak_recall("nothing found", [])


def test_weak_recall_skips_strong_english():
    # A strong English hit needs no LLM rewrite — stays on the fast path.
    assert not _weak_recall("safely schedule coroutine", [{"score": 21.0}])


def test_chat_wires_broad_pool_and_grounded_rewrite():
    body = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "chat.py"
    ).read_text(encoding="utf-8")
    assert "_llm_search_terms" in body
    assert "_weak_recall(" in body
    # broadened candidate pool (was top_k=6)
    assert "broad = max(body.top_k, 24)" in body
    # graceful fallback: rewrite swallows failures, never raises into request
    assert "never raises into the request" in body
