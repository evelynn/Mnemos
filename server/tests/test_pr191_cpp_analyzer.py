"""PR-191 — ggoss-cpp: deterministic C/C++ analyzer.

The 2026-07-07 real-repo eval (docs/04-eval/agent-context-real-repo-
test-2026-07-07.md) ran Mnemos against a C-dominant repository and every
``*:cpp`` stage skipped as ``no_analyzer`` — the P0 gap. These tests pin
the analyzer contract on a synthetic fixture: symbol kinds, file-scope
detection, C static-linkage call resolution, vendored exclusion, and the
SQL data_access pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CPP_ANALYZER = _ROOT / "analyzers" / "ggoss-cpp" / "src" / "ggoss_cpp.py"


_MAIN_C = """\
#include <stdio.h>
#include "util.h"

#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define VERSION "1.0"

struct server_config {
    int port;
    char host[64];
};

enum run_mode { MODE_CLI, MODE_SERVER };

typedef struct {
    int fd;
} conn_t;

static void handle_request(int fd);
int helper(int x);

static void handle_request(int fd) {
    int biggest = MAX(fd, 10);
    log_line("handled");
    helper(biggest);
}

int main(int argc, char **argv) {
    if (argc > 1) {
        handle_request(1);
    }
    for (int i = 0; i < argc; i++) {
        printf("%s\\n", argv[i]);
    }
    return helper(argc);
}
"""

_UTIL_C = """\
#include "util.h"
#include <sqlite3.h>

int helper(int x) {
    return x * 2;
}

void log_line(const char *msg) {
    helper(1);
}

int load_nodes(sqlite3 *db) {
    const char *sql = "SELECT id, kind "
                      "FROM nodes WHERE valid_to IS NULL";
    return run_query(db, sql);
}

void save_edge(sqlite3 *db) {
    run_query(db, "INSERT INTO edges (src, dst) VALUES (?, ?)");
}
"""

_VENDORED_C = """\
int vendored_secret(void) { return 42; }
"""


def _write_fixture(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.c").write_text(_MAIN_C, encoding="utf-8")
    (root / "src" / "util.c").write_text(_UTIL_C, encoding="utf-8")
    (root / "vendored" / "lib").mkdir(parents=True)
    (root / "vendored" / "lib" / "dep.c").write_text(_VENDORED_C, encoding="utf-8")


def _run(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_CPP_ANALYZER), verb, str(target)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert cp.returncode == 0, cp.stderr
    return [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]


def test_probe_counts_c_files(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    out = _run("probe", tmp_path)
    assert out[0]["applicable"] is True
    # vendored/ excluded: main.c + util.c only.
    assert out[0]["files_found"] == 2


def test_symbols_extracts_functions_types_and_macros(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    records = _run("symbols", tmp_path)
    data = [r["data"] for r in records]
    by_name = {d["name"]: d for d in data}

    assert by_name["main"]["kind"] == "function"
    assert by_name["main"]["is_entry_point"] is True
    assert by_name["main"]["location"]["file"] == "src/main.c"
    assert by_name["handle_request"]["kind"] == "function"
    # Definition (line 21), not the prototype (line 18).
    assert by_name["handle_request"]["location"]["line"] == 21
    assert by_name["server_config"]["kind"] == "struct"
    assert by_name["run_mode"]["kind"] == "enum"
    assert by_name["conn_t"]["metadata"].get("typedef") is True
    assert by_name["MAX"]["kind"] == "macro"
    # Object-like defines are noise, control keywords are not functions.
    assert "VERSION" not in by_name
    assert "if" not in by_name and "for" not in by_name
    # Vendored trees are excluded by default.
    assert "vendored_secret" not in by_name
    # Every record is deterministic-analyzer output.
    assert all(d["certainty"] == "asserted" for d in data)
    assert all(r["source_name"] == "ggoss-cpp" for r in records)


def test_calls_resolve_cross_file_with_same_file_priority(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    edges = [r["data"] for r in _run("calls", tmp_path)]
    by_pair = {(e["source_id"].split(":")[2].split("@")[0],
                e["target_id"]): e for e in edges}

    def edge(caller: str, callee_contains: str) -> dict:
        hits = [
            e for (c, t), e in by_pair.items()
            if c == caller and callee_contains in t
        ]
        assert hits, f"no edge {caller} -> *{callee_contains}*"
        return hits[0]

    # Cross-file: main.c calls helper() defined in util.c — resolved.
    e = edge("main", "util.c:helper")
    assert e["certainty"] == "asserted"
    assert e["metadata"]["callee_resolved"] is True
    # Same-file static: handle_request resolved inside main.c.
    e = edge("main", "main.c:handle_request")
    assert e["certainty"] == "asserted"
    # Macro invocation links to the macro symbol.
    assert edge("handle_request", "main.c:MAX")["metadata"]["callee_resolved"] is True
    # Unknown external (printf) stays honest: extern + inferred.
    e = edge("main", "c:extern:printf")
    assert e["certainty"] == "inferred"
    assert e["metadata"]["callee_resolved"] is False


def test_data_access_reads_sql_literals(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    records = _run("data_access", tmp_path)
    entities = {r["data"]["id"] for r in records if r["record_type"] == "data_entity"}
    edges = [r["data"] for r in records if r["record_type"] == "edge"]

    # Adjacent-literal concatenation: "SELECT ..." "FROM nodes" joins.
    assert "data:nodes" in entities
    assert "data:edges" in entities
    kinds = {(e["target_id"], e["kind"]) for e in edges}
    assert ("data:nodes", "READS") in kinds
    assert ("data:edges", "WRITES") in kinds
    assert all(e["certainty"] == "inferred" for e in edges)


def test_schema_declares_contract(tmp_path: Path) -> None:
    cp = subprocess.run(
        [sys.executable, str(_CPP_ANALYZER), "schema"],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0
    schema = json.loads(cp.stdout)
    assert schema["language"] == "cpp"
    assert "CALLS" in schema["edge_kinds"]
    assert "function" in schema["symbol_kinds"]


def test_registry_maps_cpp_and_javascript() -> None:
    from app.analyzers.registry import binary_for

    assert binary_for("cpp") == "ggoss-cpp"
    # ggoss-ts walks .js too; a JS-only project must not fall through to
    # the agent-extraction path when the deterministic analyzer covers it.
    assert binary_for("javascript") == "ggoss-ts"
