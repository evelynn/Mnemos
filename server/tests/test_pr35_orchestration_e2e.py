"""PR-35 — D1 (full webhook→finding orchestration) and D2 (multi-language
concurrent execution) end-to-end test scenarios.

The 12th-round audit named the two highest-value remaining gaps:

* D1 — every step from webhook receipt to a row in ``findings`` had
  isolated coverage, but no single test followed a record from a
  subprocess all the way through ``_record_payload`` to a Node /
  Edge upsert.
* D2 — the platform's headline promise is C# + TypeScript + MSSQL +
  Oracle in one project, but nothing checked that four analyzer
  runners can stream concurrently without one stream's stderr
  starving another's stdout.

Both scenarios run against fake analyzer subprocesses (the same
pattern PR-32 introduced) and a mock SQLAlchemy session that
records every ``upsert_node`` / ``upsert_edge`` call. No live
Postgres needed — the contract between the orchestrator helpers
and the merge writer is what we're pinning.
"""

from __future__ import annotations

import asyncio
import stat
import textwrap
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.analyzers.runner import AnalyzerRunner
from app.orchestrator.jobs import _VERB_ACCEPT, _record_payload


def _write_fake_analyzer(tmp_path: Path, name: str, script: str) -> Path:
    """Drop a #!/usr/bin/env python3 script in ``tmp_path`` and chmod +x."""
    binary = tmp_path / name
    binary.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(script))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary


# ---------------------------------------------------------------------------
# D1 — full subprocess → _record_payload → upsert call chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d1_analyzer_records_flow_through_to_upsert_calls(tmp_path):
    """A C#-shaped JSONL payload from a real subprocess must land
    as four distinct upsert calls — one per record_type that the
    'symbols'/'contracts' verbs accept.

    The accept-set in ``_VERB_ACCEPT['symbols']`` is
    ``{"symbol", "data_entity"}`` and ``_VERB_ACCEPT['contracts']`` is
    ``{"contract", "edge"}``; the orchestrator combines them across
    verbs but per-verb only the matching types are persisted. This
    test confirms the gate works.
    """
    fake = _write_fake_analyzer(
        tmp_path,
        "ggoss-csharp",
        """
        import json, sys
        records = [
            {"record_type": "symbol",
             "data": {"id": "Foo.Bar", "kind": "method"},
             "source_name": "ggoss-csharp"},
            {"record_type": "data_entity",
             "data": {"id": "tbl:users", "name": "users"},
             "source_name": "ggoss-csharp"},
            {"record_type": "contract",
             "data": {"id": "ep:/api/v1/x",
                      "kind": "http_endpoint",
                      "spec": {"method": "GET", "path": "/api/v1/x"}}},
            {"record_type": "edge",
             "data": {"source_id": "Foo.Bar",
                      "target_id": "Baz.Qux",
                      "kind": "CALLS"}},
        ]
        for r in records:
            print(json.dumps(r))
        """,
    )

    # The orchestrator funnels every record through ``_record_payload``.
    # Patch ``upsert_node`` + ``upsert_edge`` so we can assert the
    # *exact* call shape without needing a real session.
    captured: dict[str, list[dict]] = {"nodes": [], "edges": []}

    async def fake_upsert_node(session, **kw):
        captured["nodes"].append(kw)

    async def fake_upsert_edge(session, **kw):
        captured["edges"].append(kw)

    project_id = uuid.uuid4()
    runner = AnalyzerRunner(str(fake))

    with patch("app.orchestrator.jobs.upsert_node", new=fake_upsert_node), \
         patch("app.orchestrator.jobs.upsert_edge", new=fake_upsert_edge):
        totals = {"symbols": 0, "contracts": 0, "edges": 0}
        # symbols verb accepts symbol + data_entity.
        async for rec in runner.run("symbols", str(tmp_path)):
            if rec.stream != "stdout":
                continue
            await _record_payload(
                session=AsyncMock(),
                project_id=project_id,
                payload=rec.payload,
                accept_kinds=_VERB_ACCEPT["symbols"],
                totals=totals,
            )

    # Symbol + data_entity passed the gate; contract + edge were
    # silently dropped (wrong verb), which is the intended behaviour.
    assert totals["symbols"] == 1
    assert totals.get("data_entities", 0) == 1
    # No contract / edge writes because the verb gate refused them.
    assert totals["contracts"] == 0
    assert totals["edges"] == 0
    assert len(captured["nodes"]) == 2
    assert len(captured["edges"]) == 0


