"""PR-191 — ggoss-cpp: C/C++ analyzer.

A real-repo eval (docs/04-eval/agent-context-real-repo-test-2026-07-07.md)
ran Mnemos against a repository whose body is C (763 ``.c`` + 640 ``.h``
files) and every ``*:cpp`` stage skipped with ``no_analyzer`` — the graph
covered only the TypeScript UI and Python wrappers. This analyzer closes
that P0 gap with deterministic extraction, stdlib-only (§2.10 1-operator
friendly), same protocol as ggoss-py/ggoss-ts:

    ggoss-cpp probe|inventory|symbols|calls|contracts|data_access|schema <target>

Approach: NOT a full C/C++ semantic parser. Comments/strings are blanked
(offset-preserving), preprocessor lines are captured then blanked, and
function/struct/enum definitions are recognised at file scope with a
brace-depth scanner (``extern "C"`` / ``namespace`` braces are transparent).
This is the deliberate MVP the eval doc asked for: function/struct/enum/
macro symbols + CALLS edges first, no type inference.

Symbol kinds emitted:
- ``function`` — file-scope function definitions (incl. ``A::b`` methods)
- ``struct`` / ``enum`` / ``union`` / ``class`` — named type definitions
  (typedef'd anonymous structs get the typedef name)
- ``macro``    — function-like ``#define NAME(...)`` only (they participate
  in the call graph); object-like defines are skipped as noise

Edge kinds emitted:
- ``CALLS`` — resolution order: same-file definition (C ``static`` linkage)
  → unique project-wide name → unresolved ``c:extern:<name>``. Resolved
  edges are ``asserted``; extern edges are ``inferred``.
- ``READS``/``WRITES`` — SQL string literals inside function bodies
  (sqlite3_exec/prepare style), same verb/table regexes as ggoss-py.
  Always ``inferred``.

Paths: ``location.file`` and symbol ids use *project-relative POSIX* paths
(``c:src/main.c:main@42``) — the eval doc's P2 called out the abs/relative
mix the older analyzers produce.

Limitations (documented honesty, see docs/analyzer-contract.md):
- No preprocessor evaluation: code behind ``#ifdef`` branches is all seen;
  token-pasted (``##``) definitions are invisible.
- Function-pointer / member calls (``ops->read(...)``) are not resolved —
  only direct ``name(...)`` calls are emitted.
- K&R-style definitions and heavily macro-wrapped definitions are missed.
- ``contracts`` emits nothing: C HTTP routing is framework-less and
  string-driven; guessing routes would violate verified-vs-inferred honesty.
- ``vendored``/``vendor``/``third_party`` trees are excluded by default —
  the eval target's ``vendored/`` (89 files) is not the operator's code.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_SOURCE_NAME = "ggoss-cpp"
_SOURCE_VERSION = "1.0.0"


# ─── envelope ──────────────────────────────────────────────────────


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


# ─── source discovery ──────────────────────────────────────────────


_EXTS = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}

_SKIP_DIRS = {
    ".git", ".mnemos", ".svn", "__pycache__", "node_modules",
    "build", "dist", "out", "cmake-build-debug", "cmake-build-release",
    # Third-party code is not the operator's code; analysing it buries
    # the product graph under vendored symbols (eval doc P0 constraint).
    "vendored", "vendor", "third_party", "thirdparty", "external",
}


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


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


def _rel_posix(path: Path, target: Path) -> str:
    try:
        return path.resolve().relative_to(target.resolve()).as_posix()
    except ValueError:
        return path.name


def _component_id(target: Path) -> str:
    name = os.environ.get("MNEMOS_PROJECT_ID") or target.resolve().name or "c-project"
    return f"c.{name}"


def _symbol_id(rel: str, name: str, line: int) -> str:
    return f"c:{rel}:{name}@{line}"


# ─── comment / string / preprocessor blanking ──────────────────────
#
# Every transform below is offset-preserving (chars are replaced with
# spaces, newlines kept) so line numbers and body spans computed on the
# blanked text index directly into the original source.


_STRIP_RE = re.compile(
    r'//[^\n]*'
    r'|/\*.*?\*/'
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'",
    re.S,
)


def _blank(match: re.Match[str]) -> str:
    return "".join(c if c == "\n" else " " for c in match.group(0))


def _strip_comments_strings(src: str) -> str:
    return _STRIP_RE.sub(_blank, src)


_DEFINE_FN_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)\(", re.M)


def _blank_preprocessor(text: str) -> str:
    """Blank every preprocessor line (incl. backslash continuations),
    keeping newlines so offsets survive."""
    out: list[str] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("#"):
            while True:
                cont = lines[i].rstrip().endswith("\\")
                out.append(" " * len(lines[i]))
                if not cont or i + 1 >= len(lines):
                    break
                i += 1
        else:
            out.append(line)
        i += 1
    return "\n".join(out)


# ─── file-scope scanner ────────────────────────────────────────────


_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "return", "sizeof", "case", "do",
    "else", "goto", "defined", "typedef", "static_assert", "_Static_assert",
    "alignas", "alignof", "typeof", "__typeof__", "asm", "__asm__", "__asm",
    "decltype", "catch", "new", "delete", "throw", "not", "and", "or",
    "__attribute__", "__declspec", "__cdecl", "__stdcall", "restrict",
})

# Builtin/common type names — never call targets in C; skipping them in
# the call pass drops cast noise like ``(int)(x)``.
_TYPE_WORDS = frozenset({
    "void", "int", "char", "float", "double", "long", "short",
    "unsigned", "signed", "bool", "size_t", "ssize_t", "int8_t", "int16_t",
    "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "uintptr_t", "intptr_t", "ptrdiff_t", "wchar_t", "FILE", "va_list",
})

_IDENT_PAREN_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_~][A-Za-z0-9_]*)+"
    r"|[A-Za-z_][A-Za-z0-9_]*)\s*\("
)

_TYPE_DEF_RE = re.compile(
    r"\b(struct|union|enum|class)\s+([A-Za-z_]\w*)"
    r"\s*(?:final\s*)?(?::[^{;]*)?\{"
)

_TYPEDEF_ANON_RE = re.compile(r"\btypedef\s+(struct|union|enum)\b[^{;]*\{")

# After the parameter ``)`` only these may appear before the body ``{``.
_POST_PAREN_RE = re.compile(
    r"\s*(?:const|noexcept|override|final|restrict|__restrict__?"
    r"|__attribute__\s*\(\([^)]*\)\))*\s*$"
)

_EXTERN_C_TAIL_RE = re.compile(r'extern\s*(?:"\s*C\s*(?:\+\+)?\s*")?\s*$')
_NAMESPACE_TAIL_RE = re.compile(r"namespace\s+[\w:]*\s*$|namespace\s*$")


def _match_brace(text: str, open_idx: int) -> int:
    """Index of the ``}`` matching ``{`` at open_idx, or -1."""
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


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


@dataclass
class _Sym:
    id: str
    kind: str
    name: str
    line: int
    body_start: int = -1  # index of '{' (functions only)
    body_end: int = -1    # index of matching '}'
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FileScan:
    rel: str
    text: str            # blanked text (offset-preserving)
    symbols: list[_Sym]


def _scan_file(src: str, rel: str) -> _FileScan:
    """One pass over blanked source: macros, type defs, function defs."""
    stripped = _strip_comments_strings(src)
    symbols: list[_Sym] = []

    # Function-like macros — captured before preprocessor blanking.
    for m in _DEFINE_FN_RE.finditer(stripped):
        name = m.group(1)
        line = _line_of(stripped, m.start(1))
        symbols.append(_Sym(
            id=_symbol_id(rel, name, line), kind="macro", name=name, line=line,
        ))

    text = _blank_preprocessor(stripped)

    # Brace classification: extern "C" / namespace braces are transparent
    # for file-scope purposes. opaque_depth==0 ⇔ file scope.
    transparent_stack: list[bool] = []
    opaque_depth = 0
    pos = 0
    candidates: list[tuple[int, str]] = []  # (index, tag)
    for m in _IDENT_PAREN_RE.finditer(text):
        candidates.append((m.start(), "call_or_def"))
    for m in _TYPE_DEF_RE.finditer(text):
        candidates.append((m.start(), "type"))
    for m in _TYPEDEF_ANON_RE.finditer(text):
        candidates.append((m.start(), "typedef_anon"))
    candidates.sort()
    cand_i = 0

    def _advance_to(idx: int) -> None:
        nonlocal pos, opaque_depth
        while pos < idx:
            c = text[pos]
            if c == "{":
                head = text[max(0, pos - 80):pos]
                transparent = bool(
                    _EXTERN_C_TAIL_RE.search(head)
                    or _NAMESPACE_TAIL_RE.search(head)
                )
                transparent_stack.append(transparent)
                if not transparent:
                    opaque_depth += 1
            elif c == "}":
                if transparent_stack and not transparent_stack.pop():
                    opaque_depth = max(0, opaque_depth - 1)
            pos += 1

    src_lines = src.split("\n")

    def _signature_at(line: int) -> str:
        if 1 <= line <= len(src_lines):
            return src_lines[line - 1].strip()[:400]
        return ""

    while cand_i < len(candidates):
        idx, tag = candidates[cand_i]
        cand_i += 1
        if idx < pos:
            continue
        _advance_to(idx)
        if opaque_depth != 0:
            continue

        if tag == "type":
            m = _TYPE_DEF_RE.match(text, idx)
            if not m:
                continue
            kind, name = m.group(1), m.group(2)
            line = _line_of(text, m.start(2))
            symbols.append(_Sym(
                id=_symbol_id(rel, name, line), kind=kind, name=name, line=line,
            ))
            continue

        if tag == "typedef_anon":
            m = _TYPEDEF_ANON_RE.match(text, idx)
            if not m:
                continue
            open_idx = text.index("{", m.start())
            close = _match_brace(text, open_idx)
            if close < 0:
                continue
            tail = text[close + 1:close + 200]
            nm = re.match(r"\s*([A-Za-z_]\w*)\s*[;,]", tail)
            if nm:
                name = nm.group(1)
                line = _line_of(text, m.start())
                symbols.append(_Sym(
                    id=_symbol_id(rel, name, line), kind=m.group(1), name=name,
                    line=line, metadata={"typedef": True},
                ))
            # Skip the body so its contents are not misread as file scope.
            _advance_to(close + 1)
            continue

        # tag == "call_or_def": at file scope, IDENT(...) followed by '{'
        # (after benign specifiers) is a function definition.
        m = _IDENT_PAREN_RE.match(text, idx)
        if not m:
            continue
        name = m.group(1)
        bare = name.rsplit("::", 1)[-1]
        if bare in _KEYWORDS or bare in _TYPE_WORDS:
            continue
        open_paren = text.index("(", m.start())
        close_paren = _match_paren(text, open_paren)
        if close_paren < 0:
            continue
        after = text[close_paren + 1:]
        brace_rel = after.find("{")
        semi_rel = after.find(";")
        if brace_rel < 0 or (0 <= semi_rel < brace_rel):
            continue  # prototype / declaration
        between = after[:brace_rel]
        if not _POST_PAREN_RE.match(between):
            continue  # e.g. ``foo(1) + bar(2); if (...) {``
        # ``IDENT (`` preceded by ``.``/``->``/``)`` is an expression.
        before = text[:m.start()].rstrip()
        if before.endswith((".", "->", ")", "=", ",")):
            continue
        body_open = close_paren + 1 + brace_rel
        body_close = _match_brace(text, body_open)
        if body_close < 0:
            continue
        line = _line_of(text, m.start())
        symbols.append(_Sym(
            id=_symbol_id(rel, name, line),
            kind="function", name=name, line=line,
            body_start=body_open, body_end=body_close,
        ))
        # Jump past the body — nested braces stay out of the scope math.
        _advance_to(body_close + 1)

    for s in symbols:
        s.metadata.setdefault("signature", _signature_at(s.line))
    return _FileScan(rel=rel, text=text, symbols=symbols)


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
        rel = _rel_posix(path, target)
        out.append((path, src, _scan_file(src, rel)))
    return out


# ─── verbs ─────────────────────────────────────────────────────────


def cmd_probe(target: Path) -> dict[str, Any]:
    n = sum(1 for _ in _iter_files(target))
    return {"applicable": n > 0, "reason": f"{n} C/C++ files", "files_found": n}


def cmd_inventory(target: Path) -> dict[str, Any]:
    files = list(_iter_files(target))
    total_lines = 0
    for f in files:
        try:
            total_lines += sum(
                1 for _ in f.open("r", encoding="utf-8", errors="ignore")
            )
        except OSError:
            continue
    return {
        "language": "cpp",
        "files": len(files),
        "total_lines": total_lines,
    }


def cmd_symbols(target: Path, out_stream) -> None:
    target = target.resolve()
    comp_id = _component_id(target)
    for _path, _src, scan in _scan_all(target):
        for s in scan.symbols:
            out_stream.write(_envelope("symbol", {
                "id": s.id,
                "kind": s.kind,
                "name": s.name,
                "qual_name": s.name,
                "component_id": comp_id,
                "signature": s.metadata.get("signature", ""),
                "location": {"file": scan.rel, "line": s.line, "col": 1},
                "visibility": "public",
                "is_entry_point": s.name == "main",
                "metadata": {k: v for k, v in s.metadata.items()
                             if k != "signature"},
                "certainty": "asserted",
                "created_by": [_SOURCE_NAME],
            }) + "\n")


_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def cmd_calls(target: Path, out_stream) -> None:
    """Two passes: (A) collect every function/macro definition to build
    same-file and project-global name maps, (B) emit CALLS edges from
    each function body, deduped per (caller, callee)."""
    target = target.resolve()
    scans = _scan_all(target)

    global_names: dict[str, list[str]] = {}
    for _p, _s, scan in scans:
        for s in scan.symbols:
            if s.kind in ("function", "macro"):
                global_names.setdefault(s.name.rsplit("::", 1)[-1], []).append(s.id)

    for _p, _s, scan in scans:
        local = {
            s.name.rsplit("::", 1)[-1]: s.id
            for s in scan.symbols if s.kind in ("function", "macro")
        }
        seen: set[tuple[str, str]] = set()
        for s in scan.symbols:
            if s.kind != "function" or s.body_start < 0:
                continue
            body = scan.text[s.body_start:s.body_end]
            for m in _CALL_RE.finditer(body):
                callee = m.group(1)
                if callee in _KEYWORDS or callee in _TYPE_WORDS:
                    continue
                before = body[:m.start()].rstrip()
                if before.endswith((".", "->")):
                    continue  # member / function-pointer call: unresolvable
                # C static linkage: same file first, then unique global.
                resolved: str | None = local.get(callee)
                if resolved is None:
                    ids = global_names.get(callee, [])
                    resolved = ids[0] if len(ids) == 1 else None
                if resolved == s.id and callee == s.name:
                    resolved_is_self = True
                else:
                    resolved_is_self = False
                target_id = resolved or f"c:extern:{callee}"
                key = (s.id, target_id)
                if key in seen:
                    continue
                seen.add(key)
                line = _line_of(scan.text, s.body_start + m.start())
                out_stream.write(_envelope("edge", {
                    "source_id": s.id,
                    "target_id": target_id,
                    "kind": "CALLS",
                    "certainty": "asserted" if resolved else "inferred",
                    "created_by": [_SOURCE_NAME],
                    "metadata": {
                        "invocation_site": {"line": line, "col": 1},
                        "callee_resolved": resolved is not None,
                        **({"recursive": True} if resolved_is_self else {}),
                    },
                }) + "\n")


def cmd_contracts(target: Path, out_stream) -> None:
    """C has no framework route decorators to extract deterministically.
    Emitting guessed routes would blur verified-vs-inferred, so this verb
    is intentionally empty (the platform records 0 contracts, honestly)."""
    del target, out_stream


_SQL_VERB_RE = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE)\s+",
    re.IGNORECASE | re.MULTILINE,
)
_SQL_TABLE_RE = {
    "SELECT": re.compile(r"\bFROM\s+([A-Za-z_][\w.]*)", re.IGNORECASE),
    "INSERT": re.compile(r"\bINTO\s+([A-Za-z_][\w.]*)", re.IGNORECASE),
    "UPDATE": re.compile(r"\bUPDATE\s+([A-Za-z_][\w.]*)", re.IGNORECASE),
    "DELETE": re.compile(r"\bFROM\s+([A-Za-z_][\w.]*)", re.IGNORECASE),
}
_VERB_TO_KIND = {
    "SELECT": "READS", "INSERT": "WRITES",
    "UPDATE": "WRITES", "DELETE": "WRITES",
}

_STR_LIT_RE = re.compile(r'"(?:\\.|[^"\\\n])*"')


def _string_literals_joined(original_body: str) -> list[str]:
    """String literal contents; adjacent literals (C concatenation,
    ``"SELECT x " "FROM t"``) are joined so SQL split across lines
    still parses."""
    out: list[str] = []
    last_end = -1
    for m in _STR_LIT_RE.finditer(original_body):
        content = m.group(0)[1:-1]
        between = original_body[last_end:m.start()] if last_end >= 0 else None
        if between is not None and between.strip() == "":
            out[-1] = out[-1] + content
        else:
            out.append(content)
        last_end = m.end()
    return out


def _sql_targets(sql: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _SQL_VERB_RE.finditer(sql):
        verb = m.group(1).upper()
        tail = sql[m.end():]
        tm = _SQL_TABLE_RE[verb].search(tail)
        if tm:
            out.append((_VERB_TO_KIND[verb], tm.group(1)))
    return out


def cmd_data_access(target: Path, out_stream) -> None:
    target = target.resolve()
    comp_id = _component_id(target)
    for _path, src, scan in _scan_all(target):
        emitted: set[str] = set()
        for s in scan.symbols:
            if s.kind != "function" or s.body_start < 0:
                continue
            original_body = src[s.body_start:s.body_end]
            for lit in _string_literals_joined(original_body):
                for kind, table in _sql_targets(lit):
                    ent_id = f"data:{table.lower()}"
                    if ent_id not in emitted:
                        emitted.add(ent_id)
                        out_stream.write(_envelope("data_entity", {
                            "id": ent_id,
                            "logical_name": table.lower(),
                            "component_id": comp_id,
                            "schema": None,
                            "created_by": [_SOURCE_NAME],
                            "certainty": "inferred",
                        }) + "\n")
                    out_stream.write(_envelope("edge", {
                        "source_id": s.id,
                        "target_id": ent_id,
                        "kind": kind,
                        "certainty": "inferred",
                        "created_by": [_SOURCE_NAME],
                        "metadata": {"via": "sql_literal", "line": s.line},
                    }) + "\n")


def cmd_schema() -> dict[str, Any]:
    return {
        "language": "cpp",
        "version": _SOURCE_VERSION,
        "symbol_kinds": ["function", "struct", "enum", "union", "class", "macro"],
        "edge_kinds": ["CALLS", "READS", "WRITES"],
        "record_types": ["symbol", "edge", "data_entity"],
    }


# ─── CLI entry ─────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(
            "usage: ggoss-cpp "
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
