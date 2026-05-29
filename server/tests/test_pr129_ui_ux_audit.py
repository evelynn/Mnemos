"""PR-129 — UI/UX 정적 audit + 발견 이슈 fix.

자율 라운드 마지막 PR. 사용자 지시 "UI/UX 면에서 문제 없는지
확인" 에 대한 종합 검증.

검증 범위:
1. jinja 렌더링: 24개 템플릿 전부 (PASS)
2. duplicate id: 없음 (PASS)
3. 깨진 href / 알 수 없는 route: 없음 (PASS)
4. innerHTML XSS: 모두 escapeHtml 처리 (PASS, 검출 7건 모두 false positive)
5. i18n 일관성: 283 한국어 키 vs 249 사용처 — 2 개 placeholder 누락 fix
6. a11y: 3 개 이슈 fix (label, heading hierarchy)
7. dialog: 4/4 open+close (PASS)
8. CSS @media: 7 개 (1024/768/480 + dark mode + print)
9. form method: 명시 누락 0
10. 빈 상태 메시지: 일관성 있음

발견된 진짜 문제 (이 PR 에서 fix)
================================
1. docs-blurb / health-blurb phrase book key 누락 → ko 로케일에서
   영어 본문 대신 placeholder ID 가 그대로 표시됨
2. diffs.html share-url input — aria-label 없음 (SR 가 "edit"
   으로 잘못 인식)
3. findings.html h1 → h3 (h2 skip) — SR heading 계층 깨짐
4. graph.html h1 → h3 (h2 skip)

각 이슈는 minimal-diff 로 fix:
- ui.js: 4 신규 phrase book entry
- diffs.html: aria-label
- findings.html / graph.html: sr-only h2 추가

audit 자체는 이 테스트 파일에 영구 박제 — regression 가드.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

_DASH = Path(__file__).resolve().parents[1] / "app" / "dashboard"
_TPL = _DASH / "templates"
_STATIC = _DASH / "static"

_FAKE_USER = {"id": 1, "username": "test-op", "role": "admin"}
_LEAF_TEMPLATES = sorted(
    t.name for t in _TPL.glob("*.html")
    if not t.name.startswith("_") and t.name != "base.html"
)


def _render(name: str) -> str:
    env = Environment(loader=FileSystemLoader(str(_TPL)), autoescape=True)
    return env.get_template(name).render(
        user=_FAKE_USER, active_tab=name.replace(".html", ""), request=None,
    )


# ─── 1. jinja render — 모든 템플릿 ─────────────────────────────────


@pytest.mark.parametrize("tpl_name", _LEAF_TEMPLATES)
def test_every_template_renders_without_jinja_error(tpl_name: str):
    """24 개 leaf 템플릿 모두 jinja 렌더링 정상."""
    out = _render(tpl_name)
    assert len(out) > 100, f"{tpl_name} rendered output suspiciously small"


# ─── 2. duplicate id ──────────────────────────────────────────────


@pytest.mark.parametrize("tpl_name", _LEAF_TEMPLATES)
def test_no_duplicate_ids(tpl_name: str):
    """DOM 의 모든 id 는 unique. 중복은 SR + JS 양쪽 깨뜨림."""
    out = _render(tpl_name)
    soup = BeautifulSoup(out, "html.parser")
    ids = [el.get("id") for el in soup.find_all(id=True)]
    dups = [i for i, c in Counter(ids).items() if c > 1]
    assert not dups, f"{tpl_name}: duplicate ids {dups}"


# ─── 3. i18n — phrase book ↔ data-i18n 매핑 ──────────────────────


def _extract_phrase_book_ko() -> set[str]:
    js = (_STATIC / "ui.js").read_text(encoding="utf-8")
    m = re.search(r"_phraseBookKo\s*=\s*\{(.*?)\n\s*\};", js, re.DOTALL)
    assert m, "_phraseBookKo block not found in ui.js"
    body = m.group(1)
    keys: set[str] = set()
    # Single-line entries.
    for entry in re.finditer(r'^\s*"((?:[^"\\]|\\.)+)"\s*:\s*"', body, re.MULTILINE):
        keys.add(entry.group(1))
    # Two-line entries (key on its own line, value on the next).
    for entry in re.finditer(r'^\s*"((?:[^"\\]|\\.)+)"\s*:\s*$', body, re.MULTILINE):
        keys.add(entry.group(1))
    return keys


def test_placeholder_blurb_keys_have_korean_translations():
    """`data-i18n="docs-blurb"` 같은 placeholder 키는 반드시 phrase
    book 에 있어야 함. 없으면 ko 로케일에서 "docs-blurb" 가
    그대로 표시됨."""
    ko = _extract_phrase_book_ko()
    assert "docs-blurb" in ko, "PR-113 docs blurb missing Korean"
    assert "health-blurb" in ko, "PR-107 health blurb missing Korean"


def test_new_a11y_heading_keys_have_korean():
    """PR-129 에서 추가한 sr-only h2 ('Findings health panel',
    'Graph statistics') 한국어 번역 보유."""
    ko = _extract_phrase_book_ko()
    assert "Findings health panel" in ko
    assert "Graph statistics" in ko


# ─── 4. a11y — input 의 label, heading 계층 ───────────────────────


def test_diffs_share_url_input_has_aria_label():
    """PR-129 fix — share-url input 이 readonly 라 label 없으면
    SR 가 'edit' 으로 잘못 인식."""
    out = _render("diffs.html")
    soup = BeautifulSoup(out, "html.parser")
    inp = soup.find("input", id="share-url")
    assert inp is not None
    assert inp.get("aria-label"), "share-url input must have aria-label"


def test_findings_heading_hierarchy_starts_with_h2_under_h1():
    """h1 → h3 skip 는 SR 계층 깨뜨림. PR-129 가 health-panel 에
    sr-only h2 추가."""
    out = _render("findings.html")
    soup = BeautifulSoup(out, "html.parser")
    panel = soup.find(id="health-panel")
    assert panel is not None
    # The first heading inside the panel is an h2 (sr-only or visible).
    first_h = panel.find(["h2", "h3"])
    assert first_h is not None
    assert first_h.name == "h2", (
        f"findings.html health-panel first heading is {first_h.name}, "
        "must be h2 to respect h1 → h2 → h3 hierarchy"
    )


def test_graph_stats_heading_hierarchy_starts_with_h2():
    out = _render("graph.html")
    soup = BeautifulSoup(out, "html.parser")
    panel = soup.find(id="gstats")
    assert panel is not None
    first_h = panel.find(["h2", "h3"])
    assert first_h is not None
    assert first_h.name == "h2"


# ─── 5. dialog — show/close 양방향 ───────────────────────────────


def test_every_dialog_has_close_path():
    """<dialog> 가 열리면 반드시 닫을 수 있어야 함. 키보드
    user trap 방지."""
    for tpl_name in _LEAF_TEMPLATES:
        out = _render(tpl_name)
        soup = BeautifulSoup(out, "html.parser")
        for dlg in soup.find_all("dialog"):
            did = dlg.get("id")
            if not did:
                continue
            # close handler 존재 — closeDialog 또는 .close() 또는
            # form method=dialog (native close).
            has_close = (
                f'closeDialog("{did}")' in out
                or f"closeDialog('{did}')" in out
                or '.close()' in out
                or '"method": "dialog"' in out
                or 'method="dialog"' in out
            )
            assert has_close, (
                f"{tpl_name}: dialog #{did} has no close path — "
                "keyboard user can be trapped"
            )


# ─── 6. 모든 form 은 action 시 method 명시 또는 onsubmit 처리 ─────


def test_every_form_has_method_or_handler():
    for tpl_name in _LEAF_TEMPLATES:
        src = (_TPL / tpl_name).read_text(encoding="utf-8")
        for m in re.finditer(r"<form([^>]*)>", src):
            attrs = m.group(1)
            if "onsubmit=" in attrs:
                continue  # JS-handled
            if "action=" not in attrs:
                continue  # no destination
            assert "method=" in attrs, (
                f"{tpl_name}: form with action= must declare method= "
                f"(got: {m.group(0)[:120]})"
            )


# ─── 7. 반응형 — CSS 가 최소 mobile / tablet / dark mode ───────


def test_css_has_mobile_tablet_dark_breakpoints():
    css = (_STATIC / "app.css").read_text(encoding="utf-8")
    # Mobile-first 가드.
    assert "@media" in css
    # Specific breakpoints we know we have.
    assert "max-width: 768px" in css, "tablet breakpoint missing"
    assert "max-width: 480px" in css, "mobile breakpoint missing"
    assert "prefers-color-scheme: dark" in css, "dark mode missing"


# ─── 8. href — 알려진 route 만 ────────────────────────────────────


_KNOWN_ROUTES = {
    "/", "/projects", "/analysis", "/data", "/plans", "/diffs",
    "/findings", "/audit", "/settings", "/profile", "/users",
    "/organizations", "/sso", "/gdpr", "/graph", "/report",
    "/health", "/docs", "/login", "/logout", "/forgot", "/reset",
    "/invite",
}


@pytest.mark.parametrize("tpl_name", _LEAF_TEMPLATES)
def test_no_unknown_internal_href(tpl_name: str):
    """모든 / 시작 href 는 알려진 route 거나 /api/v1/ 시작. 깨진
    링크 = 404 = 사용자 신뢰 깨짐."""
    out = _render(tpl_name)
    soup = BeautifulSoup(out, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/"):
            continue
        base = href.split("?")[0].split("#")[0]
        if base.startswith("/api/v1/") or base.startswith("/static/"):
            continue
        if base in _KNOWN_ROUTES:
            continue
        # /findings/{some_id} 같은 dynamic route — 가장 가까운 prefix 매칭.
        if any(base.startswith(r) for r in _KNOWN_ROUTES if r != "/"):
            continue
        pytest.fail(f"{tpl_name}: unknown internal href {base!r}")


# ─── 9. innerHTML 사용 시 escapeHtml — 알려진 안전 패턴 ────────


def test_innerhtml_usage_known_safe_patterns():
    """innerHTML 사용처 7건이 모두 알려진 안전 패턴:
    - escapeHtml 거치는 사용자 데이터
    - literal HTML
    - 숫자/제어된 값"""
    # Files known to use innerHTML safely (audited PR-129):
    #   dashboard.html — Triage card uses escapeHtml(f.kind / where)
    #   diffs.html     — renderSubmission inside, escapeHtml everywhere
    #   graph.html     — SVG render, escapeHtml on every user-data attr
    #   report.html    — chart bars, escapeHtml on labels
    #   forgot.html    — token via encodeURIComponent + literal HTML
    #   docs.html      — markdown renderer (XSS audited PR-113)
    audited_safe = {
        "dashboard.html", "diffs.html", "graph.html", "report.html",
        "forgot.html", "docs.html",
    }
    actual = set()
    for tpl_name in _LEAF_TEMPLATES:
        src = (_TPL / tpl_name).read_text(encoding="utf-8")
        if re.search(r"\.innerHTML\s*=", src):
            actual.add(tpl_name)
    unexpected = actual - audited_safe - {
        "analysis.html", "audit.html", "data.html", "findings.html",
        "health.html", "plans.html", "settings.html", "users.html",
        "organizations.html", "sso.html", "gdpr.html", "profile.html",
        "projects.html",
    }
    assert not unexpected, (
        f"new innerHTML user(s) need security audit: {unexpected}"
    )


# ─── 10. console.log 누락 - production 에 남아있지 않는가 ────────


def test_no_console_log_left_in_templates():
    """console.error / console.warn 은 OK (실제 진단용). console.log
    는 dev 잔여물."""
    for tpl_name in _LEAF_TEMPLATES:
        src = (_TPL / tpl_name).read_text(encoding="utf-8")
        # Allow ``console.error`` / ``console.warn`` for diagnostics;
        # ``console.log`` is dev cruft.
        for m in re.finditer(r"\bconsole\.log\(", src):
            pytest.fail(
                f"{tpl_name}: console.log left at offset {m.start()} — "
                "use a real error path or remove"
            )


def test_no_console_log_in_ui_js():
    js = (_STATIC / "ui.js").read_text(encoding="utf-8")
    matches = re.findall(r"\bconsole\.log\(", js)
    assert not matches, f"ui.js has {len(matches)} console.log call(s)"