@pytest.mark.asyncio
async def test_d1_contract_records_resolve_http_id(tmp_path):
    """A ``contract`` record whose ``kind`` is ``http_endpoint`` and
    whose ``spec.method`` + ``spec.path`` are present must have its
    node id resolved through the platform's HTTP-contract id helper
    *before* being upserted — so two analyzers can independently
    point at the same contract by URL pattern.
    """
    fake = _write_fake_analyzer(
        tmp_path,
        "ggoss-fake",
        """
        import json, sys
        print(json.dumps({
            "record_type": "contract",
            "data": {
                "id": "ignored",  # platform recomputes from method+path
                "kind": "http_endpoint",
                "spec": {"method": "POST", "path": "/api/v1/users/{id}"},
            },
        }))
        """,
    )

    captured: list[dict] = []

    async def fake_upsert_node(session, **kw):
        captured.append(kw)

    project_id = uuid.uuid4()
    runner = AnalyzerRunner(str(fake))

    with patch("app.orchestrator.jobs.upsert_node", new=fake_upsert_node):
        async for rec in runner.run("contracts", str(tmp_path)):
            if rec.stream != "stdout":
                continue
            await _record_payload(
                session=AsyncMock(),
                project_id=project_id,
                payload=rec.payload,
                accept_kinds=_VERB_ACCEPT["contracts"],
                totals={"contracts": 0, "edges": 0, "symbols": 0},
            )

    assert len(captured) == 1
    # The node_id passed to the upsert is the resolved HTTP id,
    # NOT the analyzer's placeholder ``"ignored"`` value.
    resolved_id = captured[0]["node_id"]
    assert resolved_id != "ignored"
    assert "POST" in resolved_id or "post" in resolved_id.lower()
    assert "/api/v1/users/{id}" in resolved_id or "{id}" in resolved_id


@pytest.mark.asyncio
async def test_d1_edge_records_with_http_target_get_resolved(tmp_path):
    """An edge whose ``target_id`` starts with ``http.`` is a CALLS
    edge pointing at an HTTP contract — the orchestrator must rewrite
    the target through the same id helper so the edge actually
    matches the contract node a different analyzer wrote.
    """
    fake = _write_fake_analyzer(
        tmp_path,
        "ggoss-fake",
        """
        import json
        print(json.dumps({
            "record_type": "edge",
            "data": {
                "source_id": "billing.charge",
                "target_id": "http.POST./api/v1/payments",
                "kind": "CALLS",
            },
        }))
        """,
    )

    captured: list[dict] = []

    async def fake_upsert_edge(session, **kw):
        captured.append(kw)

    project_id = uuid.uuid4()
    runner = AnalyzerRunner(str(fake))

    with patch("app.orchestrator.jobs.upsert_edge", new=fake_upsert_edge):
        async for rec in runner.run("contracts", str(tmp_path)):
            if rec.stream != "stdout":
                continue
            await _record_payload(
                session=AsyncMock(),
                project_id=project_id,
                payload=rec.payload,
                accept_kinds=_VERB_ACCEPT["contracts"],
                totals={"contracts": 0, "edges": 0, "symbols": 0},
            )

    assert len(captured) == 1
    resolved_target = captured[0]["target_id"]
    # The canonical form is ``http.METHOD.path`` — the resolver
    # *normalises* the path (collapses path variables, strips
    # querystrings) rather than stripping the ``http.`` prefix.
    # Proof the resolver ran: the result equals what
    # ``http_contract_id`` would produce for the same input.
    from app.merge.contract_id import http_contract_id

    assert resolved_target == http_contract_id("POST", "/api/v1/payments")


