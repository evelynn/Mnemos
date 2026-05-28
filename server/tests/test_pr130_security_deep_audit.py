"""PR-130 — 세부 보안 + 코드 quality audit.

자율 라운드 계속, 사용자 지시 "좀더 세부적인 검토 시작".

audit 영역 (8 categories):
- 보안 헤더 (CSP/HSTS/XFO 등)
- cookie flag (secure/httponly/samesite)
- CSRF middleware 정확성
- rate limit 적용
- API error response shape
- DB schema / Alembic 일관성
- Pydantic input validation
- logging / production 잔여물
- OIDC nonce / state / PKCE

발견된 진짜 보안 이슈 (2건, 모두 fix)
====================================
1. CSRF cookie 의 secure flag 미명시 — production HTTPS 환경에서
   HTTP 응답에도 cookie 가 포함될 위험. PR-130 — session_cookie_secure
   설정 따라가도록 변경.

2. OIDC nonce 미사용 — id_token replay 공격에 취약. state + PKCE 가
   1차 방어이지만 nonce 가 표준 belt-and-braces. PR-130 — /login 에서
   nonce 생성, Redis stash, /callback 에서 claims.nonce 와 비교.

audit PASS 영역 (이미 깨끗):
- 모든 6 보안 헤더 적용 (CSP/HSTS/XFO/XCT/Referrer/Permissions)
- 모든 cookie 가 httponly + samesite 설정
- API error shape 일관 (snake_case detail, status_code 명시)
- alembic migration chain 단일 head
- 모든 FK 인덱스 존재
- print() 잔여 0
- bare except pass 0
- SQL injection 위험 0 (f-string text() 0)
- 모든 BaseModel 필드 명시
- bcrypt cost 12
- 세션 토큰 192 bits entropy
- OIDC state + PKCE 정상
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "app"


# ─── PR-130 fix 1: CSRF cookie secure flag ────────────────────────


def test_csrf_cookie_respects_session_cookie_secure_setting():
    """Pre-PR: CSRF cookie 가 secure 미명시 → FastAPI 기본 False →
    production HTTPS 에서도 HTTP 응답에 포함 가능. Post-PR: settings
    의 session_cookie_secure 값을 따라감."""
    src = (_APP / "security" / "csrf.py").read_text(encoding="utf-8")
    # The set_cookie call must pass secure=get_settings().session_cookie_secure.
    assert "secure=get_settings().session_cookie_secure" in src, (
        "CSRF cookie missing secure flag tied to session_cookie_secure"
    )


def test_csrf_imports_settings_for_secure_flag():
    src = (_APP / "security" / "csrf.py").read_text(encoding="utf-8")
    assert "from app.config import get_settings" in src


# ─── PR-130 fix 2: OIDC nonce ─────────────────────────────────────


def test_oidc_login_generates_and_stashes_nonce():
    """/login 이 nonce 생성하여 Redis 의 stashed state 에 함께
    저장해야 callback 이 비교 가능."""
    src = (_APP / "auth" / "oidc.py").read_text(encoding="utf-8")
    # nonce 생성 + redis 저장 + IdP params 포함.
    assert 'nonce = _b64url(secrets.token_bytes(' in src
    assert '"nonce": nonce' in src, "nonce not included in Redis stash"
    # IdP authorize request 에도 포함.
    auth_req_block = src[src.find("params = {"):src.find("from urllib")]
    assert '"nonce": nonce' in auth_req_block, (
        "nonce not forwarded to IdP authorize request"
    )


def test_oidc_callback_verifies_nonce_in_id_token():
    """/callback 에서 id_token 의 nonce claim 이 stashed nonce 와
    일치 확인. 불일치 시 401."""
    src = (_APP / "auth" / "oidc.py").read_text(encoding="utf-8")
    # expected_nonce 추출 + 비교 + raise.
    assert 'expected_nonce = stashed_data.get("nonce")' in src
    assert 'id_token_nonce = claims.get("nonce")' in src
    assert "id_token_nonce_mismatch" in src
    # Forward-compat: pre-PR-130 in-flight states 에 nonce 없으면 skip.
    assert "if expected_nonce is not None:" in src


def test_oidc_nonce_entropy_at_least_128_bits():
    """token_bytes(N) 의 N 은 ≥16 (128 bits). 표준 권장 192+ bits."""
    src = (_APP / "auth" / "oidc.py").read_text(encoding="utf-8")
    nonce_line = next(
        line for line in src.splitlines()
        if "nonce = _b64url(secrets.token_bytes(" in line
    )
    m = re.search(r'token_bytes\((\d+)\)', nonce_line)
    assert m
    n_bytes = int(m.group(1))
    assert n_bytes >= 16, f"nonce entropy too low: {n_bytes*8} bits"


# ─── audit PASS guards: security headers ──────────────────────────


_REQUIRED_HEADERS = (
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Strict-Transport-Security",
    "Content-Security-Policy",
    "Permissions-Policy",
)


def test_all_security_headers_present():
    """모든 6 보안 헤더가 SecurityHeadersMiddleware 에 의해 적용."""
    src = (_APP / "security" / "headers.py").read_text(encoding="utf-8")
    for h in _REQUIRED_HEADERS:
        assert h in src, f"security header missing: {h}"


def test_session_cookies_all_carry_secure_httponly_samesite():
    """모든 cookie 설정처가 3 flag 모두 명시. Multi-line set_cookie
    calls — 괄호 매칭으로 정확히 인자 추출."""
    paths = [
        "auth/oidc.py", "api/auth.py", "dashboard/router.py", "security/csrf.py",
    ]
    for rel in paths:
        src = (_APP / rel).read_text(encoding="utf-8")
        for m in re.finditer(r'set_cookie\s*\(', src):
            # 괄호 매칭으로 닫는 ) 찾기.
            depth = 1
            start = m.end()
            pos = start
            while pos < len(src) and depth > 0:
                if src[pos] == '(':
                    depth += 1
                elif src[pos] == ')':
                    depth -= 1
                pos += 1
            args = src[start:pos - 1].lower()
            assert "secure" in args, f"{rel}: set_cookie missing secure"
            assert "httponly" in args, f"{rel}: set_cookie missing httponly"
            assert "samesite" in args, f"{rel}: set_cookie missing samesite"


# ─── audit PASS: API error shape, no print, no bare except ─────


def test_no_print_in_production_code():
    """CLI / seed-demo 제외하고 print() 호출 0."""
    for f in sorted(_APP.rglob("*.py")):
        rel = str(f.relative_to(_APP))
        if "__pycache__" in rel:
            continue
        # CLI 모듈은 사용자-facing 출력 합법.
        if rel.endswith("cli.py") or rel.endswith("seed_demo.py"):
            continue
        src = f.read_text(encoding="utf-8")
        # Strip docstrings rough — just check non-docstring code
        # by looking at indentation: print at line start (no indent)
        # would be module-level. Indented print 는 함수 안.
        for m in re.finditer(r'^\s*print\(', src, re.MULTILINE):
            # Skip inside triple-quoted strings (cheap heuristic).
            before = src[:m.start()]
            tq_count = before.count('"""') + before.count("'''")
            if tq_count % 2 == 1:
                continue  # inside docstring
            pytest.fail(f"{rel}: print() at offset {m.start()}")


