"""PR-115 — ggoss-py: Python analyzer.

Mnemos's analyzer set was C#/TS/MSSQL/Oracle/.NET — covering the
.NET stack and SQL backends but missing the most common modern
backend language. Adding Python:

1. Closes the most-requested gap (Python ~30% of modern back-end LOC)
2. Enables true dogfood — Mnemos can now analyse its own
   ``server/app/`` codebase
3. Uses stdlib ``ast`` only — zero new dependencies, satisfies
   §2.10 1-operator friendly

Protocol matches ggoss-ts:
    ggoss-py probe|inventory|symbols|calls|schema <target>

Output: JSONL with ``record_type`` envelope. Same shape as
ggoss-ts so analyzers/registry.py + server-side merge code
treats it identically.

Symbol kinds emitted:
- ``class``      — class Foo:
- ``function``   — def foo(...):  at module scope
- ``method``     — def foo(self, ...): inside a class
- ``async_function`` — async def foo(...):

Edge kinds emitted:
- ``CALLS``      — caller_id → callee_id; resolved=True for intra-
                   module calls, False for stdlib/3rd-party.

Limitations (documented honesty, see docs/analyzer-contract.md):
- Cross-module call resolution is name-based (no full import graph): a
  callee unresolved within its own file resolves to a *unique* project-wide
  symbol of that bare name (PR-198). Ambiguous names (2+ project-wide) stay
  ``extern`` rather than guessing — precision over recall. Import-aware
  disambiguation of same-named cross-module symbols is future work.
- No type inference; method binding falls back to receiver-name
  matching.
- Decorators that wrap functions (Click, FastAPI routes) are
  visible via the symbol's signature text but not modelled as
  separate entities.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

_SOURCE_NAME = "ggoss-py"
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


_SKIP_DIRS = {
    "__pycache__", ".venv", "venv", "env", ".git", "node_modules",
    "build", "dist", ".pytest_cache", ".tox", ".eggs",
    "site-packages",
}


def _is_link_or_junction(path: Path) -> bool:
    """Return true for POSIX links and Windows directory junctions."""

    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        # An unreadable/replaced entry is not safe to traverse.
        return True


def _iter_py_files(root: Path) -> Iterator[Path]:
    """Yield .py files under root, skipping common generated /
    third-party trees. Matches what an operator would expect to
    see in a graph view of their codebase."""
    if _is_link_or_junction(root):
        return
    if root.is_file() and root.suffix == ".py":
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # Mutate dirnames in place so os.walk skips them entirely.
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
            and not _is_link_or_junction(Path(dirpath) / d)
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if name.endswith(".py") and not _is_link_or_junction(path):
                yield path


def _parse_python_source(source: str, *, filename: str) -> ast.Module:
    """Parse without leaking non-fatal compiler warnings to JSONL stderr.

    Invalid-escape warnings do not make a file unparsable, but supported
    Python versions can emit them while building the AST.  Analyzer stderr is
    a machine channel, so an unstructured warning there is interpreted as a
    coverage failure.  Real ``SyntaxError`` exceptions remain visible to each
    verb's existing diagnostic policy.
    """

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        return ast.parse(source, filename=filename)


# ─── symbol extraction ─────────────────────────────────────────────


def _symbol_id(rel_path: str, name: str, lineno: int) -> str:
    """``py:rel/path.py:Class.method@line`` — analogous to ggoss-ts
    ``ts:file:name@line:col``."""
    return f"py:{rel_path}:{name}@{lineno}"


def _component_id(target: Path) -> str:
    """Top-level project name as the component id — matches the
    convention ggoss-ts uses for monorepo packages."""
    name = os.environ.get("MNEMOS_PROJECT_ID") or target.resolve().name or "py-project"
    return f"py.{name}"


@dataclass
class _Symbol:
    id: str
    kind: str
    name: str
    qual_name: str
    file: str
    line: int


def _walk_symbols(tree: ast.Module, rel_path: str) -> list[_Symbol]:
    """Walk the AST once collecting class/function/method symbols.
    Visits nested classes / methods correctly."""
    out: list[_Symbol] = []

    def visit(node: ast.AST, qual_prefix: str, inside_class: bool) -> None:
        if isinstance(node, ast.ClassDef):
            qname = f"{qual_prefix}{node.name}"
            out.append(_Symbol(
                id=_symbol_id(rel_path, qname, node.lineno),
                kind="class", name=node.name, qual_name=qname,
                file=rel_path, line=node.lineno,
            ))
            for child in node.body:
                visit(child, qname + ".", inside_class=True)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "method" if inside_class else (
                "async_function" if isinstance(node, ast.AsyncFunctionDef)
                else "function"
            )
            qname = f"{qual_prefix}{node.name}"
            out.append(_Symbol(
                id=_symbol_id(rel_path, qname, node.lineno),
                kind=kind, name=node.name, qual_name=qname,
                file=rel_path, line=node.lineno,
            ))
            # Don't descend into a nested function's body for further
            # *symbol* extraction — that's the typical Python convention
            # (closures aren't first-class symbols). But DO descend
            # for nested class defs.
            for child in node.body:
                if isinstance(child, ast.ClassDef):
                    visit(child, qname + ".", inside_class=False)
            return
        # Generic recursion for module-level bodies.
        for child in ast.iter_child_nodes(node):
            visit(child, qual_prefix, inside_class)

    if isinstance(tree, ast.Module):
        for stmt in tree.body:
            visit(stmt, "", inside_class=False)
    return out


def _signature(src: str, node: ast.AST) -> str:
    """First line of the def — captures signature without dragging
    the body in. Mirrors ggoss-ts's truncated-to-400 signature."""
    try:
        seg = ast.get_source_segment(src, node) or ""
    except Exception:  # noqa: BLE001
        seg = ""
    return seg.splitlines()[0][:400] if seg else ""


