"""PR-194 — ggoss-kotlin: Kotlin analyzer.

Modern Spring Boot is frequently Kotlin, so the web+Java roadmap's P3 adds
deterministic Kotlin extraction. A *separate* analyzer (not a ggoss-java
extension) keeps it isolated. stdlib-only, same protocol:

    ggoss-kotlin probe|inventory|symbols|calls|contracts|data_access|schema <target>

Kotlin's ``fun`` keyword makes function detection more reliable than the
Java brace heuristic — a function is literally ``fun name(...)`` (top-level
or in a type). Types are ``class`` / ``interface`` / ``object`` (incl.
``data``/``sealed``/``enum``/``annotation`` modifiers). Enclosing type (for
fully-qualified ids) is resolved by body-span containment.

Spring annotations are identical to Java (``@RestController``/``@GetMapping``
…), so contracts are emitted with ``kind=http_endpoint`` + ``spec`` and
normalize to the SAME ``http.<METHOD>.<path>`` node a Java handler / TS
fetch / HTML form resolves to (cross-service linking, spec pillar 5).

Symbol kinds: ``class`` / ``interface`` / ``object`` / ``enum`` /
``annotation_class`` / ``function``.
Edge kinds: ``CALLS`` (same-file → unique project-wide → ``kotlin:extern:``),
``EXPOSES`` (Spring/JAX-RS mapped function), ``READS``/``WRITES`` (SQL).

Limitations (documented honesty):
- Expression-body functions (``fun f() = expr``) are symbols but contribute
  no CALLS (no brace body to scan).
- Call resolution is name-based (no type/FQN resolution); extension-function
  receivers and member calls through properties are not resolved.
- Primary-constructor properties (``class Foo(val x)``) are not extracted as
  separate symbols.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_SOURCE_NAME = "ggoss-kotlin"
_SOURCE_VERSION = "1.0.0"

_EXTS = {".kt", ".kts"}
_SKIP_DIRS = {
    ".git", ".mnemos", ".idea", ".gradle", "build", "out", "target",
    "node_modules",
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


def _iter_files(root: Path) -> Iterator[Path]:
    if _is_link_or_junction(root):
        return
    if root.is_file() and root.suffix.lower() in _EXTS:
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
            if path.suffix.lower() in _EXTS and not _is_link_or_junction(path):
                yield path


def _rel(path: Path, target: Path) -> str:
    try:
        return path.resolve().relative_to(target.resolve()).as_posix()
    except ValueError:
        return path.name


def _component_id(target: Path) -> str:
    name = os.environ.get("MNEMOS_PROJECT_ID") or target.resolve().name or "kotlin-project"
    return f"kotlin.{name}"


def _symbol_id(rel: str, qual: str, line: int) -> str:
    return f"kotlin:{rel}:{qual}@{line}"


# ─── comment / string blanking (offset-preserving) ─────────────────


_STRIP_RE = re.compile(
    r'//[^\n]*'
    r'|/\*.*?\*/'
    r'|"""(?:.*?)"""'
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'",
    re.S,
)


def _blank(m: re.Match[str]) -> str:
    return "".join(c if c == "\n" else " " for c in m.group(0))


def _strip(src: str) -> str:
    return _STRIP_RE.sub(_blank, src)


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _match_brace(text: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _match_paren(text: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


# ─── declarations ──────────────────────────────────────────────────


_TYPE_RE = re.compile(
    r"\b(?:data\s+|sealed\s+|open\s+|abstract\s+|inner\s+|value\s+|"
    r"enum\s+|annotation\s+)*(class|interface|object)\s+([A-Za-z_]\w*)"
)
_FUN_RE = re.compile(
    r"\bfun\b\s*(?:<[^>]*>\s*)?(?:[A-Za-z_][\w.]*\.)?([A-Za-z_]\w*)\s*\("
)
_ANNOT_RE = re.compile(r"@([A-Za-z_]\w*)\s*(\([^()]*(?:\([^()]*\)[^()]*)*\))?")
_CALL_RE = re.compile(r"(?:\.\s*)?([A-Za-z_]\w*)\s*\(")
_CTRL = frozenset({
    "if", "for", "while", "when", "return", "throw", "catch", "is", "as",
    "in", "out", "by", "super", "this", "else", "do", "try", "fun", "val",
    "var", "class", "object", "interface",
})


@dataclass
class _Sym:
    id: str
    kind: str
    name: str
    qual: str
    line: int
    header_src: str = ""
    body_start: int = -1   # index of body '{' (brace-body funcs); -1 otherwise
    body_end: int = -1
    is_type: bool = False
    span: tuple[int, int] = (-1, -1)  # type body span (types only)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FileScan:
    rel: str
    package: str
    symbols: list[_Sym]
    text: str = ""
    src: str = ""


_ENUM_TYPE_KIND = {"class": "class", "interface": "interface", "object": "object"}


def _scan_file(src: str, rel: str) -> _FileScan:
    text = _strip(src)
    symbols: list[_Sym] = []

    pkg = ""
    mp = re.search(r"^\s*package\s+([\w.]+)", text, re.M)
    if mp:
        pkg = mp.group(1)

    # 1) Types with body spans.
    types: list[_Sym] = []
    for m in _TYPE_RE.finditer(text):
        kw, name = m.group(1), m.group(2)
        kind = _ENUM_TYPE_KIND[kw]
        if re.search(r"\benum\s+class\b", text[max(0, m.start() - 12):m.end()]):
            kind = "enum"
        elif re.search(r"\bannotation\s+class\b",
                       text[max(0, m.start() - 18):m.end()]):
            kind = "annotation_class"
        brace = text.find("{", m.end())
        # A type may have no body (``class Foo(val x: Int)`` one-liner).
        line = _line_of(text, m.start(2))
        sym = _Sym(
            id="", kind=kind, name=name, qual=name, line=line, is_type=True,
        )
        if brace != -1:
            close = _match_brace(text, brace)
            sym.span = (brace, close if close != -1 else len(text))
        types.append(sym)

    def enclosing_chain(pos: int) -> list[_Sym]:
        containing = [t for t in types if t.span[0] <= pos <= t.span[1]
                      and t.span[0] != -1]
        containing.sort(key=lambda t: t.span[1] - t.span[0])  # inner first
        return list(reversed(containing))  # outer → inner

    # Assign type ids/qual now that we can nest them. A type's outer chain is
    # found from its body-open position (an outer type's span contains it).
    for t in types:
        pos = t.span[0]
        outer = [c.name for c in enclosing_chain(pos) if c is not t] if pos != -1 else []
        t.qual = ".".join(outer + [t.name])
        t.id = _symbol_id(rel, t.qual, t.line)
        symbols.append(t)

    # 2) Functions.
    for m in _FUN_RE.finditer(text):
        name = m.group(1)
        open_paren = text.index("(", m.start())
        close_paren = _match_paren(text, open_paren)
        if close_paren < 0:
            continue
        line = _line_of(text, m.start(1))
        chain = enclosing_chain(m.start())
        qual = ".".join([c.name for c in chain] + [name])
        sym = _Sym(
            id=_symbol_id(rel, qual, line), kind="function", name=name,
            qual=qual, line=line,
        )
        # Brace body?  ``) : Ret {``  vs expression body ``) = …``.
        tail = text[close_paren + 1:]
        bm = re.match(r"\s*(?::[^={]+)?\s*\{", tail)
        if bm:
            body_open = close_paren + 1 + tail.index("{")
            body_close = _match_brace(text, body_open)
            if body_close != -1:
                sym.body_start = body_open
                sym.body_end = body_close
        symbols.append(sym)

    src_lines = src.split("\n")
    for s in symbols:
        if 1 <= s.line <= len(src_lines):
            s.metadata.setdefault("signature", src_lines[s.line - 1].strip()[:400])
        # Kotlin annotations sit on their own line(s) above the declaration;
        # collect the contiguous ``@…`` block + the decl line for contracts.
        s.header_src = _header_annots(src_lines, s.line)

    return _FileScan(rel=rel, package=pkg, symbols=symbols, text=text, src=src)


def _header_annots(lines: list[str], line_1: int) -> str:
    li = line_1 - 1
    if not (0 <= li < len(lines)):
        return ""
    start = li
    j = li - 1
    while j >= 0 and lines[j].lstrip().startswith("@"):
        start = j
        j -= 1
    return "\n".join(lines[start:li + 1])[:800]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.stderr.write(json.dumps({
            "level": "error", "file": str(path),
            "message": f"read failed: {exc}", "recoverable": True,
        }) + "\n")
        return None


def _scan_all(target: Path) -> list[tuple[Path, str, _FileScan]]:
    out = []
    for path in _iter_files(target):
        src = _read(path)
        if src is None:
            continue
        out.append((path, src, _scan_file(src, _rel(path, target))))
    return out


# ─── verbs ─────────────────────────────────────────────────────────


def cmd_probe(target: Path) -> dict[str, Any]:
    n = sum(1 for _ in _iter_files(target))
    return {"applicable": n > 0, "reason": f"{n} Kotlin files", "files_found": n}


def cmd_inventory(target: Path) -> dict[str, Any]:
    files = list(_iter_files(target))
    total = 0
    for f in files:
        try:
            total += sum(1 for _ in f.open("r", encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return {"language": "kotlin", "files": len(files), "total_lines": total}


def cmd_symbols(target: Path, out_stream) -> None:
    target = target.resolve()
    comp = _component_id(target)
    for _p, _s, scan in _scan_all(target):
        for s in scan.symbols:
            meta = {k: v for k, v in s.metadata.items() if k != "signature"}
            if scan.package:
                meta["package"] = scan.package
            out_stream.write(_envelope("symbol", {
                "id": s.id,
                "kind": s.kind,
                "name": s.name,
                "qual_name": (f"{scan.package}.{s.qual}" if scan.package else s.qual),
                "component_id": comp,
                "signature": s.metadata.get("signature", ""),
                "location": {"file": scan.rel, "line": s.line, "col": 1},
                "visibility": "public",
                "is_entry_point": s.name == "main",
                "metadata": meta,
                "certainty": "asserted",
                "created_by": [_SOURCE_NAME],
            }) + "\n")


def cmd_calls(target: Path, out_stream) -> None:
    target = target.resolve()
    scans = _scan_all(target)
    global_fn: dict[str, list[str]] = {}
    for _p, _s, scan in scans:
        for s in scan.symbols:
            if s.kind == "function":
                global_fn.setdefault(s.name, []).append(s.id)
    for _p, _s, scan in scans:
        local = {}
        for s in scan.symbols:
            if s.kind == "function":
                local.setdefault(s.name, s.id)
        for s in scan.symbols:
            if s.kind != "function" or s.body_start < 0:
                continue
            body = scan.text[s.body_start:s.body_end]
            seen: set[tuple[str, str]] = set()
            for m in _CALL_RE.finditer(body):
                callee = m.group(1)
                if callee in _CTRL:
                    continue
                resolved = local.get(callee)
                if resolved is None:
                    ids = global_fn.get(callee, [])
                    resolved = ids[0] if len(ids) == 1 else None
                target_id = resolved or f"kotlin:extern:{callee}"
                key = (s.id, target_id)
                if key in seen:
                    continue
                seen.add(key)
                line = _line_of(scan.text, s.body_start + m.start(1))
                out_stream.write(_envelope("edge", {
                    "source_id": s.id, "target_id": target_id, "kind": "CALLS",
                    "certainty": "asserted" if resolved else "inferred",
                    "created_by": [_SOURCE_NAME],
                    "metadata": {"invocation_site": {"line": line, "col": 1},
                                 "callee_resolved": resolved is not None},
                }) + "\n")


# ── contracts (Spring / JAX-RS — identical annotations to Java) ─────

_SPRING_VERB = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}
_JAXRS_VERB = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


def _annot_string_arg(args: str | None) -> str | None:
    if not args:
        return None
    m = re.search(r'\b(?:value|path)\s*=\s*"((?:\\.|[^"\\])*)"', args)
    if m:
        return m.group(1)
    m = re.search(r'"((?:\\.|[^"\\])*)"', args)
    return m.group(1) if m else None


def _request_mapping_method(args: str | None) -> str | None:
    if not args:
        return None
    m = re.search(r"method\s*=\s*(?:\[)?\s*RequestMethod\.([A-Z]+)", args)
    return m.group(1) if m else None


def _class_prefix(header: str) -> str:
    for m in _ANNOT_RE.finditer(header):
        if m.group(1) == "RequestMapping" or m.group(1) in _SPRING_VERB:
            p = _annot_string_arg(m.group(2))
            if p:
                return p
    for m in _ANNOT_RE.finditer(header):
        if m.group(1) == "Path":
            p = _annot_string_arg(m.group(2))
            if p:
                return p
    return ""


def _join(prefix: str, route: str) -> str:
    parts = [p for p in (prefix or "", route or "") if p and p != "/"]
    joined = "/".join(s.strip("/") for s in parts)
    return "/" + joined if joined else "/"


def cmd_contracts(target: Path, out_stream) -> None:
    target = target.resolve()
    comp = _component_id(target)
    for _p, _s, scan in _scan_all(target):
        headers = {s.qual: s.header_src for s in scan.symbols if s.is_type}
        for s in scan.symbols:
            if s.kind != "function":
                continue
            type_qual = s.qual.rsplit(".", 1)[0] if "." in s.qual else ""
            prefix = _class_prefix(headers.get(type_qual, ""))
            for m in _ANNOT_RE.finditer(s.header_src):
                name, args = m.group(1), m.group(2)
                route = _annot_string_arg(args) or ""
                if name in _SPRING_VERB:
                    methods = [_SPRING_VERB[name]]
                elif name == "RequestMapping":
                    rm = _request_mapping_method(args)
                    methods = [rm] if rm else ["GET"]
                elif name in _JAXRS_VERB:
                    methods = [name]
                    pm = re.search(r'@Path\s*\(\s*"((?:\\.|[^"\\])*)"', s.header_src)
                    route = pm.group(1) if pm else ""
                else:
                    continue
                full = _join(prefix, route)
                for verb in methods:
                    cid = f"http.{verb}.{full}"
                    out_stream.write(_envelope("contract", {
                        "id": cid, "kind": "http_endpoint",
                        "name": f"{verb} {full}",
                        "spec": {"method": verb, "path": full},
                        "component_id": comp,
                        "location": {"file": scan.rel, "line": s.line, "col": 1},
                        "created_by": [_SOURCE_NAME], "certainty": "asserted",
                    }) + "\n")
                    out_stream.write(_envelope("edge", {
                        "source_id": s.id, "target_id": cid, "kind": "EXPOSES",
                        "certainty": "asserted", "created_by": [_SOURCE_NAME],
                        "metadata": {"framework": "spring_or_jaxrs", "line": s.line},
                    }) + "\n")


# ── data_access (JPA entity + SQL literals) ────────────────────────

_SQL_VERB_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.I)
_SQL_TABLE_RE = {
    "SELECT": re.compile(r"\bFROM\s+([A-Za-z_]\w*)", re.I),
    "INSERT": re.compile(r"\bINTO\s+([A-Za-z_]\w*)", re.I),
    "UPDATE": re.compile(r"\bUPDATE\s+([A-Za-z_]\w*)", re.I),
    "DELETE": re.compile(r"\bFROM\s+([A-Za-z_]\w*)", re.I),
}
_VERB_KIND = {"SELECT": "READS", "INSERT": "WRITES",
              "UPDATE": "WRITES", "DELETE": "WRITES"}
_STR_RE = re.compile(r'"((?:\\.|[^"\\\n])*)"')
_ENTITY_RE = re.compile(r"@(?:Entity|Table)\b")
_TABLE_NAME_RE = re.compile(r'@Table\s*\([^)]*\bname\s*=\s*"([^"]+)"')


def _sql_targets(sql: str) -> list[tuple[str, str]]:
    out = []
    for m in _SQL_VERB_RE.finditer(sql):
        verb = m.group(1).upper()
        tm = _SQL_TABLE_RE[verb].search(sql[m.end():])
        if tm:
            out.append((_VERB_KIND[verb], tm.group(1)))
    return out


def cmd_data_access(target: Path, out_stream) -> None:
    target = target.resolve()
    comp = _component_id(target)
    for _p, src, scan in _scan_all(target):
        emitted: set[str] = set()
        for s in scan.symbols:
            if not s.is_type:
                continue
            if not _ENTITY_RE.search(s.header_src):
                continue
            tm = _TABLE_NAME_RE.search(s.header_src)
            table = (tm.group(1) if tm else s.name).lower()
            ent = f"data:{table}"
            if ent not in emitted:
                emitted.add(ent)
                out_stream.write(_envelope("data_entity", {
                    "id": ent, "logical_name": table, "component_id": comp,
                    "schema": None, "created_by": [_SOURCE_NAME],
                    "certainty": "inferred",
                }) + "\n")
        for s in scan.symbols:
            if s.kind != "function" or s.body_start < 0:
                continue
            for lm in _STR_RE.finditer(src[s.body_start:s.body_end]):
                for kind, table in _sql_targets(lm.group(1)):
                    ent = f"data:{table.lower()}"
                    if ent not in emitted:
                        emitted.add(ent)
                        out_stream.write(_envelope("data_entity", {
                            "id": ent, "logical_name": table.lower(),
                            "component_id": comp, "schema": None,
                            "created_by": [_SOURCE_NAME], "certainty": "inferred",
                        }) + "\n")
                    out_stream.write(_envelope("edge", {
                        "source_id": s.id, "target_id": ent, "kind": kind,
                        "certainty": "inferred", "created_by": [_SOURCE_NAME],
                        "metadata": {"via": "sql_literal", "line": s.line},
                    }) + "\n")


def cmd_schema() -> dict[str, Any]:
    return {
        "language": "kotlin", "version": _SOURCE_VERSION,
        "symbol_kinds": ["class", "interface", "object", "enum",
                         "annotation_class", "function"],
        "edge_kinds": ["CALLS", "EXPOSES", "READS", "WRITES"],
        "record_types": ["symbol", "edge", "contract", "data_entity"],
    }


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(
            "usage: ggoss-kotlin "
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