def test_no_bare_except_pass():
    """Bare ``except: pass`` (정확히 그 패턴) 만 검출. ``except
    Exception: rollback()`` 같은 정상 fallback 은 OK."""
    for f in sorted(_APP.rglob("*.py")):
        rel = str(f.relative_to(_APP))
        if "__pycache__" in rel:
            continue
        src = f.read_text(encoding="utf-8")
        # Strict: bare ``except:`` (no exception type) followed by
        # nothing but ``pass``.
        if re.search(r'^\s*except\s*:\s*\n\s*pass\s*$', src, re.MULTILINE):
            pytest.fail(f"{rel}: bare 'except: pass' detected")


def test_no_sql_text_with_fstring_injection_risk():
    """SQLAlchemy text() 에 f-string 또는 + 결합 = SQL injection 위험."""
    for f in sorted(_APP.rglob("*.py")):
        rel = str(f.relative_to(_APP))
        if "__pycache__" in rel:
            continue
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r'text\(\s*f["\']', src):
            pytest.fail(f"{rel}: text(f'...') at offset {m.start()} — SQL injection risk")


# ─── audit PASS: DB schema integrity ─────────────────────────────


def test_alembic_has_single_head():
    """multi-head migration 은 alembic upgrade 시 충돌. 항상 단일 head."""
    mig_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    if not mig_dir.exists():
        pytest.skip("no alembic dir")
    migrations = list(mig_dir.glob("*.py"))
    rev_to_file = {}
    down_to_file = {}
    for m in migrations:
        src = m.read_text(encoding="utf-8")
        rev_m = re.search(r'^revision[^=]*=\s*["\']([^"\']+)["\']', src, re.MULTILINE)
        down_m = re.search(r'^down_revision[^=]*=\s*["\']([^"\']*)["\']', src, re.MULTILINE)
        if rev_m:
            rev_to_file[rev_m.group(1)] = m.name
        if down_m and down_m.group(1):
            down_to_file.setdefault(down_m.group(1), []).append(m.name)
    heads = [r for r in rev_to_file if r not in down_to_file]
    assert len(heads) == 1, f"alembic has {len(heads)} heads: {heads}"


# ─── PR-130 audit infrastructure 자체 검증 ────────────────────────


def test_audit_smoke_no_false_positives_via_module_import():
    """모듈 import 가 정상 — audit 가 의도치 않게 production 깨지지 않음."""
    import importlib
    for mod in (
        "app.security.csrf",
        "app.auth.oidc",
        "app.security.headers",
        "app.security.rate_limit",
    ):
        importlib.import_module(mod)
