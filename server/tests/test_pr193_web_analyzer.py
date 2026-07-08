"""PR-193 — ggoss-web: HTML/CSS analyzer + web+Java P2.

The web+Java eval found HTML/CSS could not be targeted at all. These tests
pin: HTML/CSS are now registered (targetable), HTML form/link routes become
HTTP contracts normalized to the SAME node a Java handler EXPOSES (so a
template ↔ Spring-handler cross-service link forms), Thymeleaf ``@{…}`` is
unwrapped, and assets/external URLs are skipped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_WEB = _ROOT / "analyzers" / "ggoss-web" / "src" / "ggoss_web.py"


_LIST_HTML = """\
<!DOCTYPE html>
<html xmlns:th="http://www.thymeleaf.org">
<head>
  <link rel="stylesheet" th:href="@{/resources/css/app.css}" />
  <link th:href="@{/webjars/bootstrap/css/bootstrap.css}" rel="stylesheet">
</head>
<body>
  <a th:href="@{/owners/new}">Add Owner</a>
  <a href="https://external.example.com/docs">External</a>
  <a href="#top">Top</a>
  <form th:action="@{/owners}" method="get"><input name="q"></form>
  <form th:action="@{/owners/new}" method="post"><input name="name"></form>
  <a th:href="@{__${owner.id}__/edit}">Edit</a>
</body>
</html>
"""

_STYLE_CSS = ".navbar { color: #333; }\n"


def _write(root: Path) -> None:
    t = root / "templates"
    t.mkdir(parents=True)
    (t / "ownersList.html").write_text(_LIST_HTML, encoding="utf-8")
    css = root / "static" / "css"
    css.mkdir(parents=True)
    (css / "app.css").write_text(_STYLE_CSS, encoding="utf-8")


def _run(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_WEB), verb, str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    return [json.loads(x) for x in cp.stdout.splitlines() if x.strip()]


def test_symbols_template_and_stylesheet(tmp_path: Path) -> None:
    _write(tmp_path)
    data = [r["data"] for r in _run("symbols", tmp_path)]
    kinds = {d["kind"] for d in data}
    assert "template" in kinds and "stylesheet" in kinds
    tpl = next(d for d in data if d["kind"] == "template")
    assert tpl["location"]["file"].endswith("ownersList.html")


def test_contracts_extract_app_routes_skip_assets(tmp_path: Path) -> None:
    _write(tmp_path)
    records = _run("contracts", tmp_path)
    contracts = [r["data"] for r in records if r["record_type"] == "contract"]
    specs = {(c["spec"]["method"], c["spec"]["path"]) for c in contracts}

    # Form actions + nav links become endpoints.
    assert ("GET", "/owners") in specs           # search form method=get
    assert ("POST", "/owners/new") in specs      # create form method=post
    assert ("GET", "/owners/new") in specs       # <a> link
    # Assets, external URLs, and anchors are NOT endpoints.
    paths = {p for _, p in specs}
    assert not any("resources" in p or "webjars" in p for p in paths)
    assert not any(p.startswith("http") or p.startswith("#") for p in paths)
    # http_endpoint + spec → platform normalizes like Java/TS (cross-service).
    assert all(c["kind"] == "http_endpoint" for c in contracts)


def test_template_calls_edge_links_to_contract(tmp_path: Path) -> None:
    _write(tmp_path)
    records = _run("contracts", tmp_path)
    edges = [r["data"] for r in records if r["record_type"] == "edge"]
    # The template CALLS the endpoint node a Java @PostMapping EXPOSES.
    calls = [e for e in edges if e["kind"] == "CALLS"
             and e["target_id"] == "http.POST./owners/new"]
    assert calls
    assert calls[0]["source_id"].startswith("web:")
    assert calls[0]["source_id"].endswith("ownersList.html@1")


def test_cross_service_node_matches_platform(tmp_path: Path) -> None:
    from app.merge.contract_id import http_contract_id

    _write(tmp_path)
    contracts = [
        r["data"] for r in _run("contracts", tmp_path)
        if r["record_type"] == "contract"
    ]
    ids = {http_contract_id(c["spec"]["method"], c["spec"]["path"])
           for c in contracts}
    # A Java @PostMapping("/owners/new") lands on the SAME node this template
    # form submits to → they link.
    assert http_contract_id("POST", "/owners/new") in ids


def test_registry_maps_html_css_scss() -> None:
    from app.analyzers.registry import binary_for
    from app.api.projects import SUPPORTED_LANGUAGES

    assert binary_for("html") == "ggoss-web"
    assert binary_for("css") == "ggoss-web"
    assert binary_for("scss") == "ggoss-web"
    # The whole point of P2: a web project can now be created at all.
    assert {"html", "css", "scss"} <= SUPPORTED_LANGUAGES