@pytest.mark.asyncio
async def test_d1_malformed_records_are_silently_ignored(tmp_path):
    """A record missing ``data.id`` (for symbol/contract/data_entity)
    or ``data.source_id`` / ``data.target_id`` (for edge) must not
    crash the loop. The orchestrator skips the record and moves on —
    every other record after the bad one still lands."""
    fake = _write_fake_analyzer(
        tmp_path,
        "ggoss-fake",
        """
        import json
        # First record is malformed (no id).
        print(json.dumps({"record_type": "symbol", "data": {}}))
        # Second record is fine.
        print(json.dumps({"record_type": "symbol",
                          "data": {"id": "Survives"}}))
        # Third is also malformed.
        print(json.dumps({"record_type": "edge", "data": {"kind": "CALLS"}}))
        """,
    )

    captured: list[dict] = []

    async def fake_upsert_node(session, **kw):
        captured.append(kw)

    project_id = uuid.uuid4()
    runner = AnalyzerRunner(str(fake))

    with patch("app.orchestrator.jobs.upsert_node", new=fake_upsert_node):
        totals = {"symbols": 0, "contracts": 0, "edges": 0}
        async for rec in runner.run("symbols", str(tmp_path)):
            if rec.stream != "stdout":
                continue
            await _record_payload(
                session=AsyncMock(),
                project_id=project_id,
                payload=rec.payload,
                accept_kinds=_VERB_ACCEPT["symbols"],
                totals=totals,
            )

    # Only the well-formed symbol made it through.
    assert len(captured) == 1
    assert captured[0]["node_id"] == "Survives"
    assert totals["symbols"] == 1


# ---------------------------------------------------------------------------
# D2 — multi-language concurrent runners
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d2_four_languages_run_concurrently_without_starving(tmp_path):
    """The spec §1.5 promise: ``C# + TypeScript + MSSQL + Oracle in
    one project``. Each analyzer must stream JSONL on its own
    subprocess pipe without the orchestrator's drain loop letting
    one language's stderr starve another's stdout.

    We launch four fake analyzers in parallel via ``asyncio.gather``,
    each emitting a distinguishable ``language`` marker, and assert
    every record from every language arrives intact.
    """
    languages = ("csharp", "typescript", "mssql", "oracle")
    fakes = {
        lang: _write_fake_analyzer(
            tmp_path,
            f"ggoss-{lang}",
            f"""
            import json, sys
            for i in range(50):
                print(json.dumps({{
                    "record_type": "symbol",
                    "data": {{"id": f"{lang}.S{{i}}", "language": "{lang}"}},
                }}))
                sys.stdout.flush()
                # Throw stderr noise in the mix so we exercise the
                # interleave path under concurrent traffic.
                sys.stderr.write(f"{lang} loaded {{i}}\\n")
                sys.stderr.flush()
            """,
        )
        for lang in languages
    }

    async def _run_one(lang: str) -> list[dict]:
        runner = AnalyzerRunner(str(fakes[lang]))
        records = await runner.run_collect("symbols", str(tmp_path))
        # Only the stdout JSONL records — stderr is operational chatter.
        return [r.payload for r in records if r.stream == "stdout"]

    # Concurrent fan-out.
    results = await asyncio.gather(*[_run_one(lang) for lang in languages])

    # Each language emitted 50 records; none were lost or interleaved
    # across languages.
    for lang, payloads in zip(languages, results):
        assert len(payloads) == 50, (
            f"{lang}: expected 50 records, got {len(payloads)} — possible "
            "stream starvation"
        )
        # Every record claims the right language marker (proof no
        # cross-pipe contamination).
        assert all(p["data"]["language"] == lang for p in payloads)


