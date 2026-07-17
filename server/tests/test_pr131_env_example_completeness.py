"""PR-131 — .env.example 모든 사용 env var 명세.

PR-130 의 env audit 가 15+ env var 가 .env.example 미명세 발견.
운영자가 env 인식 못 해 잘못된 default 로 동작 위험. PR-131 이
모든 사용처 env var + 4단계 tier (REQUIRED/HIGH/OPTIONAL/DEV)
+ generation hints 포함하여 .env.example 보강.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_APP = _REPO / "server" / "app"


def _collect_env_uses() -> set[str]:
    """모든 os.environ.get(...) / monkeypatch.setenv(...) 사용 추출."""
    used: set[str] = set()
    for f in _APP.rglob("*.py"):
        if "__pycache__" in str(f):
            continue
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r'os\.environ(?:\.get)?\(\s*["\']([A-Z_]{2,40})["\']', src):
            used.add(m.group(1))
        for m in re.finditer(r'os\.environ\[\s*["\']([A-Z_]{2,40})["\']', src):
            used.add(m.group(1))
    return used


def _env_example_keys() -> set[str]:
    ex = (_REPO / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r'^([A-Z_]{2,40})=', ex, re.MULTILINE))


def test_env_example_documents_every_actual_env_var():
    """app/ 코드에서 사용하는 모든 MNEMOS_* / OIDC_* / NOTIFY_* /
    METRICS_* env var 가 .env.example 에 있어야 함."""
    used = _collect_env_uses()
    declared = _env_example_keys()
    # OS-standard 는 제외.
    skip = {
        "PYTHONPATH",
        "PATH",
        "HOME",
        "PWD",
        "USER",
        "LANG",
        "LC_ALL",
        "WINDIR",
    }
    relevant = used - skip
    missing = relevant - declared
    assert not missing, (
        ".env.example 누락 (운영자가 인식 못함):\n  "
        + "\n  ".join(sorted(missing))
    )


def test_env_example_has_required_tier_markers():
    """4단계 tier 표시가 명시되어 운영자가 우선순위 파악 가능."""
    ex = (_REPO / ".env.example").read_text(encoding="utf-8")
    for tier in ("REQUIRED", "HIGH", "OPTIONAL", "DEV"):
        assert f"[{tier}]" in ex or f"─── {tier}" in ex, (
            f"tier marker '{tier}' missing in .env.example"
        )


def test_env_example_provides_generation_hints_for_secrets():
    """SECRET_KEY / FERNET_KEY 같이 생성 필요한 키는 한 줄 example."""
    ex = (_REPO / ".env.example").read_text(encoding="utf-8")
    assert "token_urlsafe(48)" in ex, "SECRET_KEY 생성 hint 누락"
    assert "Fernet.generate_key()" in ex, "FERNET_KEY 생성 hint 누락"


def test_env_example_warns_against_dev_only_vars_in_prod():
    """MNEMOS_SKIP_STARTUP_VERIFY 같은 dev-only 가 [DEV] 섹션에
    있고 production must not 경고 포함."""
    ex = (_REPO / ".env.example").read_text(encoding="utf-8")
    assert "MNEMOS_SKIP_STARTUP_VERIFY" in ex
    # 같은 줄/주변에 production 경고.
    skip_idx = ex.find("MNEMOS_SKIP_STARTUP_VERIFY")
    surrounding = ex[max(0, skip_idx - 200):skip_idx]
    assert "production" in surrounding.lower() or "DEV" in surrounding


def test_env_example_kept_under_one_config_page():
    """간결성: 보안상 필수인 tenant-binding 설명까지 8 KiB 이내."""
    ex = (_REPO / ".env.example").read_text(encoding="utf-8")
    size = len(ex.encode("utf-8"))
    assert size < 8192, f".env.example {size} bytes — too verbose"
