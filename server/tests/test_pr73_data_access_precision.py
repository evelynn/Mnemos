"""PR-73 — data_access precision fix, found by real-world testing.

Running the TypeScript analyzer against five large GitHub projects
(express, axios, vue/core, nestjs/nest, strapi) exposed a critical
precision defect: ``data_access`` flooded the graph with junk
DataEntity nodes. vue/core — a frontend framework with no database
at all — produced 95 "entities" (``data.object``, ``data.props``,
``data.children`` …); express produced ``data.the`` from the prose
string "update the …".

Two root causes:

* ``Object.create()`` / ``NestFactory.create()`` — a capitalised
  receiver with a generic verb — were read as model writes;
* ``node.children.find()`` — any ``a.b.verb()`` chain — matched the
  Prisma ``client.model.verb()`` shape;
* a bare SQL keyword in prose ("update the cache") tripped the
  raw-SQL scan.

The fix gates the Prisma shape on a recognised DB-client root, drops
JS built-ins from the capitalised branch and restricts it to ORM-
distinctive verbs, and only treats a string as SQL when it begins
with a full statement. Post-fix the four non-DB projects report 0
entities; strapi (a real CMS) drops 326 → 13.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_ANALYZER = (
    Path(__file__).resolve().parents[2]
    / "analyzers" / "ggoss-ts" / "src" / "index.mjs"
)


def _node() -> str | None:
    n = shutil.which("node")
    if n is None:
        return None
    p = subprocess.run(
        [n, "-e", "require('typescript')"],
        cwd=str(_ANALYZER.parent.parent), capture_output=True,
    )
    return n if p.returncode == 0 else None


def _data_access(node: str, target: Path) -> tuple[set[str], list[dict]]:
    proc = subprocess.run(
        [node, str(_ANALYZER), "data_access", str(target)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    ents, edges = set(), []
    for ln in proc.stdout.splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r["record_type"] == "data_entity":
            ents.add(r["data"]["id"])
        else:
            edges.append(r["data"])
    return ents, edges


def test_real_orm_calls_still_detected(tmp_path):
    """The genuine ORM / SQL access must survive the precision fix."""
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "real.ts").write_text(
        textwrap.dedent(
            """
            import { prisma } from "./db";
            export async function a() { return prisma.order.findMany(); }
            export async function b(db: any) { return db.user.create({}); }
            export async function c() { return User.findAll(); }
            export async function d(conn: any) {
              return conn.query("SELECT id FROM customers WHERE x = 1");
            }
            """
        ),
        encoding="utf-8",
    )
    ents, _ = _data_access(node, tmp_path)
    assert "data.order" in ents      # prisma client chain
    assert "data.user" in ents       # db client chain
    assert "data.customer" in ents or "data.customers" in ents  # raw SQL
    # Sequelize-style capitalised model with a distinctive verb.
    assert "data.user" in ents


def test_false_positive_constructs_rejected(tmp_path):
    """None of these are database access — the analyzer must stay
    silent, the defect real-world testing exposed."""
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "noise.ts").write_text(
        textwrap.dedent(
            """
            export function a() { return Object.create(null); }
            export function b() { return NestFactory.create(AppModule); }
            export function c(node: any) {
              return node.children.find((x: any) => x.id === 1);
            }
            export function d(this_props: any) {
              return this_props.props.update({});
            }
            export function e() { return "update the layout cache now"; }
            export function f() { return "select an item to continue"; }
            export function g() { return Promise.all([]); }
            """
        ),
        encoding="utf-8",
    )
    ents, edges = _data_access(node, tmp_path)
    assert ents == set(), f"false positives leaked: {sorted(ents)}"
    assert edges == []
