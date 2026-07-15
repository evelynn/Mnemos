"""PR-192 — ggoss-java: Java analyzer.

A web+Java eval (docs/04-eval/web-java-eval-2026-07-08.md) ran Mnemos
against Spring PetClinic (48 Java + 12 HTML + 5 CSS files) and produced a
completely EMPTY graph: ``symbols:java`` skipped ``no_analyzer`` and the
LLM fallback was ``agent_sdk_unavailable``. Java web backends — the most
common enterprise topology paired with a TS frontend — were a total blind
spot. This analyzer closes that with deterministic extraction, stdlib-only
(§2.10 1-operator friendly, zero pip), same protocol as ggoss-cpp/ggoss-py:

    ggoss-java probe|inventory|symbols|calls|contracts|data_access|schema <target>

Approach: NOT a full Java parser. Comments/strings/text-blocks are blanked
(offset-preserving) and a brace-depth scanner classifies each ``{`` as a
*type* body (class/interface/enum/record/@interface) or a *method* body,
tracking the type-name stack for fully-qualified ids. The deliberate MVP
mirrors ggoss-cpp: types + methods + CALLS first, no type inference.

Symbol kinds emitted:
- ``class`` / ``interface`` / ``enum`` / ``record`` / ``annotation_type``
- ``method`` (incl. constructors, tagged in metadata)

Edge kinds emitted:
- ``CALLS`` — same-file → unique project-wide name → ``java:extern:<name>``.
  Resolved edges are ``asserted``; extern edges are ``inferred``.
- ``EXPOSES`` — a mapping-annotated method exposes an HTTP contract.
- ``READS`` / ``WRITES`` — SQL string literals in ``@Query`` / method bodies.

Contracts: Spring (``@RestController``/``@Controller`` + ``@RequestMapping``/
``@GetMapping``/``@PostMapping``/``@PutMapping``/``@DeleteMapping``/
``@PatchMapping``) and JAX-RS (``@Path`` + ``@GET``/``@POST``/…). Each is
emitted with ``kind="http_endpoint"`` + ``spec={method,path}`` so the platform
normalizes it to the SAME ``http.<METHOD>.<path>`` node a TS ``fetch`` client
resolves to — that is what makes Java-server ↔ TS-client cross-service
linking (spec pillar 5) work.

Paths: ``location.file`` and ids use project-relative POSIX paths
(``java:src/main/java/.../OwnerController.java:OwnerController.processFindForm@94``).

Limitations (documented honesty, see docs/analyzer-contract.md):
- Call resolution is name-based with a receiver-class shortcut (``Type.m()``
  resolves within a known project class); a bare call to an overloaded /
  same-named method across classes with a non-class receiver stays
  unresolved (no full type inference / variable-type tracking).
- Generics, lambdas-as-calls, method-reference (``::``) targets, and calls
  through field/getter chains are not resolved.
- Parameterized route params keep the framework's own name (Spring
  ``{ownerId}`` vs a TS client's ``{id}``) so parameterized endpoints may not
  link; static paths link exactly.
- ``build``/``target``/``generated`` and test trees are lower priority; only
  ``target``/``build``/``generated`` output dirs are excluded outright.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_SOURCE_NAME = "ggoss-java"
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


_EXTS = {".java"}
_SKIP_DIRS = {
    ".git", ".mnemos", ".idea", ".gradle", ".mvn",
    "target", "build", "out", "bin", "generated", "generated-sources",
    "node_modules",
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
    name = os.environ.get("MNEMOS_PROJECT_ID") or target.resolve().name or "java-project"
    return f"java.{name}"


def _symbol_id(rel: str, qual: str, line: int) -> str:
    return f"java:{rel}:{qual}@{line}"


# ─── comment / string / text-block blanking (offset-preserving) ────


_STRIP_RE = re.compile(
    r'//[^\n]*'
    r'|/\*.*?\*/'
    r'|"""(?:\\.|[^\\])*?"""'
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'",
    re.S,
)


def _blank(match: re.Match[str]) -> str:
    return "".join(c if c == "\n" else " " for c in match.group(0))


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


# ─── declarations ──────────────────────────────────────────────────


_TYPE_KINDS = {
    "class": "class",
    "interface": "interface",
    "enum": "enum",
    "record": "record",
    "@interface": "annotation_type",
}
# Header (annotations+modifiers+keyword+name) ending right before the body '{'.
_TYPE_RE = re.compile(
    r"\b(class|interface|enum|record|@interface)\s+([A-Za-z_]\w*)"
)
_CTRL_WORDS = frozenset({
    "if", "for", "while", "switch", "catch", "synchronized", "try",
    "return", "new", "throw", "else", "do", "finally", "assert", "yield",
    "super", "this", "instanceof", "case",
})
# A method/constructor header: ``... name(params) [throws ...]`` right
# before the body brace. The name is the last identifier before '('.
_METHOD_HEAD_RE = re.compile(
    r"([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:throws\s[\w.,\s]+?)?\s*$",
    re.S,
)
# Annotations (``@Name`` or ``@Name(...)``) precede modifiers AND decorate
# parameters (``@PathVariable(...) Integer id``). Blanked from a *detection*
# copy of the header so the method name isn't shadowed by an annotation name
# whose paren-group otherwise swallows the real signature.
_ANNOT_BLANK_RE = re.compile(r"@\w+\s*(?:\([^()]*\))?")
_ANNOT_RE = re.compile(r"@([A-Za-z_]\w*)\s*(\([^()]*(?:\([^()]*\)[^()]*)*\))?")


@dataclass
class _Sym:
    id: str
    kind: str
    name: str
    qual: str          # Outer.Inner.method (type nesting, no package)
    line: int
    header_src: str = ""     # original-source header for annotation parsing
    body_start: int = -1     # blanked-text index of '{' (methods)
    body_end: int = -1
    src_body_start: int = -1  # original-source index of '{' (for SQL literals)
    src_body_end: int = -1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FileScan:
    rel: str
    package: str
    symbols: list[_Sym]
    class_annotics: dict[str, str]  # type qual → original class header
    text: str = ""   # blanked source; _strip is offset-preserving so body
    src: str = ""    # indices are valid against BOTH text and src
    # PR-202 — type-aware resolution: class qual → {field name → type simple}.
    class_fields: dict[str, dict[str, str]] = field(default_factory=dict)


# A field / local declaration: ``[<generics>] Type name`` where Type is a
# class (leading uppercase — primitives are lowercase and useless for method
# resolution). Used for receiver-type call resolution (PR-202).
_TYPED_DECL_RE = re.compile(
    r"\b([A-Z]\w*)(?:\s*<[^;{}()]*>)?(?:\s*\[\s*\])?\s+([a-z_]\w*)\s*$"
)
_LOCAL_DECL_RE = re.compile(
    r"\b([A-Z]\w*)(?:\s*<[^;{}()]*>)?(?:\s*\[\s*\])?\s+([a-z_]\w*)\s*[=;]"
)


def _prev_boundary(text: str, idx: int) -> int:
    """Index just past the nearest preceding ``;`` ``{`` or ``}`` (or 0)."""
    b = 0
    for ch in (";", "{", "}"):
        p = text.rfind(ch, 0, idx)
        if p + 1 > b:
            b = p + 1
    return b


def _scan_file(src: str, rel: str) -> _FileScan:
    text = _strip(src)
    symbols: list[_Sym] = []
    class_headers: dict[str, str] = {}
    class_fields: dict[str, dict[str, str]] = {}

    pkg = ""
    mpkg = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    if mpkg:
        pkg = mpkg.group(1)

    type_stack: list[str] = []       # names contributing to FQN
    type_kind_stack: list[str] = []  # enclosing type kind (enum guard)
    frame_kinds: list[str] = []      # 'type' | 'method' | 'other' per '{'
    method_frames: list[_Sym] = []   # open method syms awaiting body_end

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "{":
            boundary = _prev_boundary(text, i)
            header = text[boundary:i]
            header_src = src[boundary:i]
            tmatch = None
            for m in _TYPE_RE.finditer(header):
                tmatch = m  # last type keyword in the header wins
            if tmatch is not None:
                kind = _TYPE_KINDS[tmatch.group(1)]
                name = tmatch.group(2)
                qual = ".".join(type_stack + [name])
                line = _line_of(text, boundary + tmatch.start(2))
                sym = _Sym(
                    id=_symbol_id(rel, qual, line), kind=kind, name=name,
                    qual=qual, line=line, header_src=header_src.strip()[:600],
                )
                symbols.append(sym)
                class_headers[qual] = header_src
                type_stack.append(name)
                type_kind_stack.append(kind)
                frame_kinds.append("type")
            else:
                header_det = _ANNOT_BLANK_RE.sub(
                    lambda m: " " * len(m.group(0)), header
                )
                mh = _METHOD_HEAD_RE.search(header_det)
                is_method = (
                    mh is not None
                    and frame_kinds[-1:] == ["type"]
                    and mh.group(1) not in _CTRL_WORDS
                )
                if is_method:
                    name = mh.group(1)
                    qual = ".".join(type_stack + [name])
                    line = _line_of(text, boundary + mh.start(1))
                    is_ctor = bool(type_stack) and name == type_stack[-1]
                    sym = _Sym(
                        id=_symbol_id(rel, qual, line),
                        kind="method", name=name, qual=qual, line=line,
                        header_src=header_src.strip()[:600],
                        body_start=i, src_body_start=i,
                        metadata={"constructor": True} if is_ctor else {},
                    )
                    symbols.append(sym)
                    method_frames.append(sym)
                    frame_kinds.append("method")
                else:
                    frame_kinds.append("other")
            i += 1
            continue
        if c == "}":
            if frame_kinds:
                kind = frame_kinds.pop()
                if kind == "type":
                    if type_stack:
                        type_stack.pop()
                    if type_kind_stack:
                        type_kind_stack.pop()
                elif kind == "method" and method_frames:
                    m = method_frames.pop()
                    m.body_end = i
                    m.src_body_end = i
            i += 1
            continue
        if c == ";":
            # Abstract / interface method: ``ReturnType name(params);`` with no
            # body, directly in a type body. Excludes fields (have ``=`` or no
            # parens) and enum constants (enclosing enum guarded out). This is
            # what makes Spring-Data repository query methods graph symbols.
            if (
                frame_kinds[-1:] == ["type"]
                and type_kind_stack[-1:] != ["enum"]
                and type_kind_stack[-1:] != ["annotation_type"]
            ):
                boundary = _prev_boundary(text, i)
                header = text[boundary:i]
                header_det = _ANNOT_BLANK_RE.sub(
                    lambda m: " " * len(m.group(0)), header
                )
                mh = (
                    _METHOD_HEAD_RE.search(header_det)
                    if "=" not in header else None
                )
                if mh is not None and mh.group(1) not in _CTRL_WORDS:
                    name = mh.group(1)
                    qual = ".".join(type_stack + [name])
                    line = _line_of(text, boundary + mh.start(1))
                    symbols.append(_Sym(
                        id=_symbol_id(rel, qual, line), kind="method",
                        name=name, qual=qual, line=line,
                        header_src=src[boundary:i].strip()[:600],
                        metadata={"abstract": True},
                    ))
                else:
                    # Field declaration ``[modifiers] Type name [= …]`` — record
                    # its type for receiver-type call resolution (PR-202). The
                    # declarator (before ``=``) must have no parens (else it is
                    # a method) and end in ``Type name``.
                    decl = header_det.split("=", 1)[0]
                    if "(" not in decl and ")" not in decl:
                        fm = _TYPED_DECL_RE.search(decl)
                        if fm and fm.group(1) not in _CTRL_WORDS:
                            cls_qual = ".".join(type_stack)
                            if cls_qual:
                                class_fields.setdefault(cls_qual, {})[
                                    fm.group(2)] = fm.group(1)
            i += 1
            continue
        i += 1

    # Signature = first line of the original header (annotations dropped).
    src_lines = src.split("\n")
    for s in symbols:
        if 1 <= s.line <= len(src_lines):
            sig = src_lines[s.line - 1].strip()
            s.metadata.setdefault("signature", sig[:400])

    return _FileScan(rel=rel, package=pkg, symbols=symbols,
                     class_annotics=class_headers, text=text, src=src,
                     class_fields=class_fields)


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


# ─── verbs: probe / inventory / symbols ────────────────────────────


def cmd_probe(target: Path) -> dict[str, Any]:
    n = sum(1 for _ in _iter_files(target))
    return {"applicable": n > 0, "reason": f"{n} Java files", "files_found": n}


def cmd_inventory(target: Path) -> dict[str, Any]:
    files = list(_iter_files(target))
    total = 0
    for f in files:
        try:
            total += sum(1 for _ in f.open("r", encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return {"language": "java", "files": len(files), "total_lines": total}


def cmd_symbols(target: Path, out_stream) -> None:
    target = target.resolve()
    comp_id = _component_id(target)
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
                "component_id": comp_id,
                "signature": s.metadata.get("signature", ""),
                "location": {"file": scan.rel, "line": s.line, "col": 1},
                "visibility": "public",
                "is_entry_point": s.name == "main",
                "metadata": meta,
                "certainty": "asserted",
                "created_by": [_SOURCE_NAME],
            }) + "\n")


# ─── verb: calls ───────────────────────────────────────────────────


_CALL_RECV_RE = re.compile(
    r"(?:([A-Za-z_]\w*)\s*\.\s*)?([A-Za-z_]\w*)\s*\("
)


def _enclosing_class(qual: str) -> str | None:
    parts = qual.split(".")
    return parts[-2] if len(parts) >= 2 else None


def _parse_params(header_src: str) -> dict[str, str]:
    """``{param name → type simple}`` from a method header. Annotations are
    blanked so the first ``(...)`` is the parameter list; generics dropped."""
    hdr = _ANNOT_BLANK_RE.sub(lambda m: " " * len(m.group(0)), header_src)
    m = re.search(r"\(([^()]*)\)", hdr)
    if not m:
        return {}
    params = re.sub(r"<[^<>]*>", "", m.group(1))
    env: dict[str, str] = {}
    for part in params.split(","):
        toks = [t for t in part.replace("final", " ").replace("[]", " ").split()
                if t]
        if len(toks) >= 2 and toks[-2][:1].isupper() and toks[-1].isidentifier():
            env[toks[-1]] = toks[-2]
    return env


def cmd_calls(target: Path, out_stream) -> None:
    target = target.resolve()
    scans = _scan_all(target)

    global_methods: dict[str, list[str]] = {}
    # class simple-name → {method name → id}. Type-aware resolution (PR-202)
    # looks a receiver's type up here; static ``Type.method()`` too.
    class_methods: dict[str, dict[str, str]] = {}
    # class simple-name → {field name → type simple} (receiver-type env).
    fields_by_class: dict[str, dict[str, str]] = {}
    # class simple-name → class node id / its constructor id (for ``new T()``).
    class_by_name: dict[str, list[str]] = {}
    ctor_by_class: dict[str, str] = {}
    for _p, _s, scan in scans:
        for s in scan.symbols:
            if s.kind == "method":
                global_methods.setdefault(s.name, []).append(s.id)
                cls = _enclosing_class(s.qual)
                if cls:
                    class_methods.setdefault(cls, {}).setdefault(s.name, s.id)
                if s.metadata.get("constructor"):
                    ctor_by_class.setdefault(s.name, s.id)
            elif s.kind in ("class", "enum", "record"):
                class_by_name.setdefault(s.name, []).append(s.id)
        for cls_qual, flds in scan.class_fields.items():
            fields_by_class.setdefault(cls_qual.split(".")[-1], {}).update(flds)

    for _p, _s, scan in scans:
        # Same-file method names → ids, keeping AMBIGUITY: a name defined by
        # two classes in one file must NOT resolve to whichever came first
        # (pre-fix precision bug) — only a unique same-file name resolves.
        local_by_name: dict[str, list[str]] = {}
        for s in scan.symbols:
            if s.kind == "method":
                local_by_name.setdefault(s.name, []).append(s.id)
        for s in scan.symbols:
            if s.kind != "method" or s.body_start < 0:
                continue
            body = scan.text[s.body_start:s.body_end]
            # Type env for THIS method: enclosing-class fields + params +
            # method-body local declarations. Drives receiver-type resolution.
            type_env: dict[str, str] = {}
            encl = _enclosing_class(s.qual)
            if encl:
                type_env.update(fields_by_class.get(encl, {}))
            type_env.update(_parse_params(s.header_src))
            for lm in _LOCAL_DECL_RE.finditer(body):
                if lm.group(1) not in _CTRL_WORDS:
                    type_env.setdefault(lm.group(2), lm.group(1))
            seen: set[tuple[str, str]] = set()
            for m in _CALL_RECV_RE.finditer(body):
                receiver, callee = m.group(1), m.group(2)
                if callee in _CTRL_WORDS or (receiver and receiver in _CTRL_WORDS):
                    continue
                via = None
                resolved = None
                # 0) Receiver is a typed variable (field/param/local) → resolve
                #    the call in that variable's class. The precise, cbm-like
                #    path that disambiguates same-named methods (PR-202).
                if receiver and receiver in type_env:
                    rtype = type_env[receiver]
                    resolved = class_methods.get(rtype, {}).get(callee)
                    if resolved is not None:
                        via = "receiver_type"
                # 1) Receiver is a known project class → static call.
                if resolved is None and receiver and callee in class_methods.get(receiver, {}):
                    resolved = class_methods[receiver][callee]
                    via = "receiver_class"
                # 2) Unique same-file method of that name (ambiguous → skip).
                if resolved is None:
                    ids = local_by_name.get(callee, [])
                    if len(ids) == 1:
                        resolved = ids[0]
                # 3) Unique project-wide.
                if resolved is None:
                    ids = global_methods.get(callee, [])
                    resolved = ids[0] if len(ids) == 1 else None
                # 4) Constructor call ``new Type()`` → the type's constructor,
                #    or the class node when the constructor is implicit.
                if resolved is None and receiver is None and callee in class_by_name:
                    before = body[:m.start()].rstrip()
                    if before.endswith("new"):
                        cids = class_by_name[callee]
                        resolved = ctor_by_class.get(callee) or (
                            cids[0] if len(cids) == 1 else None
                        )
                        if resolved is not None:
                            via = "constructor"
                target_id = resolved or f"java:extern:{callee}"
                key = (s.id, target_id)
                if key in seen:
                    continue
                seen.add(key)
                line = _line_of(scan.text, s.body_start + m.start(2))
                meta = {
                    "invocation_site": {"line": line, "col": 1},
                    "callee_resolved": resolved is not None,
                }
                if via:
                    meta["resolution"] = via
                out_stream.write(_envelope("edge", {
                    "source_id": s.id,
                    "target_id": target_id,
                    "kind": "CALLS",
                    "certainty": "asserted" if resolved else "inferred",
                    "created_by": [_SOURCE_NAME],
                    "metadata": meta,
                }) + "\n")


# ─── verb: contracts (Spring / JAX-RS) ─────────────────────────────


_SPRING_VERB = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}
_JAXRS_VERB = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


def _annot_string_arg(annot_args: str | None) -> str | None:
    """First string literal in an annotation arg list, honouring
    ``value=``/``path=`` attributes. Adjacent literals are joined."""
    if not annot_args:
        return None
    # value = "..." or path = "..." takes precedence if present.
    m = re.search(r'\b(?:value|path)\s*=\s*"((?:\\.|[^"\\])*)"', annot_args)
    if m:
        return m.group(1)
    m = re.search(r'"((?:\\.|[^"\\])*)"', annot_args)
    return m.group(1) if m else None


def _request_mapping_method(annot_args: str | None) -> str | None:
    if not annot_args:
        return None
    m = re.search(r"method\s*=\s*(?:\{)?\s*RequestMethod\.([A-Z]+)", annot_args)
    return m.group(1) if m else None


def _class_route_prefix(header_src: str) -> str:
    for m in _ANNOT_RE.finditer(header_src):
        if m.group(1) in ("RequestMapping",) or m.group(1) in _SPRING_VERB:
            p = _annot_string_arg(m.group(2))
            if p:
                return p
    # JAX-RS class-level @Path
    for m in _ANNOT_RE.finditer(header_src):
        if m.group(1) == "Path":
            p = _annot_string_arg(m.group(2))
            if p:
                return p
    return ""


def _join_route(prefix: str, route: str) -> str:
    parts = [p for p in (prefix or "", route or "") if p and p != "/"]
    joined = "/".join(s.strip("/") for s in parts)
    return "/" + joined if joined else "/"


def _enclosing_type_qual(sym: _Sym) -> str:
    return sym.qual.rsplit(".", 1)[0] if "." in sym.qual else ""


def cmd_contracts(target: Path, out_stream) -> None:
    target = target.resolve()
    comp_id = _component_id(target)
    for _p, _s, scan in _scan_all(target):
        for s in scan.symbols:
            if s.kind != "method":
                continue
            type_qual = _enclosing_type_qual(s)
            class_header = scan.class_annotics.get(type_qual, "")
            prefix = _class_route_prefix(class_header)
            for m in _ANNOT_RE.finditer(s.header_src):
                name, args = m.group(1), m.group(2)
                methods: list[str] = []
                route = _annot_string_arg(args) or ""
                if name in _SPRING_VERB:
                    methods = [_SPRING_VERB[name]]
                elif name == "RequestMapping":
                    rm = _request_mapping_method(args)
                    methods = [rm] if rm else ["GET"]
                elif name in _JAXRS_VERB:
                    methods = [name]
                    route = ""  # JAX-RS path is a separate @Path
                    pm = re.search(r'@Path\s*\(\s*"((?:\\.|[^"\\])*)"',
                                   s.header_src)
                    if pm:
                        route = pm.group(1)
                else:
                    continue
                full = _join_route(prefix, route)
                for verb in methods:
                    cid = f"http.{verb}.{full}"
                    out_stream.write(_envelope("contract", {
                        "id": cid,
                        "kind": "http_endpoint",
                        "name": f"{verb} {full}",
                        "spec": {"method": verb, "path": full},
                        "component_id": comp_id,
                        "location": {"file": scan.rel, "line": s.line, "col": 1},
                        "created_by": [_SOURCE_NAME],
                        "certainty": "asserted",
                    }) + "\n")
                    out_stream.write(_envelope("edge", {
                        "source_id": s.id,
                        "target_id": cid,
                        "kind": "EXPOSES",
                        "certainty": "asserted",
                        "created_by": [_SOURCE_NAME],
                        "metadata": {"framework": "spring_or_jaxrs",
                                     "line": s.line},
                    }) + "\n")


# ─── verb: data_access (JPA entities + SQL literals) ───────────────


_SQL_VERB_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_SQL_TABLE_RE = {
    "SELECT": re.compile(r"\bFROM\s+([A-Za-z_]\w*)", re.IGNORECASE),
    "INSERT": re.compile(r"\bINTO\s+([A-Za-z_]\w*)", re.IGNORECASE),
    "UPDATE": re.compile(r"\bUPDATE\s+([A-Za-z_]\w*)", re.IGNORECASE),
    "DELETE": re.compile(r"\bFROM\s+([A-Za-z_]\w*)", re.IGNORECASE),
}
_VERB_KIND = {"SELECT": "READS", "INSERT": "WRITES",
              "UPDATE": "WRITES", "DELETE": "WRITES"}
_STR_RE = re.compile(r'"((?:\\.|[^"\\\n])*)"')
_ENTITY_RE = re.compile(r"@(?:Entity|Table)\b")
_TABLE_NAME_RE = re.compile(r'@Table\s*\([^)]*\bname\s*=\s*"([^"]+)"')


def _sql_targets(sql: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _SQL_VERB_RE.finditer(sql):
        verb = m.group(1).upper()
        tail = sql[m.end():]
        tm = _SQL_TABLE_RE[verb].search(tail)
        if tm:
            out.append((_VERB_KIND[verb], tm.group(1)))
    return out


def cmd_data_access(target: Path, out_stream) -> None:
    target = target.resolve()
    comp_id = _component_id(target)
    for _p, src, scan in _scan_all(target):
        emitted: set[str] = set()

        # JPA entities: @Entity / @Table(name=...) on a class header.
        for s in scan.symbols:
            if s.kind not in ("class", "record"):
                continue
            hdr = scan.class_annotics.get(s.qual, "")
            if not _ENTITY_RE.search(hdr):
                continue
            tm = _TABLE_NAME_RE.search(hdr)
            table = (tm.group(1) if tm else s.name).lower()
            ent_id = f"data:{table}"
            if ent_id not in emitted:
                emitted.add(ent_id)
                out_stream.write(_envelope("data_entity", {
                    "id": ent_id, "logical_name": table,
                    "component_id": comp_id, "schema": None,
                    "created_by": [_SOURCE_NAME], "certainty": "inferred",
                }) + "\n")

        # SQL string literals inside method bodies (@Query / raw SQL/JPQL).
        for s in scan.symbols:
            if s.kind != "method" or s.src_body_start < 0:
                continue
            body = src[s.src_body_start:s.src_body_end]
            for lm in _STR_RE.finditer(body):
                for kind, table in _sql_targets(lm.group(1)):
                    ent_id = f"data:{table.lower()}"
                    if ent_id not in emitted:
                        emitted.add(ent_id)
                        out_stream.write(_envelope("data_entity", {
                            "id": ent_id, "logical_name": table.lower(),
                            "component_id": comp_id, "schema": None,
                            "created_by": [_SOURCE_NAME], "certainty": "inferred",
                        }) + "\n")
                    out_stream.write(_envelope("edge", {
                        "source_id": s.id, "target_id": ent_id, "kind": kind,
                        "certainty": "inferred", "created_by": [_SOURCE_NAME],
                        "metadata": {"via": "sql_literal", "line": s.line},
                    }) + "\n")


def cmd_schema() -> dict[str, Any]:
    return {
        "language": "java",
        "version": _SOURCE_VERSION,
        "symbol_kinds": ["class", "interface", "enum", "record",
                         "annotation_type", "method"],
        "edge_kinds": ["CALLS", "EXPOSES", "READS", "WRITES"],
        "record_types": ["symbol", "edge", "contract", "data_entity"],
    }


# ─── CLI entry ─────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(
            "usage: ggoss-java "
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