# ─── call extraction ───────────────────────────────────────────────


@dataclass
class _Call:
    caller_id: str
    callee_name: str           # for display
    callee_resolved_id: str | None  # None if not resolved in-module
    line: int
    col: int
    resolution: str | None = None   # how it resolved (e.g. "receiver_type")


def _type_from_value(node: ast.AST, project_classes: set[str]) -> str | None:
    """Infer a class name from an assignment RHS: ``ClassName(...)`` → the
    class (PR-203 type inference). Only project classes count."""
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id in project_classes:
            return f.id
        if isinstance(f, ast.Attribute) and f.attr in project_classes:
            return f.attr
    return None


def _type_from_annotation(node: ast.AST | None, project_classes: set[str]) -> str | None:
    """Infer a class name from a type annotation: ``repo: Repo`` /
    ``x: Optional[Repo]`` → ``Repo``."""
    if node is None:
        return None
    if isinstance(node, ast.Name) and node.id in project_classes:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in project_classes:
        return node.attr
    if isinstance(node, ast.Subscript):  # Optional[X], list[X] …
        return _type_from_annotation(node.slice, project_classes)
    return None


def _class_attr_types(
    tree: ast.Module, project_classes: set[str]
) -> dict[str, dict[str, str]]:
    """``{class name → {attr → type}}`` from ``self.x = Type()`` /
    ``self.x: Type`` / constructor-injected ``def __init__(self, x: Type):
    self.x = x``. The dominant Python DI pattern."""
    out: dict[str, dict[str, str]] = {}

    def scan_class(cls: ast.ClassDef) -> None:
        attrs: dict[str, str] = {}
        for item in cls.body:
            # class-body annotation: ``repo: Repo``
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                t = _type_from_annotation(item.annotation, project_classes)
                if t:
                    attrs[item.target.id] = t
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                param_types = {
                    a.arg: _type_from_annotation(a.annotation, project_classes)
                    for a in item.args.args
                }
                for sub in ast.walk(item):
                    # ``self.x = Type()`` or ``self.x = param`` (injected).
                    if isinstance(sub, ast.Assign):
                        for tgt in sub.targets:
                            if (isinstance(tgt, ast.Attribute)
                                    and isinstance(tgt.value, ast.Name)
                                    and tgt.value.id == "self"):
                                t = _type_from_value(sub.value, project_classes)
                                if not t and isinstance(sub.value, ast.Name):
                                    t = param_types.get(sub.value.id)
                                if t:
                                    attrs[tgt.attr] = t
                    elif (isinstance(sub, ast.AnnAssign)
                          and isinstance(sub.target, ast.Attribute)
                          and isinstance(sub.target.value, ast.Name)
                          and sub.target.value.id == "self"):
                        t = _type_from_annotation(sub.annotation, project_classes)
                        if t:
                            attrs[sub.target.attr] = t
        if attrs:
            out[cls.name] = attrs

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            scan_class(node)
    return out


