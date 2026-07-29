from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_ANALYZER = _ROOT / "analyzers" / "ggoss-ts" / "src" / "index.mjs"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    probe = subprocess.run(
        [node, "-e", "require('typescript')"],
        cwd=str(_ANALYZER.parent.parent),
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("typescript unavailable")
    return node


def _run(target: Path, verb: str) -> list[dict]:
    completed = subprocess.run(
        [_node(), str(_ANALYZER), verb, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert not completed.stderr, completed.stderr
    return [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.strip()
    ]


def test_vue_sfc_indexes_only_script_with_original_locations(tmp_path: Path) -> None:
    component = tmp_path / "OrderPanel.vue"
    component.write_text(
        """<!-- Documentation example: <script>fakeCall()</script> -->
<template>{{ templateOnlyCall() }}</template>
<script setup lang="ts">
interface Order { id: string }
const loadOrders = async () => {
  const response = await fetch("/api/orders")
  return prisma.order.findMany()
}
</script>
<style>.ignored { color: red }</style>
""",
        encoding="utf-8",
    )

    symbols = [
        item["data"]
        for item in _run(tmp_path, "symbols")
        if item["record_type"] == "symbol"
    ]
    assert {item["name"] for item in symbols} == {
        "<module>",
        "Order",
        "loadOrders",
    }
    assert all(item["location"]["file"] == "OrderPanel.vue" for item in symbols)
    module = next(item for item in symbols if item["name"] == "<module>")
    assert module["id"] == "ts:OrderPanel.vue:<module>"
    assert module["location"] == {"file": "OrderPanel.vue", "line": 1, "col": 1}
    load_orders = next(item for item in symbols if item["name"] == "loadOrders")
    assert next(item for item in symbols if item["name"] == "Order")[
        "location"
    ]["line"] == 4

    calls = [item["data"] for item in _run(tmp_path, "calls")]
    fetch_call = next(
        item for item in calls if item["target_id"] == "ts:extern:fetch"
    )
    assert fetch_call["source_id"] == load_orders["id"]
    assert not any("templateOnlyCall" in item["target_id"] for item in calls)
    assert not any("fakeCall" in item["target_id"] for item in calls)
    assert all("OrderPanel.vue" in item["source_id"] for item in calls)

    contracts = _run(tmp_path, "contracts")
    assert any(
        item["record_type"] == "contract"
        and item["data"]["id"] == "http.GET./api/orders"
        for item in contracts
    )
    contract_edge = next(
        item["data"]
        for item in contracts
        if item["record_type"] == "edge"
        and item["data"]["target_id"] == "http.GET./api/orders"
    )
    assert contract_edge["source_id"] == module["id"]

    data_access = _run(tmp_path, "data_access")
    assert any(
        item["record_type"] == "data_entity"
        and item["data"]["id"] == "data.order"
        for item in data_access
    )
    data_edge = next(
        item["data"]
        for item in data_access
        if item["record_type"] == "edge"
        and item["data"]["target_id"] == "data.order"
    )
    assert data_edge["source_id"] == load_orders["id"]


def test_vue_self_closing_style_does_not_hide_pug_script(tmp_path: Path) -> None:
    (tmp_path / "PugPanel.vue").write_text(
        """<template lang="pug">
section Pug content
</template>
<style src="./theme.scss" />
<script setup lang="ts">
const loadPugPanel = () => fetch("/api/pug")
</script>
""",
        encoding="utf-8",
    )

    symbols = [
        item["data"]
        for item in _run(tmp_path, "symbols")
        if item["record_type"] == "symbol"
    ]
    assert {item["name"] for item in symbols} == {"<module>", "loadPugPanel"}
    assert next(item for item in symbols if item["name"] == "loadPugPanel")[
        "location"
    ]["line"] == 6


def test_malformed_vue_is_recoverable_and_other_files_continue(
    tmp_path: Path,
) -> None:
    (tmp_path / "Broken.vue").write_text(
        "<script setup>const broken = true\n",
        encoding="utf-8",
    )
    (tmp_path / "Healthy.vue").write_text(
        "<script setup>export function healthy() { return 1 }</script>\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [_node(), str(_ANALYZER), "symbols", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0
    assert "vue_sfc_parse_failed" in completed.stderr
    records = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.strip()
    ]
    assert any(
        item["record_type"] == "symbol"
        and item["data"]["name"] == "healthy"
        for item in records
    )


def test_vue_relative_import_resolves_to_the_declared_symbol(tmp_path: Path) -> None:
    (tmp_path / "helper.vue").write_text(
        '<script lang="ts">export function helper() { return 1 }</script>\n',
        encoding="utf-8",
    )
    (tmp_path / "caller.vue").write_text(
        """<script setup lang="ts">
import { helper } from "./helper.vue"
const caller = () => helper()
</script>
""",
        encoding="utf-8",
    )

    calls = [item["data"] for item in _run(tmp_path, "calls")]
    edge = next(item for item in calls if "helper" in item["target_id"])
    assert edge["target_id"].startswith("ts:helper.vue:helper@")
    assert edge["metadata"]["callee_resolved"] is True


def test_generated_module_formats_are_excluded_case_insensitively(
    tmp_path: Path,
) -> None:
    (tmp_path / "real.js").write_text(
        "export function realSource() { return 1 }\n",
        encoding="utf-8",
    )
    allowed = {
        "domain.bundle.ts",
        "source.esm.js",
        "app.js",
        "min.js",
    }
    for name in allowed:
        (tmp_path / name).write_text("export const source = true\n", encoding="utf-8")
    for name in (
        "vendor.min.js",
        "vendor.bundle.js",
        "bundle.js",
        "vendor.bundle.mjs",
        "vendor.iife.js",
        "vendor.umd.js",
        "vendor.umd.cjs",
        "UPPER.IIFE.JS",
    ):
        (tmp_path / name).write_text(
            f"export function generated_{name.replace('.', '_')}() {{}}\n",
            encoding="utf-8",
        )

    completed = subprocess.run(
        [_node(), str(_ANALYZER), "inventory", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    payload = json.loads(completed.stdout)
    assert set(payload["files"]) == {"real.js", *allowed}

    symbols = [
        item["data"]["name"]
        for item in _run(tmp_path, "symbols")
        if item["record_type"] == "symbol"
    ]
    assert "realSource" in symbols
    assert not any(name.startswith("generated_") for name in symbols)


def test_excluded_bundle_cannot_reenter_through_import(tmp_path: Path) -> None:
    (tmp_path / "vendor.iife.js").write_text(
        """export function generatedApi() {
  fetch("/generated-only")
  return prisma.secret.findMany()
}
""",
        encoding="utf-8",
    )
    (tmp_path / "app.ts").write_text(
        """import { generatedApi } from "./vendor.iife.js"
export function app() { return generatedApi() }
""",
        encoding="utf-8",
    )

    symbols = [
        item["data"]
        for item in _run(tmp_path, "symbols")
        if item["record_type"] == "symbol"
    ]
    assert {item["name"] for item in symbols} == {"app"}

    calls = [item["data"] for item in _run(tmp_path, "calls")]
    assert all("vendor.iife.js" not in item["target_id"] for item in calls)
    assert not any("fetch" in item["target_id"] for item in calls)

    contracts = _run(tmp_path, "contracts")
    assert not any(
        item["record_type"] == "contract"
        and item["data"]["id"] == "http.GET./generated-only"
        for item in contracts
    )
    data_access = _run(tmp_path, "data_access")
    assert not any(
        item["record_type"] == "data_entity"
        and item["data"]["id"] == "data.secret"
        for item in data_access
    )
