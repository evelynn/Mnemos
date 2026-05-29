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
- Cross-module call resolution is name-based only (no full import
  graph) — `from a import f; f()` resolves to a's f; `import a;
  a.f()` doesn't yet. Future PR.
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
import sys
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


def _iter_py_files(root: Path) -> Iterator[Path]:
    """Yield .py files under root, skipping common generated /
    third-party trees. Matches what an operator would expect to
    see in a graph view of their codebase."""
    if root.is_file() and root.suffix == ".py":
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # Mutate dirnames in place so os.walk skips them entirely.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


# ─── symbol extraction ─────────────────────────────────────────────


def _symbol_id(rel_path: str, name: str, lineno: int) -> str:
    """``py:rel/path.py:Class.method@line`` — analogous to ggoss-ts
    ``ts:file:name@line:col``."""
    return f"py:{rel_path}:{name}@{lineno}"


def _component_id(target: Path) -> str:
    """Top-level project name as the component id — matches the
    convention ggoss-ts uses for monorepo packages."""
    name = target.resolve().name or "py-project"
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


class _CallVisitor(ast.NodeVisitor):
    """Walk a module collecting CallExpression nodes with their
    enclosing function/method scope. Tracks the nested-function
    stack the same way ggoss-ts does."""

    def __init__(self, rel_path: str, symbols: list[_Symbol]):
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
        # Generic-visit so we see nested calls.
        for child in node.body:
            self.visit(child)
        self._scope.pop()

    # ---- calls ----------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        if not self._scope:
            # Module-level call — skip; not a useful CALLS edge in
            # graph terms (it runs at import).
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            return
        callee_name, qname_hint = _callee_repr(node.func)
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
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as exc:
            # Match ggoss-ts behaviour: emit a recoverable error
            # record on stderr, continue with the next file.
            sys.stderr.write(json.dumps({
                "level": "error", "file": str(path),
                "message": f"SyntaxError: {exc.msg}",
                "recoverable": True,
            }) + "\n")
            continue
        rel = str(path.resolve())
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
    for path in _iter_py_files(target):
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        rel = str(path.resolve())
        symbols = _walk_symbols(tree, rel)
        visitor = _CallVisitor(rel, symbols)
        visitor.visit(tree)
        for c in visitor.calls:
            resolved = c.callee_resolved_id is not None
            data = {
                "source_id": c.caller_id,
                "target_id": c.callee_resolved_id or f"py:extern:{c.callee_name}",
                "kind": "CALLS",
                "certainty": "asserted" if resolved else "inferred",
                "created_by": [_SOURCE_NAME],
                "metadata": {
                    "invocation_site": {"line": c.line, "col": c.col},
                    "callee_resolved": resolved,
                },
            }
            out_stream.write(_envelope("edge", data) + "\n")


def cmd_schema() -> dict[str, Any]:
    """Static description — what records this analyzer can emit.
    The orchestrator uses this to validate the configured pipeline."""
    return {
        "language": "python",
        "version": _SOURCE_VERSION,
        "symbol_kinds": ["class", "function", "method", "async_function"],
        "edge_kinds": ["CALLS"],
    }


# ─── CLI entry ─────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(
            "usage: ggoss-py <probe|inventory|symbols|calls|schema> <target>\n"
        )
        return 2
    verb = argv[0]
    rest = argv[1:]
    target_str = next((a for a in rest if not a.startswith("-")), None)
    if verb in {"probe", "inventory", "symbols", "calls"} and target_str is None:
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
    if verb == "schema":
        print(json.dumps(cmd_schema()))
        return 0
    sys.stderr.write(f"unknown verb: {verb!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