class _CallVisitor(ast.NodeVisitor):
    """Walk a module collecting CallExpression nodes with their
    enclosing function/method scope. Tracks the nested-function
    stack the same way ggoss-ts does."""

    def __init__(
        self,
        rel_path: str,
        symbols: list[_Symbol],
        class_methods: dict[str, dict[str, str]] | None = None,
        class_attrs: dict[str, dict[str, str]] | None = None,
        project_classes: set[str] | None = None,
    ):
        self._rel = rel_path
        self.calls: list[_Call] = []
        # qual_name → symbol id, for in-module resolution.
        self._qname_to_id = {s.qual_name: s.id for s in symbols}
        # bare name → list of ids (collisions possible across classes)
        self._name_to_ids: dict[str, list[str]] = {}
        for s in symbols:
            self._name_to_ids.setdefault(s.name, []).append(s.id)
        self._scope: list[str] = []  # qual_name stack
        self._class_stack: list[str] = []
        # PR-203 — type-aware resolution maps (project-wide).
        self._class_methods = class_methods or {}
        self._class_attrs = class_attrs or {}
        self._project_classes = project_classes or set()
        self._local_types: list[dict[str, str]] = []  # per-function var → class

    # ---- scope ----------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_fn(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_fn(node)

    def _visit_fn(self, node) -> None:
        qname = ".".join(self._class_stack + [node.name])
        self._scope.append(qname)
        # PR-203 — local type env: param annotations + ``x = Type()`` /
        # ``x: Type`` in the body. Drives receiver-type call resolution.
        local: dict[str, str] = {}
        for a in list(node.args.args) + list(node.args.kwonlyargs):
            t = _type_from_annotation(a.annotation, self._project_classes)
            if t:
                local[a.arg] = t
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                t = _type_from_value(sub.value, self._project_classes)
                if t:
                    for tgt in sub.targets:
                        if isinstance(tgt, ast.Name):
                            local[tgt.id] = t
            elif (isinstance(sub, ast.AnnAssign)
                  and isinstance(sub.target, ast.Name)):
                t = _type_from_annotation(sub.annotation, self._project_classes)
                if t:
                    local[sub.target.id] = t
        self._local_types.append(local)
        for child in node.body:
            self.visit(child)
        self._local_types.pop()
        self._scope.pop()

    def _resolve_by_type(self, func: ast.expr) -> str | None:
        """Type-aware resolution of ``recv.method()``: if ``recv``'s class is
        known (local var / param / ``self.attr``), resolve the method in that
        class. This is what disambiguates same-named methods (PR-203)."""
        if not isinstance(func, ast.Attribute):
            return None
        method = func.attr
        recv = func.value
        rtype: str | None = None
        if isinstance(recv, ast.Name):
            for env in reversed(self._local_types):
                if recv.id in env:
                    rtype = env[recv.id]
                    break
        elif (isinstance(recv, ast.Attribute)
              and isinstance(recv.value, ast.Name)
              and recv.value.id == "self" and self._class_stack):
            rtype = self._class_attrs.get(self._class_stack[-1], {}).get(recv.attr)
        if rtype:
            return self._class_methods.get(rtype, {}).get(method)
        return None

    # ---- calls ----------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        if not self._scope:
            # Module-level call — skip; not a useful CALLS edge in
            # graph terms (it runs at import).
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            return
        callee_name, qname_hint = _callee_repr(node.func)
        # PR-203 — receiver-type resolution first (disambiguates same-named
        # methods), then the name-based ladder.
        resolution = None
        resolved_id = self._resolve_by_type(node.func)
        if resolved_id is not None:
            resolution = "receiver_type"
        else:
            resolved_id = self._resolve(callee_name, qname_hint)
        caller_qname = self._scope[-1]
        caller_id = self._qname_to_id.get(caller_qname)
        if caller_id is None:
            # Shouldn't happen if symbol walk + call walk see the
            # same AST, but defensive.
            return
        self.calls.append(_Call(
            caller_id=caller_id,
            callee_name=callee_name,
            callee_resolved_id=resolved_id,
            line=node.lineno,
            col=node.col_offset + 1,
            resolution=resolution,
        ))
        # Descend into args (could contain further calls).
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def _resolve(self, bare_name: str, qname_hint: str | None) -> str | None:
        """Try qual_name → bare_name match against the symbol map.

        Returns None when there's no unique in-module symbol. Accepts
        these display-name shapes (mapped from the AST by
        _callee_repr): ``foo``, ``foo.bar``, ``self.bar``, ``a.b.c``.
        For attribute chains we strip down to the last segment and
        try that as the bare name — that's where most intra-module
        method resolution lives (``repo.add()`` → method ``add``).
        Pre-fix this returned None for every attribute call,
        tanking the accuracy harness's edge-recall metric.
        """
        if qname_hint and qname_hint in self._qname_to_id:
            return self._qname_to_id[qname_hint]
        # Strip ``self.`` or ``cls.`` so methods called on self
        # resolve like bare method names. Also strip a single
        # receiver prefix so ``repo.add`` tries bare ``add``.
        candidate = bare_name
        if "." in candidate:
            candidate = candidate.rsplit(".", 1)[-1]
        ids = self._name_to_ids.get(candidate, [])
        if len(ids) == 1:
            return ids[0]
        # Ambiguous (multiple symbols share this bare name) — leave
        # unresolved rather than guessing wrong.
        return None


