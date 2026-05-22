"""PR-76 — server-route contract detection, found by real-world testing.

The 15-project benchmark showed ``cmdContracts`` only ever found
client-side ``fetch()`` calls: express and nestjs/nest — both HTTP
frameworks — reported 0 contracts, so the analyzer saw none of the
endpoints they *expose*. The C# analyzer detects ``[HttpGet]``
attributes; the TypeScript analyzer had no server-side equivalent.

This PR adds two detectors to ``cmdContracts``:

* Express / Koa routers — ``app.get("/path", handler)`` (the path
  must start with ``/`` so ``map.get("key")`` is not mistaken for a
  route), emitting an EXPOSES edge;
* NestJS decorators — ``@Get(':id')`` on a method, combined with the
  class's ``@Controller('cats')`` prefix.

Measured: express 0 → 1,222 contracts, nest 0 → 1,175.
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


def _contracts(node: str, target: Path) -> tuple[dict[str, str], list[dict]]:
    proc = subprocess.run(
        [node, str(_ANALYZER), "contracts", str(target)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    contracts, edges = {}, []
    for ln in proc.stdout.splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r["record_type"] == "contract":
            contracts[r["data"]["id"]] = r["data"]["metadata"]["detected_by"]
        else:
            edges.append(r["data"])
    return contracts, edges


def test_express_routes_detected(tmp_path):
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "app.ts").write_text(
        textwrap.dedent(
            """
            import express from "express";
            const app = express();
            app.get("/health", (req, res) => res.send("ok"));
            app.post("/api/orders", (req, res) => res.json({}));
            const cache = new Map();
            cache.get("plain-key");
            """
        ),
        encoding="utf-8",
    )
    contracts, edges = _contracts(node, tmp_path)
    assert "http.GET./health" in contracts
    assert "http.POST./api/orders" in contracts
    # cache.get("plain-key") is not a route — the path has no leading /.
    assert not any("plain-key" in c for c in contracts)
    # A server route EXPOSES the contract.
    assert any(e["kind"] == "EXPOSES" for e in edges)


def test_nestjs_decorator_routes_detected(tmp_path):
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "cats.ts").write_text(
        textwrap.dedent(
            """
            import { Controller, Get, Post } from "@nestjs/common";

            @Controller("cats")
            export class CatsController {
              @Get(":id")
              findOne() { return {}; }

              @Post()
              create() { return {}; }
            }
            """
        ),
        encoding="utf-8",
    )
    contracts, edges = _contracts(node, tmp_path)
    # The @Controller prefix is joined onto the method route.
    assert "http.GET./cats/:id" in contracts
    assert "http.POST./cats" in contracts
    assert contracts["http.GET./cats/:id"] == "ts_nest_decorator"
    assert all(e["kind"] == "EXPOSES" for e in edges)


def test_client_fetch_still_calls_not_exposes(tmp_path):
    """A client fetch must still produce a CALLS edge, not EXPOSES —
    the server/client direction is what makes the cross-language join
    meaningful."""
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "client.ts").write_text(
        'export async function load() { return fetch("/api/orders"); }\n',
        encoding="utf-8",
    )
    contracts, edges = _contracts(node, tmp_path)
    assert "http.GET./api/orders" in contracts
    assert contracts["http.GET./api/orders"] == "ts_fetch_literal"
    assert all(e["kind"] == "CALLS" for e in edges)


def test_schema_declares_exposes_edge():
    body = _ANALYZER.read_text(encoding="utf-8")
    idx = body.find("function cmdSchema(")
    slab = body[idx:idx + 300]
    assert '"EXPOSES"' in slab
