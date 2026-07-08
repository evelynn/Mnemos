"""PR-195 — ggoss-treesitter: config-driven multi-language analyzer.

The web+Java comparison against codebase-memory-mcp showed our biggest gap
was *language extensibility*: cbm ships 160 tree-sitter grammars; Mnemos
hand-writes one regex analyzer per language. This absorbs cbm's extensibility
*mechanism* natively (T2 of the absorption review): a single analyzer backed
by ``tree-sitter-language-pack`` (100+ precompiled grammars) that extracts
symbols + CALLS for any configured language. Adding a language = adding a
small ``_LANG`` config entry, not a new analyzer.

Unlike the pure-stdlib ggoss-{py,cpp,java,kotlin,web}, this one has ONE
dependency (``tree-sitter`` + ``tree-sitter-language-pack``). It degrades
gracefully — if the packages aren't importable, every verb reports the
language as unsupported so the orchestrator records a clean skip (never a
crash). The stdlib analyzers stay dependency-free; this is the opt-in path
to breadth.

    ggoss-treesitter probe|inventory|symbols|calls|contracts|data_access|schema <path>

PoC languages (all currently LLM-inferred-only in Mnemos — this makes them
deterministic): Go, Rust, Ruby. The walk is generic; the per-language config
declares which AST node types are functions/types and how to read the callee.

Symbol kinds: language-dependent (function/method/struct/class/enum/…).
Edge kinds: ``CALLS`` — same-file → unique project-wide → ``<lang>:extern:<n>``.

Limitations (documented honesty):
- Symbols + CALLS only (no contracts/data_access yet — per-framework, later).
- Call resolution is name-based (no type resolution); a member call keeps its
  last identifier as the callee name.
- Requires ``tree-sitter-language-pack``; absent → language reported skipped.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_SOURCE_NAME = "ggoss-treesitter"
_SOURCE_VERSION = "1.0.0"


# ─── per-language config (adding a language = a new entry here) ─────
#
# ``func``/``type`` = AST node types to emit as symbols; ``call`` = call node
# types; ``callee_field`` = the tree-sitter field holding the callee
# expression; ``kinds`` maps node type → Mnemos symbol kind.

_LANG: dict[str, dict[str, Any]] = {
    "go": {
        "exts": (".go",),
        "func": {"function_declaration", "method_declaration"},
        "type": {"type_spec"},
        "call": {"call_expression"},
        "callee_field": "function",
        "kinds": {
            "function_declaration": "function",
            "method_declaration": "method",
            "type_spec": "type",
        },
        # HTTP route registrations → contracts (cross-service linking). Keyed
        # by the call's method name (lowercased): Gin/Echo/chi router verbs
        # (r.GET/e.POST/…) carry the method; net/http HandleFunc/Handle don't,
        # so they default to GET (documented approximation).
        "routes": {
            "get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE",
            "patch": "PATCH", "head": "HEAD", "options": "OPTIONS",
            "handlefunc": "GET", "handle": "GET",
        },
    },
    "rust": {
        "exts": (".rs",),
        "func": {"function_item"},
        "type": {"struct_item", "enum_item", "trait_item"},
        "call": {"call_expression"},
        "callee_field": "function",
        "kinds": {
            "function_item": "function",
            "struct_item": "struct",
            "enum_item": "enum",
            "trait_item": "interface",
        },
    },
    "ruby": {
        "exts": (".rb",),
        "func": {"method", "singleton_method"},
        "type": {"class", "module"},
        "call": {"call"},
        "callee_field": "method",
        "kinds": {
            "method": "method",
            "singleton_method": "method",
            "class": "class",
            "module": "module",
        },
    },
    "php": {
        "exts": (".php",),
        "func": {"function_definition", "method_declaration"},
        "type": {"class_declaration", "interface_declaration",
                 "trait_declaration", "enum_declaration"},
        "call": {"function_call_expression", "member_call_expression",
                 "scoped_call_expression"},
        # function_call_expression carries the callee on ``function``;
        # member/scoped calls carry the method on ``name``.
        "callee_field": ("function", "name"),
        "kinds": {
            "function_definition": "function",
            "method_declaration": "method",
            "class_declaration": "class",
            "interface_declaration": "interface",
            "trait_declaration": "trait",
            "enum_declaration": "enum",
        },
    },
    "scala": {
        "exts": (".scala", ".sc"),
        "func": {"function_definition"},
        "type": {"class_definition", "object_definition", "trait_definition"},
        "call": {"call_expression"},
        "callee_field": "function",
        "kinds": {
            "function_definition": "function",
            "class_definition": "class",
            "object_definition": "object",
            "trait_definition": "interface",
        },
    },
    "swift": {
        "exts": (".swift",),
        "func": {"function_declaration"},
        "type": {"class_declaration", "struct_declaration",
                 "protocol_declaration", "enum_declaration"},
        "call": {"call_expression"},
        # Swift call_expression has no ``function`` field — the callee is the
        # first child; ``callee_field`` empty triggers the first-child path.
        "callee_field": (),
        "kinds": {
            "function_declaration": "function",
            "class_declaration": "class",
            "struct_declaration": "struct",
            "protocol_declaration": "interface",
            "enum_declaration": "enum",
        },
    },
}

_EXT_TO_LANG: dict[str, str] = {
    ext: lang for lang, cfg in _LANG.items() for ext in cfg["exts"]
}
_ALL_EXTS = set(_EXT_TO_LANG)
_SKIP_DIRS = {
    ".git", ".mnemos", "node_modules", "target", "build", "dist", "out",
    "vendor", "vendored", "third_party", ".venv", "__pycache__",
}


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


def _ts_available() -> bool:
    try:
        import tree_sitter_language_pack  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        if root.suffix.lower() in _ALL_EXTS:
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if Path(name).suffix.lower() in _ALL_EXTS:
                yield Path(dirpath) / name


def _rel(path: Path, target: Path) -> str:
    try:
        return path.resolve().relative_to(target.resolve()).as_posix()
    except ValueError:
        return path.name


def _component_id(target: Path) -> str:
    return f"ts.{target.resolve().name or 'project'}"


# ─── AST walk ──────────────────────────────────────────────────────


@dataclass
class _Sym:
    id: str
    kind: str
    name: str
    qual: str
    line: int


@dataclass
class _Call:
    caller_id: str
    callee: str
    line: int


@dataclass
class _Route:
    method: str
    path: str
    exposer_id: str | None  # enclosing function that registers the route
    line: int


@dataclass
class _FileParse:
    lang: str
    rel: str
    symbols: list[_Sym]
    calls: list[_Call] = field(default_factory=list)
    routes: list[_Route] = field(default_factory=list)


def _text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _node_name(src: bytes, node) -> str | None:
    nm = node.child_by_field_name("name")
    return _text(src, nm) if nm is not None else None


def _first_string_arg(src: bytes, node) -> str | None:
    """First string-literal argument of a call — the route path in
    ``r.GET("/owners", h)``. Quotes/backticks stripped; f-strings/dynamic
    args are ignored (we don't guess a path we can't read)."""
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    for c in args.named_children:
        if "string" in c.type:
            txt = _text(src, c).strip()
            for q in ('"', "`", "'"):
                if txt.startswith(q) and txt.endswith(q) and len(txt) >= 2:
                    return txt[1:-1]
            return txt
    return None


def _bare_callee(src: bytes, node, callee_field) -> str | None:
    """Last identifier of the callee expression: ``r.db.Insert`` → ``Insert``,
    ``foo`` → ``foo``, ``a::b::c`` → ``c``.

    ``callee_field`` may be a field name, a tuple of field names to try in
    order (languages differ: Go/Scala use ``function``, Ruby/PHP-method use
    ``name``), or empty — in which case the callee is the call node's first
    named child (Swift, whose call_expression exposes no callee field)."""
    fields = (callee_field,) if isinstance(callee_field, str) else tuple(callee_field)
    fn = None
    for f in fields:
        fn = node.child_by_field_name(f)
        if fn is not None:
            break
    if fn is None:
        # First-child fallback (Swift): the callee expression leads the call.
        fn = next((c for c in node.named_children), None)
    if fn is None:
        return None
    txt = _text(src, fn).strip()
    # Drop call args if the first child was the whole ``callee(args)`` shape.
    if "(" in txt:
        txt = txt.split("(", 1)[0]
    for sep in ("::", ".", "->"):
        if sep in txt:
            txt = txt.rsplit(sep, 1)[-1]
    txt = txt.strip()
    return txt if txt.isidentifier() else None


def _symbol_id(lang: str, rel: str, qual: str, line: int) -> str:
    return f"{lang}:{rel}:{qual}@{line}"


def _parse_file(path: Path, target: Path) -> _FileParse | None:
    from tree_sitter_language_pack import get_parser

    lang = _EXT_TO_LANG.get(path.suffix.lower())
    if lang is None:
        return None
    cfg = _LANG[lang]
    try:
        src = path.read_bytes()
    except OSError as exc:
        sys.stderr.write(json.dumps({
            "level": "error", "file": str(path),
            "message": f"read failed: {exc}", "recoverable": True,
        }) + "\n")
        return None
    try:
        parser = get_parser(lang)
        tree = parser.parse(src)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(json.dumps({
            "level": "error", "file": str(path),
            "message": f"parse failed: {exc}", "recoverable": True,
        }) + "\n")
        return None

    rel = _rel(path, target)
    fp = _FileParse(lang=lang, rel=rel, symbols=[])
    func_types, type_types = cfg["func"], cfg["type"]
    call_types, callee_field = cfg["call"], cfg["callee_field"]
    kinds = cfg["kinds"]
    routes_cfg = cfg.get("routes") or {}

    def walk(node, type_prefix: str, enclosing_func: str | None) -> None:
        nt = node.type
        if nt in call_types:
            callee = _bare_callee(src, node, callee_field)
            if callee:
                if enclosing_func is not None:
                    fp.calls.append(_Call(enclosing_func, callee,
                                          node.start_point[0] + 1))
                # HTTP route registration → contract (cross-service linking).
                verb = routes_cfg.get(callee.lower())
                if verb:
                    route = _first_string_arg(src, node)
                    if route and route.startswith("/"):
                        fp.routes.append(_Route(
                            verb, route, enclosing_func, node.start_point[0] + 1))
            for c in node.children:
                walk(c, type_prefix, enclosing_func)
            return
        if nt in type_types:
            name = _node_name(src, node)
            if name:
                line = node.start_point[0] + 1
                qual = f"{type_prefix}{name}"
                fp.symbols.append(_Sym(
                    _symbol_id(lang, rel, qual, line), kinds.get(nt, "type"),
                    name, qual, line))
                for c in node.children:
                    walk(c, qual + ".", enclosing_func)
                return
        if nt in func_types:
            name = _node_name(src, node)
            if name:
                line = node.start_point[0] + 1
                qual = f"{type_prefix}{name}"
                sym = _Sym(_symbol_id(lang, rel, qual, line),
                           kinds.get(nt, "function"), name, qual, line)
                fp.symbols.append(sym)
                for c in node.children:
                    walk(c, type_prefix, sym.id)
                return
        for c in node.children:
            walk(c, type_prefix, enclosing_func)

    walk(tree.root_node, "", None)
    return fp


def _parse_all(target: Path) -> list[_FileParse]:
    out = []
    for path in _iter_files(target):
        fp = _parse_file(path, target)
        if fp is not None:
            out.append(fp)
    return out


# ─── verbs ─────────────────────────────────────────────────────────


def cmd_probe(target: Path) -> dict[str, Any]:
    if not _ts_available():
        return {"applicable": False, "reason": "tree-sitter-language-pack not installed",
                "files_found": 0}
    n = sum(1 for _ in _iter_files(target))
    return {"applicable": n > 0, "reason": f"{n} files in configured languages",
            "files_found": n}


def cmd_inventory(target: Path) -> dict[str, Any]:
    by_lang: dict[str, int] = {}
    for p in _iter_files(target):
        lang = _EXT_TO_LANG.get(p.suffix.lower(), "?")
        by_lang[lang] = by_lang.get(lang, 0) + 1
    return {"languages": sorted(_LANG), "files_by_language": by_lang}


def cmd_symbols(target: Path, out_stream) -> None:
    if not _ts_available():
        return
    target = target.resolve()
    comp = _component_id(target)
    for fp in _parse_all(target):
        for s in fp.symbols:
            out_stream.write(_envelope("symbol", {
                "id": s.id, "kind": s.kind, "name": s.name, "qual_name": s.qual,
                "component_id": comp, "signature": "",
                "location": {"file": fp.rel, "line": s.line, "col": 1},
                "visibility": "public", "is_entry_point": s.name == "main",
                "metadata": {"language": fp.lang, "extractor": "tree_sitter"},
                "certainty": "asserted", "created_by": [_SOURCE_NAME],
            }) + "\n")


def cmd_calls(target: Path, out_stream) -> None:
    if not _ts_available():
        return
    target = target.resolve()
    parses = _parse_all(target)
    global_fn: dict[str, list[str]] = {}
    for fp in parses:
        for s in fp.symbols:
            if s.kind in ("function", "method"):
                global_fn.setdefault(s.name, []).append(s.id)
    for fp in parses:
        local = {}
        for s in fp.symbols:
            if s.kind in ("function", "method"):
                local.setdefault(s.name, s.id)
        seen: set[tuple[str, str]] = set()
        for c in fp.calls:
            resolved = local.get(c.callee)
            if resolved is None:
                ids = global_fn.get(c.callee, [])
                resolved = ids[0] if len(ids) == 1 else None
            target_id = resolved or f"{fp.lang}:extern:{c.callee}"
            key = (c.caller_id, target_id)
            if key in seen:
                continue
            seen.add(key)
            out_stream.write(_envelope("edge", {
                "source_id": c.caller_id, "target_id": target_id, "kind": "CALLS",
                "certainty": "asserted" if resolved else "inferred",
                "created_by": [_SOURCE_NAME],
                "metadata": {"invocation_site": {"line": c.line, "col": 1},
                             "callee_resolved": resolved is not None},
            }) + "\n")


def cmd_contracts(target: Path, out_stream) -> None:
    """HTTP route registrations → contracts, normalized to the same
    ``http.<METHOD>.<path>`` node a TS fetch / Java handler / HTML form
    resolves to (cross-service linking). Currently Go frameworks (net/http,
    Gin, Echo, chi) via the language's ``routes`` config."""
    if not _ts_available():
        return
    target = target.resolve()
    comp = _component_id(target)
    for fp in _parse_all(target):
        seen: set[str] = set()
        for r in fp.routes:
            cid = f"http.{r.method}.{r.path}"
            if cid not in seen:
                seen.add(cid)
                out_stream.write(_envelope("contract", {
                    "id": cid, "kind": "http_endpoint",
                    "name": f"{r.method} {r.path}",
                    "spec": {"method": r.method, "path": r.path},
                    "component_id": comp,
                    "location": {"file": fp.rel, "line": r.line, "col": 1},
                    "created_by": [_SOURCE_NAME], "certainty": "inferred",
                }) + "\n")
            if r.exposer_id is not None:
                out_stream.write(_envelope("edge", {
                    "source_id": r.exposer_id, "target_id": cid,
                    "kind": "EXPOSES", "certainty": "inferred",
                    "created_by": [_SOURCE_NAME],
                    "metadata": {"framework": "go_http", "line": r.line},
                }) + "\n")


def cmd_data_access(target: Path, out_stream) -> None:
    del target, out_stream


def cmd_schema() -> dict[str, Any]:
    return {
        "language": "multi", "version": _SOURCE_VERSION,
        "languages": sorted(_LANG),
        "symbol_kinds": ["function", "method", "struct", "class", "enum",
                         "interface", "module", "type"],
        "edge_kinds": ["CALLS", "EXPOSES"],
        "record_types": ["symbol", "edge", "contract"],
        "backend": "tree-sitter-language-pack",
    }


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(
            "usage: ggoss-treesitter "
            "<probe|inventory|symbols|calls|contracts|data_access|schema> <path>\n"
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