def _callee_repr(func: ast.expr) -> tuple[str, str | None]:
    """Return (display_name, fully_qualified_hint).

    The qual hint helps disambiguate ``Class.method`` calls. The
    display name is what ggoss-ts puts in the edge envelope.
    """
    if isinstance(func, ast.Name):
        return func.id, None
    if isinstance(func, ast.Attribute):
        # foo.bar.baz() — display "foo.bar.baz", hint "bar.baz"
        # (last two segments often map to Class.method).
        parts: list[str] = []
        n: ast.expr = func
        while isinstance(n, ast.Attribute):
            parts.append(n.attr)
            n = n.value
        if isinstance(n, ast.Name):
            parts.append(n.id)
        parts.reverse()
        display = ".".join(parts)
        # qname hint = last two segments if attribute chain;
        # plain method on self → just method name.
        hint = None
        if len(parts) >= 2 and parts[0] != "self":
            hint = ".".join(parts[-2:])
        return display, hint
    # Subscript, Call (chained), Lambda — fall back to AST text.
    try:
        return ast.unparse(func)[:80], None
    except Exception:  # noqa: BLE001
        return "<expr>", None


# ─── verbs ────────────────────────────────────────────────────────


def cmd_probe(target: Path) -> dict[str, Any]:
    """Cheap viability check — count .py files."""
    n = sum(1 for _ in _iter_py_files(target))
    return {"ok": n > 0, "language": "python", "py_files": n}


