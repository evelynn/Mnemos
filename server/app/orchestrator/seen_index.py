"""Bounded identity index for one source-analysis run.

Deletion/rename reconciliation must remember every logical node/edge emitted
by authoritative analyzers.  Plain Python sets are fast for normal projects
but make worker memory grow linearly with very large graphs.  This index keeps
the fast in-memory path up to a configurable threshold, then spills identities
to a private temporary SQLite file and answers final sweep membership in
batches.

The SQLite file contains only semantic graph identifiers, is never exposed to
users, and is removed when the run finishes (success, failure, or cancellation).
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from pathlib import Path

EdgeKey = tuple[str, str, str]


def configured_memory_limit() -> int:
    """Maximum identities retained in Python sets before disk spill."""

    try:
        return max(1_000, int(os.environ.get("MNEMOS_SEEN_MEMORY_LIMIT", "100000")))
    except ValueError:
        return 100_000


def _edge_value(key: EdgeKey) -> str:
    # JSON's escaping makes this collision-free even when identifiers contain
    # delimiters or control characters.
    return json.dumps(key, ensure_ascii=False, separators=(",", ":"))


def _chunks[T](items: list[T], size: int) -> Iterable[list[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class SeenFactIndex:
    """Run-local set with bounded RAM and transparent disk spill.

    ``add_*`` returns whether the key was already known only while the index is
    fully in memory.  Once spilled, :attr:`fast_path_enabled` becomes false and
    callers use the normal semantic upsert path; exact duplicate knowledge is
    no longer required until the batched final sweep.
    """

    def __init__(self, *, memory_limit: int = 100_000, flush_size: int = 2_000):
        self._memory_limit = max(1_000, int(memory_limit))
        self._flush_size = max(100, int(flush_size))
        self._nodes: set[str] = set()
        self._edges: set[EdgeKey] = set()
        self._conn: sqlite3.Connection | None = None
        self._path: Path | None = None

    @property
    def fast_path_enabled(self) -> bool:
        return self._conn is None

    @property
    def spilled(self) -> bool:
        return self._conn is not None

    def add_node(self, node_id: str) -> bool:
        if self._conn is None:
            already = node_id in self._nodes
            self._nodes.add(node_id)
            self._spill_if_needed()
            return already
        self._nodes.add(node_id)
        if len(self._nodes) >= self._flush_size:
            self._flush_nodes()
        return False

    def add_edge(self, key: EdgeKey) -> bool:
        if self._conn is None:
            already = key in self._edges
            self._edges.add(key)
            self._spill_if_needed()
            return already
        self._edges.add(key)
        if len(self._edges) >= self._flush_size:
            self._flush_edges()
        return False

    def contains_nodes(self, node_ids: Iterable[str]) -> set[str]:
        values = list(node_ids)
        if self._conn is None:
            return {value for value in values if value in self._nodes}
        self._flush()
        found: set[str] = set()
        for batch in _chunks(values, 900):
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(
                f"SELECT value FROM node_ids WHERE value IN ({placeholders})",  # noqa: S608
                batch,
            )
            found.update(str(row[0]) for row in rows)
        return found

    def contains_edges(self, keys: Iterable[EdgeKey]) -> set[EdgeKey]:
        values = list(keys)
        if self._conn is None:
            return {value for value in values if value in self._edges}
        self._flush()
        encoded = {_edge_value(value): value for value in values}
        found: set[EdgeKey] = set()
        encoded_values = list(encoded)
        for batch in _chunks(encoded_values, 900):
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(
                f"SELECT value FROM edge_ids WHERE value IN ({placeholders})",  # noqa: S608
                batch,
            )
            found.update(encoded[str(row[0])] for row in rows)
        return found

    def stats(self) -> dict[str, int | str]:
        if self._conn is None:
            return {
                "storage": "memory",
                "nodes": len(self._nodes),
                "edges": len(self._edges),
                "memory_limit": self._memory_limit,
            }
        self._flush()
        nodes = int(self._conn.execute("SELECT count(*) FROM node_ids").fetchone()[0])
        edges = int(self._conn.execute("SELECT count(*) FROM edge_ids").fetchone()[0])
        return {
            "storage": "disk",
            "nodes": nodes,
            "edges": edges,
            "memory_limit": self._memory_limit,
        }

    def close(self) -> None:
        conn, path = self._conn, self._path
        self._conn = None
        self._path = None
        self._nodes.clear()
        self._edges.clear()
        if conn is not None:
            conn.close()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Cleanup failure is non-authoritative state only; the OS temp
                # cleaner can remove it later and the graph result stays valid.
                pass

    def _spill_if_needed(self) -> None:
        if len(self._nodes) + len(self._edges) < self._memory_limit:
            return
        fd, raw_path = tempfile.mkstemp(prefix="mnemos-seen-", suffix=".sqlite3")
        os.close(fd)
        self._path = Path(raw_path)
        self._conn = sqlite3.connect(raw_path)
        self._conn.execute("PRAGMA journal_mode=OFF")
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute("CREATE TABLE node_ids (value TEXT PRIMARY KEY)")
        self._conn.execute("CREATE TABLE edge_ids (value TEXT PRIMARY KEY)")
        self._flush()

    def _flush_nodes(self) -> None:
        if self._conn is None or not self._nodes:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO node_ids(value) VALUES (?)",
            ((value,) for value in self._nodes),
        )
        self._nodes.clear()

    def _flush_edges(self) -> None:
        if self._conn is None or not self._edges:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO edge_ids(value) VALUES (?)",
            ((_edge_value(value),) for value in self._edges),
        )
        self._edges.clear()

    def _flush(self) -> None:
        if self._conn is None:
            return
        self._flush_nodes()
        self._flush_edges()
        self._conn.commit()
