"""PR-164 — Next.js App Router handlers EXPOSE their contract.

A dogfood run of Mnemos on a real Next.js app extracted 63 HTTP contracts but
0 EXPOSES edges: ggoss-ts saw the client ``fetch('/api/..')`` (CALLS) yet never
the server handler, because it had detectors for NestJS decorators and Express
routers but not for the Next.js pattern (``export async function GET`` in an
``app/**/route.ts`` file). Without EXPOSES, detect_duplicate_endpoints and OTLP
runtime reconcile (which key off the contract's exposer) could not work for the
single most common TS backend shape. ggoss-ts now derives the route from the
file path and emits one contract + EXPOSES edge per exported HTTP method.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ANALYZER = (
    Path(__file__).resolve().parents[2] / "analyzers" / "ggoss-ts" / "src" / "index.mjs"
)


def _node() -> str | None:
    n = shutil.which("node")
    if n is None:
        return None
    p = subprocess.run(
        [n, "-e", "require('typescript')"],
        cwd=str(_ANALYZER.parent.parent),
        capture_output=True,
    )
    return n if p.returncode == 0 else None


def _contracts(node: str, target: Path) -> tuple[dict[str, str], list[dict]]:
    proc = subprocess.run(
        [node, str(_ANALYZER), "contracts", str(target)],
        capture_output=True,
        text=True,
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


def _route(tmp_path: Path, rel: str, body: str) -> None:
    p = tmp_path / "app" / rel / "route.ts"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_nextjs_route_handlers_exposed(tmp_path):
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    # One file, two exported HTTP methods → two contracts + two EXPOSES.
    _route(
        tmp_path,
        "api/admin/users",
        "export async function GET() { return new Response('[]'); }\n"
        "export async function POST() { return new Response('{}'); }\n",
    )
    contracts, edges = _contracts(node, tmp_path)

    assert contracts.get("http.GET./api/admin/users") == "ts_nextjs_route"
    assert contracts.get("http.POST./api/admin/users") == "ts_nextjs_route"
    exposes = {e["target_id"] for e in edges if e["kind"] == "EXPOSES"}
    assert "http.GET./api/admin/users" in exposes
    assert "http.POST./api/admin/users" in exposes


def test_dynamic_segments_and_route_groups(tmp_path):
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    # [id] → {id}; the (dash) route group contributes no URL segment.
    _route(tmp_path, "(dash)/reports/[id]", "export function GET() { return new Response(''); }\n")
    contracts, _ = _contracts(node, tmp_path)
    assert "http.GET./reports/{id}" in contracts
    assert "http.GET./(dash)/reports/[id]" not in contracts


def test_arrow_const_handler_detected(tmp_path):
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    _route(tmp_path, "api/ping", "export const GET = async () => new Response('pong');\n")
    contracts, edges = _contracts(node, tmp_path)
    assert "http.GET./api/ping" in contracts
    assert any(e["kind"] == "EXPOSES" and e["target_id"] == "http.GET./api/ping" for e in edges)


def test_non_route_file_with_get_export_is_ignored(tmp_path):
    """A plain module (not app/**/route.ts) that happens to export a GET
    function must NOT be treated as an endpoint."""
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "helpers.ts").write_text(
        "export function GET() { return 1; }\n", encoding="utf-8"
    )
    contracts, _ = _contracts(node, tmp_path)
    assert not any(c.startswith("http.GET.") for c in contracts)