def cmd_inventory(target: Path) -> dict[str, Any]:
    """Top-level summary used by the orchestrator to decide whether
    to schedule deeper passes."""
    files = list(_iter_py_files(target))
    total_lines = 0
    for f in files:
        try:
            total_lines += sum(1 for _ in f.open("r", encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return {
        "language": "python",
        "py_files": len(files),
        "total_lines": total_lines,
    }


def cmd_symbols(target: Path, out_stream) -> None:
    target = target.resolve()
    comp_id = _component_id(target)
    for path in _iter_py_files(target):
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = _parse_python_source(src, filename=str(path))
        except SyntaxError as exc:
            # Match ggoss-ts behaviour: emit a recoverable error
            # record on stderr, continue with the next file.
            sys.stderr.write(json.dumps({
                "level": "error", "file": str(path),
                "message": f"SyntaxError: {exc.msg}",
                "recoverable": True,
            }) + "\n")
            continue
        rel = path.resolve().relative_to(target).as_posix()
        symbols = _walk_symbols(tree, rel)
        for s in symbols:
            out_stream.write(_envelope("symbol", {
                "id": s.id,
                "kind": s.kind,
                "name": s.name,
                "qual_name": s.qual_name,
                "component_id": comp_id,
                "signature": _signature(src, _find_node_at(tree, s.line)),
                "location": {"file": s.file, "line": s.line, "col": 1},
                "visibility": "private" if s.name.startswith("_") else "public",
                "is_entry_point": s.qual_name in {"main", "__main__"},
                "metadata": {},
                "certainty": "asserted",
                "created_by": [_SOURCE_NAME],
            }) + "\n")


def _find_node_at(tree: ast.Module, line: int) -> ast.AST:
    """Look up a def/class node by line — used for signature
    extraction. Falls back to the module if no match."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.lineno == line:
            return node
    return tree


def cmd_calls(target: Path, out_stream) -> None:
    target = target.resolve()
    # Pass 1 — parse every module, collect its calls, and build a project-wide
    # bare-name → symbol-id map. Pre-fix, ``_CallVisitor`` only knew the
    # *current file's* symbols, so a call to a function/class defined in
    # another module degraded to ``py:extern`` (the documented cross-module
    # gap). The global map lets an unresolved-in-file callee resolve to a
    # unique project-wide symbol of that name.
    files: list[tuple[str, ast.Module, list[_Symbol]]] = []
    global_by_name: dict[str, set[str]] = {}
    project_classes: set[str] = set()
    # class simple-name → {method → id} (receiver-type resolution, PR-203).
    class_methods: dict[str, dict[str, str]] = {}
    for path in _iter_py_files(target):
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = _parse_python_source(src, filename=str(path))
        except SyntaxError:
            continue
        rel = path.resolve().relative_to(target).as_posix()
        symbols = _walk_symbols(tree, rel)
        files.append((rel, tree, symbols))
        for s in symbols:
            global_by_name.setdefault(s.name, set()).add(s.id)
            if s.kind == "class":
                project_classes.add(s.name)
            if s.kind in ("method",) and "." in s.qual_name:
                cls = s.qual_name.rsplit(".", 1)[0].rsplit(".", 1)[-1]
                class_methods.setdefault(cls, {}).setdefault(s.name, s.id)

    # class attribute types need the full project-class set first.
    class_attrs: dict[str, dict[str, str]] = {}
    for _rel, tree, _symbols in files:
        for cls, attrs in _class_attr_types(tree, project_classes).items():
            class_attrs.setdefault(cls, {}).update(attrs)

    # Pass 2 — run each visitor with the project-wide type maps, then emit.
    # Cross-module name fallback is precision-safe (unique-name only).
    parsed: list[tuple[str, list[_Call]]] = []
    for rel, tree, symbols in files:
        visitor = _CallVisitor(rel, symbols, class_methods, class_attrs,
                               project_classes)
        visitor.visit(tree)
        parsed.append((rel, visitor.calls))

    for _rel, calls in parsed:
        for c in calls:
            resolved_id = c.callee_resolved_id
            resolution = c.resolution
            if resolved_id is None:
                bare = c.callee_name.rsplit(".", 1)[-1]
                ids = global_by_name.get(bare, set())
                if len(ids) == 1:
                    resolved_id = next(iter(ids))
                    resolution = "cross_module"
            resolved = resolved_id is not None
            meta: dict[str, Any] = {
                "invocation_site": {"line": c.line, "col": c.col},
                "callee_resolved": resolved,
            }
            if resolution:
                meta["resolution"] = resolution
            data = {
                "source_id": c.caller_id,
                "target_id": resolved_id or f"py:extern:{c.callee_name}",
                "kind": "CALLS",
                "certainty": "asserted" if resolved else "inferred",
                "created_by": [_SOURCE_NAME],
                "metadata": meta,
            }
            out_stream.write(_envelope("edge", data) + "\n")


# ─── contracts (HTTP routes) ─────────────────────────────────────
#
# PR-137 — Python equivalent of ggoss-ts's NestJS decorator scan.
# Detects FastAPI / Flask / Starlette / aiohttp / Sanic / Django
# REST framework routes from decorators on the source side, and
# ``requests`` / ``httpx`` calls from the client side. Each EXPOSES /
# CALLS edge connects a symbol to a contract node.

_HTTP_DECO_NAMES = {
    "get", "post", "put", "delete", "patch", "head", "options",
    "route",   # Flask: @app.route("/x", methods=["GET"])
}


def _string_literal(node: ast.expr) -> str | None:
    """Return the literal value of a Constant string (Python 3.8+).
    Returns None for f-strings / dynamic expressions — we don't
    pretend to evaluate them."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _deco_route(deco: ast.expr) -> tuple[str, str] | None:
    """Inspect a decorator AST node and, if it looks like
    ``@<router>.<verb>("/path")`` (FastAPI/Starlette/Sanic/aiohttp) or
    ``@app.route("/path", methods=["POST"])`` (Flask), return
    ``(http_method, route)``. Bound to known verb names only — avoids
    flagging e.g. ``@functools.wraps``.
    """
    # @something(...) — must be a Call, otherwise no path arg.
    if not isinstance(deco, ast.Call):
        return None
    func = deco.func
    if not isinstance(func, ast.Attribute):
        return None
    verb = func.attr.lower()
    if verb not in _HTTP_DECO_NAMES:
        return None
    if not deco.args:
        return None
    path = _string_literal(deco.args[0])
    if not path:
        return None
    # @app.route("/x", methods=["POST"]) — pick the first listed
    # method, default GET.
    if verb == "route":
        method = "GET"
        for kw in deco.keywords:
            if kw.arg == "methods":
                m_node = kw.value
                if isinstance(m_node, (ast.List, ast.Tuple)) and m_node.elts:
                    m = _string_literal(m_node.elts[0])
                    if m:
                        method = m.upper()
                break
        return method, path
    return verb.upper(), path


def cmd_contracts(target: Path, out_stream) -> None:
    """Stream ``record_type=contract`` + the EXPOSES edges that
    connect them to the function/method symbol carrying the route."""
    target = target.resolve()
    comp_id = _component_id(target)
    for path in _iter_py_files(target):
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = _parse_python_source(src, filename=str(path))
        except SyntaxError:
            continue
        rel = path.resolve().relative_to(target).as_posix()
        symbols = _walk_symbols(tree, rel)
        sym_by_line: dict[int, _Symbol] = {s.line: s for s in symbols}

        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            for deco in getattr(node, "decorator_list", []):
                hit = _deco_route(deco)
                if not hit:
                    continue
                method, route = hit
                sym = sym_by_line.get(node.lineno)
                if not sym:
                    continue
                contract_id = f"contract:http:{method}:{route}"
                out_stream.write(_envelope("contract", {
                    "id": contract_id,
                    "kind": "http",
                    "method": method,
                    "path": route,
                    "component_id": comp_id,
                    "location": {"file": rel, "line": deco.lineno, "col": 1},
                    "created_by": [_SOURCE_NAME],
                    "certainty": "asserted",
                }) + "\n")
                out_stream.write(_envelope("edge", {
                    "source_id": sym.id,
                    "target_id": contract_id,
                    "kind": "EXPOSES",
                    "certainty": "asserted",
                    "created_by": [_SOURCE_NAME],
                    "metadata": {
                        "framework": _deco_framework(deco),
                        "line": deco.lineno,
                    },
                }) + "\n")


def _deco_framework(deco: ast.expr) -> str:
    """Best-effort framework hint from the receiver name (``app`` /
    ``router`` / ``api``) — not authoritative, just useful metadata."""
    if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
        recv = deco.func.value
        if isinstance(recv, ast.Name):
            return recv.id
    return "unknown"


# ─── data_access (READS/WRITES) ──────────────────────────────────
#
# Minimal but honest detector. Two patterns:
#
#  1. ``session.execute(text("SELECT/INSERT/UPDATE/DELETE FROM t ..."))``
#     — common SQLAlchemy raw-SQL shape. The verb decides READS vs
#     WRITES; the table is the first identifier after FROM/INTO/UPDATE.
#
#  2. ``session.query(Model)`` / ``select(Model)`` — Model name is the
#     logical entity. READS-only.
#
# Limits intentionally documented per analyzer-contract.md:
# - No type inference; Model in select(Model) is taken at face value.
# - Schema-qualification falls to the merge layer (DB analyzers emit
#   schema-qualified entities; this one emits ``data.<table>``).

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


def _sql_targets(sql: str) -> list[tuple[str, str]]:
    """Return ``[(kind, table_name), ...]`` from a raw SQL string."""
    out: list[tuple[str, str]] = []
    for m in _SQL_VERB_RE.finditer(sql):
        verb = m.group(1).upper()
        kind = _VERB_TO_KIND[verb]
        tail = sql[m.end():]
        tm = _SQL_TABLE_RE[verb].search(tail)
        if tm:
            out.append((kind, tm.group(1)))
    return out


def _fns_with_qname(tree: ast.Module) -> Iterator[tuple[Any, str]]:
    """Yield ``(fn_node, qual_name)`` so a nested class's methods get
    the ``Class.method`` qname (matching ``_walk_symbols``)."""
    def walk(node: ast.AST, stack: list[str]) -> Iterator:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from walk(child, stack + [child.name])
            elif isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                qname = ".".join(stack + [child.name])
                yield child, qname
                # Nested defs inside a function are unusual but
                # legal — recurse so they're not lost.
                yield from walk(child, stack + [child.name])
            else:
                yield from walk(child, stack)
    yield from walk(tree, [])


def cmd_data_access(target: Path, out_stream) -> None:
    """Stream READS/WRITES edges and the data_entity nodes they
    reference. Emits one ``data_entity`` per distinct table the file
    touches (deduped within a file)."""
    target = target.resolve()
    comp_id = _component_id(target)
    for path in _iter_py_files(target):
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = _parse_python_source(src, filename=str(path))
        except SyntaxError:
            continue
        rel = path.resolve().relative_to(target).as_posix()
        symbols = _walk_symbols(tree, rel)
        sym_by_qname: dict[str, _Symbol] = {s.qual_name: s for s in symbols}
        emitted_entities: set[str] = set()

        # Walk every Call node within a function/method scope.
        for fn, qname in _fns_with_qname(tree):
            sym = sym_by_qname.get(qname)
            if not sym:
                continue
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                # Pattern A: text("SQL") or execute("SQL"). The SQL
                # may sit as the first arg or inside a text() wrapper.
                sql_blob: str | None = None
                if call.args:
                    a0 = call.args[0]
                    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        sql_blob = a0.value
                    elif (
                        isinstance(a0, ast.Call) and
                        isinstance(a0.func, ast.Name) and
                        a0.func.id == "text" and a0.args
                    ):
                        nested = a0.args[0]
                        if isinstance(nested, ast.Constant) \
                                and isinstance(nested.value, str):
                            sql_blob = nested.value
                if sql_blob:
                    for kind, table in _sql_targets(sql_blob):
                        ent_id = f"data:{table.lower()}"
                        if ent_id not in emitted_entities:
                            emitted_entities.add(ent_id)
                            out_stream.write(_envelope("data_entity", {
                                "id": ent_id,
                                "logical_name": table.lower(),
                                "component_id": comp_id,
                                "schema": None,
                                "created_by": [_SOURCE_NAME],
                                "certainty": "inferred",
                            }) + "\n")
                        out_stream.write(_envelope("edge", {
                            "source_id": sym.id,
                            "target_id": ent_id,
                            "kind": kind,
                            "certainty": "inferred",
                            "created_by": [_SOURCE_NAME],
                            "metadata": {
                                "via": "raw_sql",
                                "line": call.lineno,
                            },
                        }) + "\n")

                # Pattern B: session.query(Model) / select(Model).
                f = call.func
                method_name = None
                if isinstance(f, ast.Attribute):
                    method_name = f.attr
                elif isinstance(f, ast.Name):
                    method_name = f.id
                if method_name in {"query", "select"} and call.args:
                    arg0 = call.args[0]
                    if isinstance(arg0, ast.Name):
                        model = arg0.id
                        ent_id = f"data:{model.lower()}"
                        if ent_id not in emitted_entities:
                            emitted_entities.add(ent_id)
                            out_stream.write(_envelope("data_entity", {
                                "id": ent_id,
                                "logical_name": model.lower(),
                                "component_id": comp_id,
                                "schema": None,
                                "created_by": [_SOURCE_NAME],
                                "certainty": "inferred",
                            }) + "\n")
                        out_stream.write(_envelope("edge", {
                            "source_id": sym.id,
                            "target_id": ent_id,
                            "kind": "READS",
                            "certainty": "inferred",
                            "created_by": [_SOURCE_NAME],
                            "metadata": {
                                "via": f"orm_{method_name}",
                                "line": call.lineno,
                            },
                        }) + "\n")


def cmd_schema() -> dict[str, Any]:
    """Static description — what records this analyzer can emit.
    The orchestrator uses this to validate the configured pipeline."""
    return {
        "language": "python",
        "version": _SOURCE_VERSION,
        "symbol_kinds": ["class", "function", "method", "async_function"],
        "edge_kinds": ["CALLS", "EXPOSES", "READS", "WRITES"],
        "record_types": ["symbol", "edge", "contract", "data_entity"],
    }


# ─── CLI entry ─────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(
            "usage: ggoss-py "
            "<probe|inventory|symbols|calls|contracts|data_access|schema> "
            "<target>\n"
        )
        return 2
    verb = argv[0]
    rest = argv[1:]
    target_str = next((a for a in rest if not a.startswith("-")), None)
    if verb in {
        "probe", "inventory", "symbols", "calls",
        "contracts", "data_access",
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
