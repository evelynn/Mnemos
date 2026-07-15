"""PR-193 — ggoss-web: HTML / CSS analyzer.

The web+Java eval (docs/04-eval/web-java-eval-2026-07-08.md) found that
HTML/CSS could not even be *targeted* — they were absent from
``SUPPORTED_LANGUAGES``, so a web project was rejected at creation. This
analyzer closes that (P2) with stdlib-only extraction, same protocol as
the other ggoss-* analyzers:

    ggoss-web probe|inventory|symbols|calls|contracts|data_access|schema <target>

The high-value web artifact for AI source analysis is NOT css selectors —
it is the *routes a template hits*. A Thymeleaf ``th:action="@{/owners}"``
form (method=post) and an ``<a th:href="@{/owners/new}">`` link reference
backend endpoints. Emitting those as HTTP contracts with ``kind=http_endpoint``
+ ``spec={method,path}`` makes the platform normalize them to the SAME
``http.<METHOD>.<path>`` node a Java ``@GetMapping`` / TS ``fetch`` resolves
to — so a template ↔ Spring-handler cross-service link forms automatically
(spec pillar 5), the same mechanism ggoss-java/ggoss-ts use.

Symbol kinds emitted:
- ``template``   — one per HTML file (the node a link/form CALLS from)
- ``stylesheet`` — one per CSS/SCSS file (targetable; low-value, no edges)

Edge kinds emitted:
- ``CALLS`` — template → an HTTP contract it navigates to / submits to.

Contracts: ``<form action|th:action method=…>`` and ``<a href|th:href>`` that
reference an application route (leading ``/`` or Thymeleaf ``@{…}``). Asset
links (css/js/images/webjars/resources), external URLs, and ``#`` anchors are
skipped — they are not endpoints.

Limitations (documented honesty):
- Regex/entity extraction, not a full HTML parser; malformed markup degrades.
- Dynamic Thymeleaf segments (``@{__${owner.id}__/edit}``) collapse to a
  ``{var}`` slot, so parameterized template links may not match a handler's
  own param name (static links link exactly).
- CSS is targetable but intentionally low-signal (one stylesheet symbol per
  file, no selector graph) — selectors are not a source-logic artifact.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

_SOURCE_NAME = "ggoss-web"
_SOURCE_VERSION = "1.0.0"

_HTML_EXTS = {".html", ".htm", ".xhtml"}
_CSS_EXTS = {".css", ".scss", ".sass", ".less"}
_ALL_EXTS = _HTML_EXTS | _CSS_EXTS
_SKIP_DIRS = {
    ".git", ".mnemos", "node_modules", "target", "build", "dist", "out",
    "bower_components", "vendor",
}


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


def _envelope(record_type: str, data: dict[str, Any]) -> str:
    return json.dumps({
        "record_type": record_type,
        "source_name": _SOURCE_NAME,
        "source_version": _SOURCE_VERSION,
        "analyzed_at": _iso_now(),
        "data": data,
    })


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat()


def _iter_files(root: Path, exts: set[str]) -> Iterator[Path]:
    if _is_link_or_junction(root):
        return
    if root.is_file():
        if root.suffix.lower() in exts:
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
            and not _is_link_or_junction(Path(dirpath) / d)
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() in exts and not _is_link_or_junction(path):
                yield path


def _rel(path: Path, target: Path) -> str:
    try:
        return path.resolve().relative_to(target.resolve()).as_posix()
    except ValueError:
        return path.name


def _component_id(target: Path) -> str:
    name = os.environ.get("MNEMOS_PROJECT_ID") or target.resolve().name or "web-project"
    return f"web.{name}"


def _template_id(rel: str) -> str:
    return f"web:{rel}@1"


# ─── route extraction ──────────────────────────────────────────────


_ASSET_RE = re.compile(
    r"\.(?:css|scss|less|js|mjs|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|map|webp)"
    r"(?:$|[?#])",
    re.I,
)
_SKIP_PREFIXES = ("http://", "https://", "//", "#", "mailto:", "tel:",
                  "javascript:", "data:")


def _thymeleaf_unwrap(raw: str) -> str | None:
    """``@{/owners/new}`` → ``/owners/new``; dynamic ``__${..}__`` / ``${..}``
    segments collapse to ``{var}``. Returns None if not an app route."""
    s = raw.strip()
    m = re.fullmatch(r"@\{(.*)\}", s)
    if m:
        s = m.group(1)
    # Strip Thymeleaf param list: @{/x(a=${b})} → /x
    s = re.sub(r"\([^)]*\)", "", s)
    # Dynamic segments → {var}
    s = re.sub(r"__\$\{[^}]*\}__", "{var}", s)
    s = re.sub(r"\$\{[^}]*\}", "{var}", s)
    s = s.strip()
    return s or None


def _is_app_route(path: str) -> bool:
    if not path:
        return False
    low = path.lower()
    if low.startswith(_SKIP_PREFIXES):
        return False
    if _ASSET_RE.search(low):
        return False
    if "/resources/" in low or "/webjars/" in low or low.startswith("/static/"):
        return False
    return path.startswith("/") or path.startswith("{")


_ATTR = r"""["']([^"']*)["']"""
_FORM_RE = re.compile(r"<form\b([^>]*)>", re.I | re.S)
_LINK_RE = re.compile(r"<a\b([^>]*)>", re.I | re.S)
_ACTION_RE = re.compile(rf"(?:th:action|action)\s*=\s*{_ATTR}", re.I)
_HREF_RE = re.compile(rf"(?:th:href|href)\s*=\s*{_ATTR}", re.I)
_METHOD_RE = re.compile(rf"method\s*=\s*{_ATTR}", re.I)


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _routes_in_html(src: str) -> list[tuple[str, str, int]]:
    """Return ``[(method, path, line), …]`` for form submits + nav links."""
    out: list[tuple[str, str, int]] = []
    for m in _FORM_RE.finditer(src):
        attrs = m.group(1)
        am = _ACTION_RE.search(attrs)
        if not am:
            continue  # form with no action submits to self URL — unresolvable
        route = _thymeleaf_unwrap(am.group(1))
        if not route or not _is_app_route(route):
            continue
        mm = _METHOD_RE.search(attrs)
        method = (mm.group(1).upper() if mm else "GET")
        out.append((method, route, _line_of(src, m.start())))
    for m in _LINK_RE.finditer(src):
        hm = _HREF_RE.search(m.group(1))
        if not hm:
            continue
        route = _thymeleaf_unwrap(hm.group(1))
        if not route or not _is_app_route(route):
            continue
        out.append(("GET", route, _line_of(src, m.start())))
    return out


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.stderr.write(json.dumps({
            "level": "error", "file": str(path),
            "message": f"read failed: {exc}", "recoverable": True,
        }) + "\n")
        return None


# ─── verbs ─────────────────────────────────────────────────────────


def cmd_probe(target: Path) -> dict[str, Any]:
    n = sum(1 for _ in _iter_files(target, _ALL_EXTS))
    return {"applicable": n > 0, "reason": f"{n} web files", "files_found": n}


def cmd_inventory(target: Path) -> dict[str, Any]:
    html = sum(1 for _ in _iter_files(target, _HTML_EXTS))
    css = sum(1 for _ in _iter_files(target, _CSS_EXTS))
    return {"language": "web", "html_files": html, "css_files": css}


def cmd_symbols(target: Path, out_stream) -> None:
    target = target.resolve()
    comp = _component_id(target)
    for path in _iter_files(target, _ALL_EXTS):
        rel = _rel(path, target)
        is_html = path.suffix.lower() in _HTML_EXTS
        out_stream.write(_envelope("symbol", {
            "id": _template_id(rel),
            "kind": "template" if is_html else "stylesheet",
            "name": path.name,
            "qual_name": rel,
            "component_id": comp,
            "signature": "",
            "location": {"file": rel, "line": 1, "col": 1},
            "visibility": "public",
            "is_entry_point": False,
            "metadata": {},
            "certainty": "asserted",
            "created_by": [_SOURCE_NAME],
        }) + "\n")


def cmd_contracts(target: Path, out_stream) -> None:
    target = target.resolve()
    comp = _component_id(target)
    for path in _iter_files(target, _HTML_EXTS):
        src = _read(path)
        if src is None:
            continue
        rel = _rel(path, target)
        tid = _template_id(rel)
        seen: set[str] = set()
        for method, route, line in _routes_in_html(src):
            cid = f"http.{method}.{route}"
            if cid not in seen:
                seen.add(cid)
                out_stream.write(_envelope("contract", {
                    "id": cid,
                    "kind": "http_endpoint",
                    "name": f"{method} {route}",
                    "spec": {"method": method, "path": route},
                    "component_id": comp,
                    "location": {"file": rel, "line": line, "col": 1},
                    "created_by": [_SOURCE_NAME],
                    "certainty": "asserted",
                }) + "\n")
            # The template CALLS (navigates to / submits to) the endpoint —
            # the client side of the contract, mirroring ggoss-ts fetch.
            out_stream.write(_envelope("edge", {
                "source_id": tid,
                "target_id": cid,
                "kind": "CALLS",
                "certainty": "asserted",
                "created_by": [_SOURCE_NAME],
                "metadata": {"via": "template_link", "line": line},
            }) + "\n")


def cmd_calls(target: Path, out_stream) -> None:
    # Template→endpoint edges are emitted with the contracts (they need the
    # contract node); nothing extra to stream here.
    del target, out_stream


def cmd_data_access(target: Path, out_stream) -> None:
    del target, out_stream


def cmd_schema() -> dict[str, Any]:
    return {
        "language": "web",
        "version": _SOURCE_VERSION,
        "symbol_kinds": ["template", "stylesheet"],
        "edge_kinds": ["CALLS"],
        "record_types": ["symbol", "contract", "edge"],
    }


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(
            "usage: ggoss-web "
            "<probe|inventory|symbols|calls|contracts|data_access|schema> "
            "<target>\n"
        )
        return 2
    verb = argv[0]
    rest = argv[1:]
    target_str = next((a for a in rest if not a.startswith("-")), None)
    if verb in {
        "probe", "inventory", "symbols", "calls", "contracts", "data_access",
    } and target_str is None:
        sys.stderr.write(f"missing target arg for verb {verb!r}\n")
        return 2
    target = Path(target_str) if target_str else Path(".")

    if verb == "probe":
        print(json.dumps(cmd_probe(target)))
        return 0
    if verb == "inventory":
        print(json.dumps(cmd_inventory(target)))
        return 0
    if verb == "symbols":
        cmd_symbols(target, sys.stdout)
        return 0
    if verb == "calls":
        cmd_calls(target, sys.stdout)
        return 0
    if verb == "contracts":
        cmd_contracts(target, sys.stdout)
        return 0
    if verb == "data_access":
        cmd_data_access(target, sys.stdout)
        return 0
    if verb == "schema":
        print(json.dumps(cmd_schema()))
        return 0
    sys.stderr.write(f"unknown verb: {verb!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
