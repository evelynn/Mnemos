"""PR-134 — ExcelJS-backed .xlsx export for analysis results.

사용자 요청: 분석 결과를 엑셀 형태로 출력할 때 exceljs
(github.com/exceljs/exceljs, MIT) 라이브러리 사용.

설계
====
- exceljs.min.js (~950 KB) 를 /static 에 호스팅 — CSP
  ``script-src 'self'`` 가 커버, CDN 불필요 (air-gapped OK).
- MnemosUI.exportXlsx(spec, filename) — CSV path 와 같은 데이터로
  스타일된 .xlsx (bold/frozen header, autofilter, 열 폭) 생성.
- 라이브러리는 **첫 export 클릭 시 lazy-load** — Excel 안 쓰는
  탭은 950 KB 를 내려받지 않음.
- CSV 와 동일한 formula-injection 방어 (= + - @ → '접두).

검증
====
1. 정적 자산 존재 + MIT + 합리적 크기
2. ui.js: exportXlsx + 게으른 로더 + injection 가드 + namespace export
3. findings.html: Export Excel 버튼 + 핸들러 + ExcelJS 호출
4. CSP self-host 허용 (CDN 의존 없음)
5. i18n 한국어 phrase
6. jinja 렌더
7. (node 가 있으면) **실제 ExcelJS 로 .xlsx 생성 + 재파싱** 행동 테스트
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_STATIC = _REPO / "server" / "app" / "dashboard" / "static"
_TEMPLATES = _REPO / "server" / "app" / "dashboard" / "templates"
_EXCELJS = _STATIC / "exceljs.min.js"
_UIJS = _STATIC / "ui.js"


# ─── 1. 정적 자산 ────────────────────────────────────────────────


def test_exceljs_asset_present_and_reasonable_size():
    assert _EXCELJS.is_file(), "exceljs.min.js missing from /static"
    size = _EXCELJS.stat().st_size
    # 압축된 ExcelJS 는 ~950 KB. 너무 작으면 다운로드 실패 stub.
    assert 500_000 < size < 2_000_000, f"unexpected exceljs size: {size}"


def test_exceljs_is_mit_licensed():
    head = _EXCELJS.read_text(encoding="utf-8", errors="ignore")[:5000]
    # ExcelJS 헤더 배너 + MIT 토큰 존재.
    assert "ExcelJS" in head
    blob = _EXCELJS.read_text(encoding="utf-8", errors="ignore")
    assert "MIT" in blob, "ExcelJS bundle should retain its MIT license string"


def test_exceljs_umd_exposes_browser_global():
    """브라우저에서 window.ExcelJS 로 노출되는 UMD 인지 확인 —
    lazy-loader 가 window.ExcelJS 를 기대함."""
    head = _EXCELJS.read_text(encoding="utf-8", errors="ignore")[:600]
    assert ").ExcelJS=" in head, "UMD must assign window.ExcelJS in browser"


# ─── 2. ui.js 통합 ──────────────────────────────────────────────


def test_ui_js_defines_export_xlsx():
    js = _UIJS.read_text(encoding="utf-8")
    assert "function exportXlsx(" in js


def test_ui_js_lazy_loads_exceljs_from_self():
    """CDN 아닌 /static 에서 로드 — CSP self + air-gapped 준수."""
    js = _UIJS.read_text(encoding="utf-8")
    assert "function _loadExcelJS(" in js
    assert '"/static/exceljs.min.js"' in js
    # 캐시 — 두 번째 클릭에 재다운로드 안 함.
    assert "_exceljsPromise" in js
    # http(s) CDN 직접 참조가 없어야 (self-host 원칙).
    assert "cdn.jsdelivr" not in js
    assert "unpkg.com/exceljs" not in js


def test_ui_js_xlsx_has_formula_injection_guard():
    js = _UIJS.read_text(encoding="utf-8")
    assert "function _xlsxSafe(" in js
    # = + - @ 로 시작하는 셀에 ' 접두.
    idx = js.find("function _xlsxSafe(")
    slab = js[idx:idx + 400]
    assert '"=+-@"' in slab
    assert "'" in slab


def test_ui_js_exports_xlsx_on_namespace():
    js = _UIJS.read_text(encoding="utf-8")
    assert "exportXlsx: exportXlsx," in js


def test_ui_js_styles_header_and_freezes():
    """frozen + autofilter + bold header — analyst-friendly output."""
    js = _UIJS.read_text(encoding="utf-8")
    idx = js.find("function exportXlsx(")
    slab = js[idx:idx + 2500]
    assert "frozen" in slab
    assert "autoFilter" in slab
    assert "bold: true" in slab


def test_ui_js_xlsx_failure_shows_toast_not_silent():
    js = _UIJS.read_text(encoding="utf-8")
    idx = js.find("function exportXlsx(")
    slab = js[idx:idx + 2800]
    assert "showToast" in slab
    assert "Excel export failed" in slab


# ─── 3. findings.html 통합 ──────────────────────────────────────


def test_findings_html_has_excel_button_and_handler():
    html = (_TEMPLATES / "findings.html").read_text(encoding="utf-8")
    assert 'onclick="exportFindingsXlsx()"' in html
    assert 'data-i18n="Export Excel"' in html
    assert "async function exportFindingsXlsx(" in html
    assert "MnemosUI.exportXlsx(" in html
    # CSV 버튼도 여전히 존재 (대체가 아니라 추가).
    assert 'onclick="exportFindings()"' in html


def test_findings_xlsx_includes_richer_columns_than_csv():
    """Excel export 가 risk_score / priority / remediation 같은
    풍부한 열을 포함 (CSV 보다 분석 친화적)."""
    html = (_TEMPLATES / "findings.html").read_text(encoding="utf-8")
    idx = html.find("async function exportFindingsXlsx(")
    slab = html[idx:idx + 2000]
    for col in ("risk_score", "priority", "remediation"):
        assert col in slab, f"xlsx export missing richer column: {col}"


def test_audit_html_has_excel_export():
    """감사 로그도 Excel 내보내기 — 컴플라이언스 리뷰어 친화."""
    html = (_TEMPLATES / "audit.html").read_text(encoding="utf-8")
    assert 'onclick="exportAuditXlsx()"' in html
    assert "async function exportAuditXlsx(" in html
    assert "MnemosUI.exportXlsx(" in html
    assert 'name: "Audit Log"' in html


def test_audit_template_renders():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    out = env.get_template("audit.html").render(
        user={"id": 1, "username": "op", "role": "admin"},
        active_tab="audit", request=None,
    )
    assert "exportAuditXlsx" in out


# ─── 4. CSP self-host ───────────────────────────────────────────


def test_csp_allows_self_hosted_script():
    headers = (_REPO / "server" / "app" / "security" / "headers.py").read_text(
        encoding="utf-8"
    )
    idx = headers.find("_DEFAULT_CSP")
    slab = headers[idx:idx + 400]
    # script-src 가 'self' 포함 → /static/exceljs.min.js 허용.
    assert "script-src 'self'" in slab


# ─── 5. i18n ────────────────────────────────────────────────────


def test_korean_phrases_present():
    js = _UIJS.read_text(encoding="utf-8")
    assert '"Export Excel": "Excel 내보내기"' in js
    assert "Excel 내보내기 실패" in js


# ─── 6. jinja 렌더 ──────────────────────────────────────────────


def test_findings_template_renders():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    out = env.get_template("findings.html").render(
        user={"id": 1, "username": "op", "role": "operator"},
        active_tab="findings", request=None,
    )
    assert "exportFindingsXlsx" in out
    assert "Export Excel" in out


# ─── 7. 실제 ExcelJS 행동 테스트 (node) ─────────────────────────

_NODE = shutil.which("node")

_NODE_SCRIPT = textwrap.dedent("""
    const ExcelJS = require(process.argv[2]);
    function xlsxSafe(v){
      if (v===null||v===undefined) return "";
      let s = typeof v==="object"? JSON.stringify(v): v;
      if (typeof s==="string" && s.length && "=+-@".indexOf(s[0])!==-1) return "'"+s;
      return s;
    }
    (async () => {
      const rows = [
        { id:"f1", kind:"duplicate_endpoint", risk_score:88, subject:"c:POST /o" },
        { id:"=cmd|calc", kind:"+evil", risk_score:0, subject:"@SUM(A1)" },
      ];
      const columns = [
        {key:"id",header:"ID"},{key:"kind",header:"Kind"},
        {key:"risk_score",header:"Risk"},{key:"subject",header:"Subject"},
      ];
      const wb = new ExcelJS.Workbook();
      const ws = wb.addWorksheet("Findings");
      ws.columns = columns.map(c => ({header:c.header, key:c.key, width:20}));
      rows.forEach(r => { const o={}; columns.forEach(c=>o[c.key]=xlsxSafe(r[c.key])); ws.addRow(o); });
      const hr = ws.getRow(1);
      hr.font = {bold:true, color:{argb:"FFFFFFFF"}};
      hr.fill = {type:"pattern",pattern:"solid",fgColor:{argb:"FF1F2933"}};
      ws.views=[{state:"frozen",ySplit:1}];
      ws.autoFilter={from:{row:1,column:1},to:{row:1,column:columns.length}};
      const buf = await wb.xlsx.writeBuffer();
      const u8 = new Uint8Array(buf);
      const wb2 = new ExcelJS.Workbook();
      await wb2.xlsx.load(buf);
      const ws2 = wb2.getWorksheet("Findings");
      const out = {
        zip_magic: u8[0]===0x50 && u8[1]===0x4B && u8[2]===0x03 && u8[3]===0x04,
        row_count: ws2.rowCount,
        col_count: ws2.columnCount,
        header_a1: ws2.getRow(1).getCell(1).value,
        header_bold: !!(ws2.getRow(1).font && ws2.getRow(1).font.bold),
        injection_safe: String(ws2.getRow(3).getCell(1).value).startsWith("'="),
      };
      process.stdout.write(JSON.stringify(out));
    })();
""")


requires_node = pytest.mark.skipif(
    _NODE is None or not _EXCELJS.is_file(),
    reason="node missing or exceljs.min.js absent",
)


@requires_node
def test_real_exceljs_produces_valid_xlsx(tmp_path):
    """실제 ExcelJS 로 우리 export 변환을 그대로 실행 → .xlsx 가
    유효 zip 이고, 재파싱 시 header/rows/injection-guard 보존."""
    script = tmp_path / "gen.js"
    script.write_text(_NODE_SCRIPT, encoding="utf-8")
    cp = subprocess.run(
        [_NODE, str(script), str(_EXCELJS)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, f"node gen failed: {cp.stderr[:500]}"
    out = json.loads(cp.stdout)
    assert out["zip_magic"] is True, "output is not a valid .xlsx (zip) file"
    assert out["row_count"] == 3, f"expected 1 header + 2 data rows, got {out['row_count']}"
    assert out["col_count"] == 4
    assert out["header_a1"] == "ID"
    assert out["header_bold"] is True, "header row must be bold"
    assert out["injection_safe"] is True, (
        "formula-injection cell (=cmd|calc) must be prefixed with '"
    )
