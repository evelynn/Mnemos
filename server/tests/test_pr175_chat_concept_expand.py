"""PR-175 — Korean/concept query expansion for the Chat retrieval.

A pure-Korean question tokenises to no ASCII terms, so the lexical ranker
can't reach English code. ``_expand_terms`` maps concept words to English
keywords so the chat retrieves the right symbols. Pure function — no LLM,
no DB — so it runs in CI.
"""

from app.api.chat import _expand_terms


def test_korean_concepts_map_to_english_keywords():
    assert "auth" in _expand_terms("인증은 어떻게 동작해?")
    assert "session" in _expand_terms("세션 만료는 어디서 처리해?")
    assert "payment" in _expand_terms("결제 처리는 어디서 해?")
    assert "database" in _expand_terms("데이터베이스 쿼리는 어디서 실행돼?")
    assert "audit" in _expand_terms("감사 로그는 어떻게 보여줘?")


def test_agglutinative_substring_match():
    # "인증을" / "인증은" both contain "인증".
    assert "auth" in _expand_terms("이 앱의 인증을 설명해줘")


def test_english_only_question_not_expanded():
    # An identifier the operator typed needs no concept mapping.
    assert _expand_terms("what does saveCache do") == []


def test_returns_deduped_bounded_list():
    out = _expand_terms("인증 로그인 세션 토큰 비밀번호 권한 보안")
    assert out == list(dict.fromkeys(out))  # no duplicates
    assert len(out) <= 10
