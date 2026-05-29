"""PR-132b — exception message leak in HTTPException detail.

PR-132 audit 가 OIDC 의 `detail=f"...: {exc}"` 4건 발견. PyJWT
exception message 가 attacker 에게 token 검증 실패 hint 제공.

Fix: detail 은 generic identifier (id_token_malformed 등),
exception 은 server log 에 warning 으로 기록.
"""

from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def test_oidc_does_not_leak_exception_str_in_detail():
    """OIDC 의 모든 raise HTTPException 의 detail 이 f-string + {exc}
    형식 아닌 generic identifier 만."""
    src = (_APP / "auth" / "oidc.py").read_text(encoding="utf-8")
    # detail=f"...{exc}" 또는 detail=f"...{exc.X}" 패턴 검출.
    bad = re.findall(r'detail\s*=\s*f["\'][^"\']*\{exc(?:\.\w+)?\}', src)
    assert not bad, (
        "OIDC detail leaks exception text — generic identifier 사용 필요:\n"
        + "\n".join(f"  - {b}" for b in bad)
    )


def test_oidc_id_token_errors_use_generic_detail():
    """모든 OIDC id_token_* 에러가 generic 식별자."""
    src = (_APP / "auth" / "oidc.py").read_text(encoding="utf-8")
    # 다음 4개 모두 generic detail 사용.
    for token in (
        '"id_token_malformed"',
        '"id_token_jwk_invalid"',
        '"id_token_unsupported_kty"',
        '"id_token_invalid"',
    ):
        assert f"detail={token}" in src, (
            f"OIDC error must use generic detail {token}"
        )


def test_oidc_logs_warning_alongside_generic_detail():
    """generic detail 만 사용자에게 보내고 exc 전문은 server log
    에 warning 으로 기록 (debugging 가능)."""
    src = (_APP / "auth" / "oidc.py").read_text(encoding="utf-8")
    # logger.warning(...exc) 호출이 detail 옆에 있어야.
    warnings = re.findall(r'logger\.warning\(\s*"oidc\s+id_token_\w+', src)
    assert len(warnings) >= 4, (
        f"expected ≥4 oidc warning logs (one per id_token_* error), "
        f"got {len(warnings)}"
    )


# ─── 회귀 가드: 다른 endpoint 에서도 exc str leak 없음 ──────────


def test_no_endpoint_detail_leaks_raw_exception_str():
    """모든 HTTPException 의 detail 이 f-string + raw ``{exc}`` 형식 아님.
    Controlled attributes (``exc.kind``, ``exc.code``,
    ``exc.__class__.__name__``) 는 OK — fixed enums or class names
    safe to surface."""
    api_dir = _APP / "api"
    bad = []
    for f in api_dir.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        # raw {exc} 또는 {exc} 직접 (attribute access 없이) — 이게 leak.
        for m in re.finditer(r'detail\s*=\s*f["\'][^"\']*\{exc\}', src):
            bad.append(f"{f.name}: {m.group(0)}")
    assert not bad, "endpoint detail leaks raw exception str:\n  " + "\n  ".join(bad)