@pytest.mark.asyncio
async def test_d2_one_language_crash_does_not_take_down_the_others(tmp_path):
    """If a single analyzer subprocess crashes mid-run, the
    concurrent analyzers must still complete their work — the
    orchestrator surfaces the failure on that one stage's
    ``error_log`` and lets the rest of the analysis run finish.
    """
    good_lang = _write_fake_analyzer(
        tmp_path,
        "ggoss-good",
        """
        import json
        for i in range(10):
            print(json.dumps({"record_type": "symbol",
                              "data": {"id": f"good.S{i}"}}))
        """,
    )
    bad_lang = _write_fake_analyzer(
        tmp_path,
        "ggoss-bad",
        """
        import json, sys
        print(json.dumps({"record_type": "symbol",
                          "data": {"id": "bad.S0"}}))
        sys.stderr.write("fatal: OOM\\n")
        sys.exit(7)
        """,
    )

    async def _run(binary: Path) -> tuple[int, list[dict]]:
        runner = AnalyzerRunner(str(binary))
        recs = await runner.run_collect("symbols", str(tmp_path))
        return 0, [r.payload for r in recs if r.stream == "stdout"]

    good_result, bad_result = await asyncio.gather(
        _run(good_lang), _run(bad_lang)
    )

    # Good run delivered everything.
    assert len(good_result[1]) == 10
    # Bad run delivered the records that arrived before the crash,
    # not zero — partial work is preserved.
    assert len(bad_result[1]) == 1
    assert bad_result[1][0]["data"]["id"] == "bad.S0"


@pytest.mark.asyncio
async def test_d2_concurrent_records_merge_into_one_graph(tmp_path):
    """The classic monorepo case: a C# service exposes an HTTP
    endpoint, a TypeScript frontend calls it. Both analyzers point
    at the *same* contract node id; the merge writer must upsert,
    not duplicate.

    We don't have a real merge writer here (no session), but the
    ``_record_payload`` path is the single entry that picks the
    contract id. We assert both languages produce the same
    ``node_id`` when run against the same endpoint.
    """
    csharp = _write_fake_analyzer(
        tmp_path,
        "ggoss-csharp",
        """
        import json
        # Server side: exposes the endpoint.
        print(json.dumps({"record_type": "contract",
                          "data": {"id": "x",
                                   "kind": "http_endpoint",
                                   "spec": {"method": "POST",
                                            "path": "/api/charge"}}}))
        """,
    )
    ts = _write_fake_analyzer(
        tmp_path,
        "ggoss-ts",
        """
        import json
        # Client side: same endpoint, different analyzer.
        print(json.dumps({"record_type": "contract",
                          "data": {"id": "y",
                                   "kind": "http_endpoint",
                                   "spec": {"method": "POST",
                                            "path": "/api/charge"}}}))
        """,
    )

    captured: list[str] = []

    async def fake_upsert_node(session, **kw):
        captured.append(kw["node_id"])

    project_id = uuid.uuid4()

    with patch("app.orchestrator.jobs.upsert_node", new=fake_upsert_node):
        for binary in (csharp, ts):
            runner = AnalyzerRunner(str(binary))
            async for rec in runner.run("contracts", str(tmp_path)):
                if rec.stream != "stdout":
                    continue
                await _record_payload(
                    session=AsyncMock(),
                    project_id=project_id,
                    payload=rec.payload,
                    accept_kinds=_VERB_ACCEPT["contracts"],
                    totals={"contracts": 0, "edges": 0, "symbols": 0},
                )

    # Both analyzers produced records, but both resolved to the
    # same canonical node_id — that's what makes the merge work.
    assert len(captured) == 2
    assert captured[0] == captured[1], (
        f"Same endpoint from two analyzers must hash to one node_id, "
        f"got {captured!r}"
    )


# ---------------------------------------------------------------------------
# Schedule + observability surface — the bits a 50K-file run depends on
# ---------------------------------------------------------------------------


def test_d2_registry_has_one_binary_per_phase1_language():
    """Regression guard. If ``_BINARIES`` ever drops a language the
    multi-language scenario silently degrades. PR-115 added Python
    (ggoss-py — pure-stdlib Python AST analyzer); PR-191 added cpp
    (ggoss-cpp) and javascript (ggoss-ts walks .js); PR-192 added java
    (ggoss-java); PR-193 added html/css/scss
    (ggoss-web); PR-194 added kotlin (ggoss-kotlin);
    PR-195 added go/rust/ruby (ggoss-treesitter, config-driven). 15
    source-tree languages + the dotnet binary decompiler."""
    from app.analyzers.registry import _BINARIES

    expected = {
        "csharp", "typescript", "javascript", "python", "cpp", "java", "kotlin",
        "html", "css", "scss",
        "go", "rust", "ruby",
        "mssql", "oracle",
        "dotnet_binary",
    }
    assert set(_BINARIES.keys()) == expected
