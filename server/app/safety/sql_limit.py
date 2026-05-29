"""SQLGlot-backed row-cap and write-block gate for ``/data/query``.

Spec §2.8 mandates that data exposure stays minimal: every operator
query against a project DB lands with a hard upper bound on rows, and
no statement that could mutate state is ever forwarded. The analyzer
also caps `fetchmany`, but a single layer of defence is one too few —
this module is the platform-side enforcement point.

Design choices (reflecting Team B's critique):

* The parser is :mod:`sqlglot`. Regex pre-screening of "starts with
  SELECT" is not enough: ``WITH foo AS (UPDATE ...) SELECT * FROM foo``
  is a legitimate-looking SELECT that mutates state on Postgres. The
  AST walk catches every flavour.
* Write kinds (``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``, ``CREATE``,
  ``ALTER``, ``DROP``, ``TRUNCATE``) are rejected outright.
* A missing ``LIMIT`` (or its dialect-specific equivalent) is filled in
  at the AST level so the analyzer never sees an unbounded query.
* A ``LIMIT`` already in the query gets *clamped* — not rejected —
  because a hard refusal makes a useful interactive UX worse without
  buying any safety. The response surfaces ``limit_clamped_from`` so
  operators see we trimmed their request.
* Operations that do not have a natural LIMIT (window functions over
  the full result, UNION at the top level, etc.) get an outer LIMIT
  on a wrapping subquery — SQLGlot does this for us.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp


# Top-level statements we refuse to forward to the analyzer, no matter
# how the query is wrapped. SQLGlot exposes one parse-tree class per
# statement; checking against the set is a safe over-approximation.
_FORBIDDEN_STMTS: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Alter,
    exp.Drop,
    exp.TruncateTable,
)

# Dialect identifiers SQLGlot accepts. Maps the kind we carry on
# ProjectDB rows to what SQLGlot wants on the read/write side.
_DIALECT_MAP = {
    "mssql": "tsql",
    "oracle": "oracle",
    "postgres": "postgres",
    "mysql": "mysql",
    "snowflake": "snowflake",
}

DEFAULT_MAX_ROWS = 10_000


@dataclass
class EnforceResult:
    sql: str
    """The rewritten SQL, ready to send to the analyzer."""

    clamp: Literal["none", "added", "clamped"]
    """How the row cap was applied.

    * ``none`` — the original LIMIT was already within bounds.
    * ``added`` — no LIMIT was present; we added one.
    * ``clamped`` — the original LIMIT exceeded max_rows; we clamped it
      and the response carries the original value for transparency.
    """

    original_limit: int | None
    """The LIMIT the operator typed, if any."""

    effective_limit: int
    """The LIMIT the analyzer will see."""


class WriteStatementBlocked(ValueError):
    """Raised when the query contains a write-capable statement."""

    def __init__(self, kind: str):
        super().__init__(f"write_kind_not_allowed: {kind}")
        self.kind = kind


def _resolve_dialect(kind: str) -> str:
    return _DIALECT_MAP.get(kind, kind)


def _statement_kind(tree: exp.Expression) -> str | None:
    """Return the human name of any forbidden top-level statement."""
    for forbidden in _FORBIDDEN_STMTS:
        if isinstance(tree, forbidden):
            return forbidden.__name__.lower()
        # Walk into CTE / wrappers — a SELECT shell over an INSERT
        # still mutates state.
        for node in tree.walk():
            if isinstance(node, forbidden):
                return forbidden.__name__.lower()
    return None


def _read_limit(tree: exp.Expression) -> int | None:
    """Return the top-level integer LIMIT, or None if absent / dynamic."""
    limit_arg = tree.args.get("limit") if hasattr(tree, "args") else None
    if limit_arg is None:
        return None
    expr = getattr(limit_arg, "expression", None) or getattr(limit_arg, "this", None)
    # Literal.number(123).this → "123" (string). Variables / params → not int.
    try:
        return int(getattr(expr, "this", expr))
    except (TypeError, ValueError):
        return None


def enforce_limit(
    sql: str, *, dialect: str, max_rows: int = DEFAULT_MAX_ROWS
) -> EnforceResult:
    """Block writes and apply the row cap.

    Raises ``sqlglot.errors.ParseError`` for syntactically invalid SQL
    and :class:`WriteStatementBlocked` if a write statement is present.
    Returns the rewritten SQL plus an audit-friendly ``EnforceResult``.
    """
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")
    target = _resolve_dialect(dialect)
    tree = sqlglot.parse_one(sql, read=target)

    forbidden = _statement_kind(tree)
    if forbidden is not None:
        raise WriteStatementBlocked(forbidden)

    current = _read_limit(tree)
    if current is None:
        tree = tree.limit(max_rows)
        clamp: Literal["none", "added", "clamped"] = "added"
        effective = max_rows
        original: int | None = None
    elif current > max_rows:
        tree = tree.limit(max_rows)
        clamp = "clamped"
        effective = max_rows
        original = current
    else:
        clamp = "none"
        effective = current
        original = current

    return EnforceResult(
        sql=tree.sql(dialect=target),
        clamp=clamp,
        original_limit=original,
        effective_limit=effective,
    )


__all__ = [
    "EnforceResult",
    "WriteStatementBlocked",
    "enforce_limit",
    "DEFAULT_MAX_ROWS",
]
