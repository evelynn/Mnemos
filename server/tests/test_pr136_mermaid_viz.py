"""PR-136 — Mermaid-backed graphs/sequence/charts library.

사용자 요청: 질문 응답에 그래프/순서도/차트가 충분한지 확인 후
필요한 라이브러리를 추가해 설치 가능하게.

설계
====
- mermaid.min.js (~3.2 MB) 를 ``/static`` 에 자체 호스팅 — PR-130
  CSP ``script-src 'self'`` 가 그대로 커버, CDN 의존성 0.
- ``MnemosUI.renderMermaid(host, code)`` — flowchart / sequence /
  state / gantt / pie / ER / journey 등을 한 헬퍼로 렌더.
- 라이브러리는 **첫 호출 시 lazy-load** — Mermaid 안 쓰는 탭은
  3.2 MB 를 내려받지 않음 (ExcelJS PR-134 패턴 그대로).
- ``securityLevel: "strict"`` — 라벨에 인라인 HTML 주입 차단.
- 다이어그램 첫 사용처: report.html 의 "Finding lifecycle" 6-state
  state diagram. 데이터 0건이어도 PM/lead 가 워크플로 한눈에 파악.

검증
====
1. 정적 자산 존재 + MIT + 합리적 크기
2. ui.js: renderMermaid + lazy loader + strict security level +
   namespace export
3. report.html: Finding lifecycle 섹션 + 렌더 호출 + Mermaid syntax
4. CSP self-host 허용 (CDN 의존 없음)
5. jinja 렌더 성공
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_STATIC = _REPO / "server" / "app" / "dashboard" / "static"
_TEMPLATES = _REPO / "server" / "app" / "dashboard" / "templates"
_MERMAID = _STATIC / "mermaid.min.js"
_UIJS = _STATIC / "ui.js"


# ─── 1. 정적 자산 ────────────────────────────────────────────────


def test_mermaid_asset_present_and_reasonable_size():
    assert _MERMAID.is_file(), "mermaid.min.js missing from /static"
    size = _MERMAID.stat().st_size
    # 압축된 Mermaid 11 UMD 는 ~3.2 MB. 너무 작으면 다운로드 실패 stub.
    assert 2_000_000 < size < 5_000_000, f"unexpected mermaid size: {size}"


def test_mermaid_is_mit_licensed():
    blob = _MERMAID.read_text(encoding="utf-8", errors="ignore")
    # Mermaid 번들은 MIT 라이선스 + d3 + dagre 의존성 모두 MIT.
    assert "MIT" in blob, "mermaid bundle should retain a MIT license string"
    # 글로벌 노출 라인 존재 — 로더가 의존.
    assert 'globalThis["mermaid"]' in blob, (
        "mermaid bundle does not expose globalThis.mermaid"
    )


# ─── 2. ui.js loader + namespace ────────────────────────────────


def test_ui_js_exports_render_mermaid():
    src = _UIJS.read_text(encoding="utf-8")
    assert "renderMermaid: renderMermaid" in src, (
        "MnemosUI.renderMermaid not exported"
    )


def test_ui_js_lazy_loads_from_self_hosted_path():
    src = _UIJS.read_text(encoding="utf-8")
    # /static path — CSP self-host (PR-130) 와 일치, CDN 불필요.
    assert 's.src = "/static/mermaid.min.js"' in src, (
        "mermaid script tag should point at /static/mermaid.min.js"
    )
    # 동일 promise reuse — 중복 로드 방지.
    assert "_mermaidPromise" in src


def test_ui_js_initialises_with_strict_security_level():
    """``securityLevel: "strict"`` 가 라벨/노드 텍스트의 HTML 을
    sanitise. 빠지면 사용자 입력이 그대로 DOM 으로 들어가 XSS."""
    src = _UIJS.read_text(encoding="utf-8")
    assert 'securityLevel: "strict"' in src, (
        "mermaid.initialize must pass securityLevel: 'strict'"
    )
    # startOnLoad: false — 첫 호출 시점에만 렌더, auto-scan 안 함.
    assert "startOnLoad: false" in src


def test_ui_js_respects_dark_theme():
    """``data-theme=dark`` 가 켜져 있으면 Mermaid 도 dark theme 로
    초기화 — 다이어그램이 surface 와 대비되도록."""
    src = _UIJS.read_text(encoding="utf-8")
    assert 'getAttribute("data-theme") === "dark"' in src
    assert 'theme: theme' in src


# ─── 3. report.html — 첫 사용처 ─────────────────────────────────


def test_report_html_has_lifecycle_diagram_section():
    src = (_TEMPLATES / "report.html").read_text(encoding="utf-8")
    assert 'id="rp-lifecycle"' in src, (
        "report.html should host the Finding lifecycle diagram"
    )
    assert "Finding lifecycle" in src


def test_report_html_invokes_mermaid_renderer():
    src = (_TEMPLATES / "report.html").read_text(encoding="utf-8")
    assert "MnemosUI.renderMermaid(host" in src or \
        "MnemosUI.renderMermaid(host," in src, (
            "renderLifecycleDiagram must call MnemosUI.renderMermaid"
        )
    # Mermaid state diagram 헤더가 코드에 포함.
    assert "stateDiagram-v2" in src


def test_report_html_lifecycle_states_match_finding_model():
    """Lifecycle 6 state 가 findings 의 status enum 과 일치 —
    문서/UI 가 실제 transition 과 어긋나면 PM 의 사고 모형이 깨짐."""
    src = (_TEMPLATES / "report.html").read_text(encoding="utf-8")
    for state in ("new", "triaged", "in_progress", "resolved",
                  "closed", "suppressed"):
        assert state in src, f"lifecycle missing state: {state}"


# ─── 4. CSP / self-host 보장 ───────────────────────────────────


def test_no_cdn_references_for_mermaid():
    """``script-src 'self'`` CSP 위반 방지. 외부 CDN URL 발견 시
    air-gapped 배포가 깨지므로 즉시 실패."""
    for f in (_UIJS, _TEMPLATES / "report.html",
              _TEMPLATES / "plans.html", _TEMPLATES / "diffs.html"):
        src = f.read_text(encoding="utf-8")
        for bad in ("cdn.jsdelivr.net/npm/mermaid", "unpkg.com/mermaid",
                    "cdnjs.cloudflare.com/ajax/libs/mermaid"):
            assert bad not in src, f"{f.name} references CDN: {bad}"


# ─── 6. plans.html + diffs.html — 추가 사용처 (PR-136c) ─────────


def test_plans_html_has_lifecycle_diagram():
    src = (_TEMPLATES / "plans.html").read_text(encoding="utf-8")
    assert 'id="plan-lifecycle"' in src
    # Plan 의 실제 status: pending_approval → approved/rejected, regenerate loop.
    for state in ("pending_approval", "approved", "rejected"):
        assert state in src
    assert "MnemosUI.renderMermaid" in src
    # Lazy render — details toggle 에 바인딩.
    assert "toggle" in src and "renderPlanLifecycle" in src


def test_diffs_html_has_lifecycle_diagram_with_break_glass():
    src = (_TEMPLATES / "diffs.html").read_text(encoding="utf-8")
    assert 'id="diff-lifecycle"' in src
    # blocked → break-glass → approved 두-눈 흐름 표현.
    for state in ("submitted", "pending_approval", "blocked",
                  "grant_issued", "approved", "approved_no_mr",
                  "rejected"):
        assert state in src, f"diff lifecycle missing state: {state}"
    assert "different operator consumes token" in src
    assert "MnemosUI.renderMermaid" in src


def test_plans_diffs_templates_render():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    class _U:
        role = "operator"
    for name in ("plans.html", "diffs.html"):
        out = env.get_template(name).render(user=_U(), request=None,
                                            csrf_token="t")
        assert "lifecycle-diagram" in out, f"{name} missing lifecycle div"


# ─── 5. 템플릿 렌더 ─────────────────────────────────────────────


def test_report_template_still_renders():
    """문법 오류로 jinja parse 깨졌나 확인 — 변경량 적지만 안전망."""
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)),
                      autoescape=True)
    tpl = env.get_template("report.html")
    # _layout 의 ``{{ user.role }}`` 처럼 view 가 주입하는 컨텍스트는
    # 이 단위 테스트에서 의미가 없으므로 빈 객체 stub 으로 채움.
    class _U:
        role = "viewer"
    out = tpl.render(user=_U(), request=None, csrf_token="t")
    assert 'id="rp-lifecycle"' in out
    assert "stateDiagram-v2" in out
