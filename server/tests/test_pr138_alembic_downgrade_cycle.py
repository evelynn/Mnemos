"""PR-138 — alembic downgrade safety net.

Score-audit gap (security-ops agent): every migration has a downgrade
function, but there was zero test coverage of the integrity of those
functions. A bad downgrade only surfaces during a real rollback
(worst possible time). This test:

1. Forward-guards the chain (single head, no orphan revisions).
2. Statically verifies every downgrade body has at least one real
   statement (catches the ``pass`` / docstring-only regression).
3. AST-pairs every ``op.create_table`` in upgrade with a matching
   ``op.drop_table`` in downgrade, ditto for create_index / add_column
   / create_foreign_key. A missing pair = silent schema drift on
   rollback.

The full ``upgrade head → downgrade base → upgrade head`` round-trip
needs Postgres (migration 0001 issues ``CREATE EXTENSION pgcrypto``
which has no SQLite equivalent). That round-trip is owned by CI
(.github/workflows/ci.yml runs it on every push) — the static AST
check above is the laptop-runnable complement that catches typos
before the contributor hits push.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SERVER_ROOT = Path(__file__).resolve().parents[1]
_VDIR = _SERVER_ROOT / "alembic" / "versions"


def _migrations() -> list[Path]:
    return sorted(p for p in _VDIR.glob("*.py") if not p.name.startswith("__"))


def _fn(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    for n in ast.iter_child_nodes(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _op_calls(fn: ast.FunctionDef) -> list[str]:
    """Return every ``op.<verb>`` call name inside the function body."""
    out: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name) and f.value.id == "op"
        ):
            out.append(f.attr)
    return out


# ─── chain integrity ─────────────────────────────────────────────


def test_single_head_no_branch():
    """Multi-head migration chains silently break ``upgrade head``.
    Force-fail if anyone introduces a branch without a merge."""
    rev_re = re.compile(r'^revision[^=]*=\s*["\']([^"\']+)["\']', re.MULTILINE)
    down_re = re.compile(
        r'^down_revision[^=]*=\s*["\']([^"\']*)["\']', re.MULTILINE
    )
    rev_to_file: dict[str, str] = {}
    down_to_files: dict[str, list[str]] = {}
    for f in _migrations():
        src = f.read_text(encoding="utf-8")
        rm = rev_re.search(src)
        dm = down_re.search(src)
        if rm:
            rev_to_file[rm.group(1)] = f.name
        if dm and dm.group(1):
            down_to_files.setdefault(dm.group(1), []).append(f.name)
    heads = [r for r in rev_to_file if r not in down_to_files]
    assert len(heads) == 1, (
        f"alembic has {len(heads)} heads — expected 1: {heads}"
    )
    # No fork: every revision that's referenced as down_revision
    # appears at most once as such.
    for rev, files in down_to_files.items():
        assert len(files) == 1, (
            f"revision {rev!r} is the down_revision of {len(files)} "
            f"migrations (would create a fork): {files}"
        )


# ─── downgrade body integrity ────────────────────────────────────


def test_each_migration_has_a_real_downgrade():
    """Forward-guard against an empty / pass-only downgrade body.

    Pre-PR-138 every file had ``def downgrade`` but a future
    migration might land with ``pass`` or just comment text. Catch
    that here so the round-trip stays meaningful.
    """
    for f in _migrations():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        downgrade = _fn(tree, "downgrade")
        assert downgrade is not None, f"{f.name}: missing downgrade()"
        body = list(downgrade.body)
        # Strip docstring if present.
        if (
            body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        real_stmts = [n for n in body if not isinstance(n, ast.Pass)]
        assert real_stmts, (
            f"{f.name}: downgrade() body is empty / Pass-only — "
            "rollback would be a no-op, leaving the schema upgraded "
            "but version stamped base."
        )


# ─── op-pair integrity (the real catch) ──────────────────────────


# Each entry: an upgrade-op name → the downgrade-op name that
# reverses it. The set is intentionally narrow — broader ops like
# op.execute / op.alter_column are too freeform to pair generically.
_REVERSE = {
    "create_table":         "drop_table",
    "create_index":         "drop_index",
    "create_foreign_key":   "drop_constraint",
    "create_unique_constraint": "drop_constraint",
    "create_check_constraint":  "drop_constraint",
    "add_column":           "drop_column",
}


def test_every_upgrade_op_has_a_matching_downgrade_op():
    """For each ``op.create_X`` in upgrade, downgrade must have at
    least one matching ``op.drop_X`` (or ``op.drop_constraint``).
    Misses are common when a migration adds an index inline with a
    column and forgets to drop it. SQLite hides this (the next CI
    run is fine), Postgres surfaces it as ``relation "x" already
    exists`` on the next rollback-then-re-upgrade.
    """
    failures: list[str] = []
    for f in _migrations():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        up = _fn(tree, "upgrade")
        down = _fn(tree, "downgrade")
        if up is None or down is None:
            continue
        up_ops = _op_calls(up)
        down_ops = set(_op_calls(down))
        for op_name in up_ops:
            target = _REVERSE.get(op_name)
            if not target:
                continue
            if target not in down_ops:
                # Acceptable escape hatches:
                # - op.execute (handcrafted SQL — author judgement)
                # - op.batch_alter_table (SQLite batched ops)
                if "execute" in down_ops or "batch_alter_table" in down_ops:
                    continue
                failures.append(
                    f"{f.name}: upgrade has op.{op_name}() but "
                    f"downgrade has no op.{target}()"
                )
    assert not failures, "downgrade missing reverse op:\n  " + \
        "\n  ".join(failures)
