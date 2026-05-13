"""End-to-end analysis pipeline tests — exercises the analyzer-runner
contract with a real subprocess that emits the JSONL the platform
expects from a production analyzer.

The 11th-round audit (거대 시스템 분석 e2e 가능성 검증) flagged that
``AnalyzerRunner.run`` had unit-level coverage only — the contract
that ``orchestrator/jobs.py`` depends on (a binary that prints one
JSON object per stdout line, errors to stderr, exits 0 on success)
had no end-to-end check. These tests script a stand-in analyzer in
a temp dir, point ``AnalyzerRunner`` at it, and assert the platform
side decodes everything the way the analyzer-contract document
promises.
"""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest

from app.analyzers.runner import AnalyzerRunner, RunRecord


def _write_fake_analyzer(tmp_path: Path, script: str) -> Path:
    """Drop a #!/usr/bin/env python3 script in ``tmp_path`` and chmod +x."""
    binary = tmp_path / "ggoss-fake"
    binary.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(script))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary


# ---------------------------------------------------------------------------
# Happy path — a multi-language analyzer that emits the four record types
# the platform's _VERB_ACCEPT whitelist expects.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyzer_streaming_decodes_jsonl_records(tmp_path):
    fake = _write_fake_analyzer(
        tmp_path,
        """
        import json, sys
        # One record per line — matches docs/analyzer-contract.md.
        print(json.dumps({"record_type": "symbol", "id": "Foo.Bar"}))
        print(json.dumps({"record_type": "edge",
                          "from_id": "Foo.Bar", "to_id": "Baz.Qux",
                          "kind": "CALLS"}))
        print(json.dumps({"record_type": "contract",
                          "id": "ep:/api/v1/x", "method": "GET",
                          "path": "/api/v1/x"}))
        print(json.dumps({"record_type": "data_entity",
                          "id": "tbl:users", "name": "users"}))
        sys.exit(0)
        """,
    )
    runner = AnalyzerRunner(str(fake))
    records = await runner.run_collect("symbols", str(tmp_path))
    # Four valid JSON lines on stdout, zero on stderr.
    stdout_recs = [r for r in records if r.stream == "stdout"]
    stderr_recs = [r for r in records if r.stream == "stderr"]
    assert len(stdout_recs) == 4
    assert stderr_recs == []
    kinds = [r.payload["record_type"] for r in stdout_recs]
    assert kinds == ["symbol", "edge", "contract", "data_entity"]


@pytest.mark.asyncio
async def test_analyzer_stderr_lines_carry_parse_error_flag_when_unparseable(
    tmp_path,
):
    """Operational analyzers print human-readable warnings to stderr.
    The runner must tag those lines as ``stream="stderr"`` and, when
    the line isn't valid JSON, surface a ``parse_error: True`` flag
    instead of crashing the merge loop.
    """
    fake = _write_fake_analyzer(
        tmp_path,
        """
        import json, sys
        sys.stderr.write("loaded 12 source files\\n")
        sys.stderr.flush()
        print(json.dumps({"record_type": "symbol", "id": "A.B"}))
        """,
    )
    runner = AnalyzerRunner(str(fake))
    records = await runner.run_collect("symbols", str(tmp_path))
    stderr_recs = [r for r in records if r.stream == "stderr"]
    assert len(stderr_recs) == 1
    assert stderr_recs[0].payload.get("parse_error") is True
    assert stderr_recs[0].payload.get("raw") == "loaded 12 source files"


@pytest.mark.asyncio
async def test_analyzer_non_zero_exit_still_yields_records_before_failure(
    tmp_path, caplog
):
    """An analyzer that prints partial output and then crashes must
    still deliver whatever it already wrote — the merge pass keys off
    the records that did arrive, and surfaces the failure in
    `analysis_runs.stats` rather than discarding the partial work.
    """
    fake = _write_fake_analyzer(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"record_type": "symbol", "id": "Partial"}))
        sys.stdout.flush()
        sys.stderr.write("fatal: out of memory\\n")
        sys.exit(7)
        """,
    )
    runner = AnalyzerRunner(str(fake))
    records = await runner.run_collect("symbols", str(tmp_path))
    stdout_recs = [r for r in records if r.stream == "stdout"]
    assert any(r.payload["id"] == "Partial" for r in stdout_recs)


# ---------------------------------------------------------------------------
# Env scrubbing — the platform must not leak its own secrets to the analyzer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyzer_does_not_see_unsafe_host_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "this-should-never-reach-analyzer")
    monkeypatch.setenv("DATABASE_URL", "postgres://leak/leak")
    monkeypatch.setenv("FERNET_KEY", "leak-fernet")
    fake = _write_fake_analyzer(
        tmp_path,
        """
        import json, os, sys
        # Dump every env key the subprocess can see so the test can
        # assert no platform secrets crossed the boundary.
        print(json.dumps({"record_type": "symbol",
                          "id": "envdump",
                          "env": sorted(os.environ.keys())}))
        """,
    )
    runner = AnalyzerRunner(str(fake))
    records = await runner.run_collect("symbols", str(tmp_path))
    seen_env = records[0].payload["env"]
    for leaky in ("SECRET_KEY", "DATABASE_URL", "FERNET_KEY"):
        assert leaky not in seen_env, (
            f"{leaky} must NOT be visible to the analyzer subprocess"
        )


@pytest.mark.asyncio
async def test_analyzer_receives_caller_extra_env(tmp_path):
    """Caller-supplied env (e.g. ``MNEMOS_DB_CONN``) overrides + supplements
    the safe-host set so the analyzer can pick up the credentials it
    actually needs."""
    fake = _write_fake_analyzer(
        tmp_path,
        """
        import json, os, sys
        print(json.dumps({"record_type": "symbol",
                          "id": "envdump",
                          "conn": os.environ.get("MNEMOS_DB_CONN", "")}))
        """,
    )
    runner = AnalyzerRunner(str(fake))
    records = []
    async for rec in runner.run(
        "symbols",
        str(tmp_path),
        env={"MNEMOS_DB_CONN": "Server=test;User=app"},
    ):
        records.append(rec)
    assert records[0].payload["conn"] == "Server=test;User=app"


# ---------------------------------------------------------------------------
# Stream isolation — neither stream is allowed to starve the other.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyzer_stdout_and_stderr_interleave_freely(tmp_path):
    """A real analyzer prints stderr warnings while it streams stdout
    records. The drainer keeps both flowing independently."""
    fake = _write_fake_analyzer(
        tmp_path,
        """
        import json, sys, time
        for i in range(10):
            print(json.dumps({"record_type": "symbol", "id": f"S{i}"}))
            sys.stdout.flush()
            sys.stderr.write(json.dumps({"warn": i}) + "\\n")
            sys.stderr.flush()
        """,
    )
    runner = AnalyzerRunner(str(fake))
    records = await runner.run_collect("symbols", str(tmp_path))
    stdout_recs = [r for r in records if r.stream == "stdout"]
    stderr_recs = [r for r in records if r.stream == "stderr"]
    # All 10 lines from each stream survive.
    assert len(stdout_recs) == 10
    assert len(stderr_recs) == 10


# ---------------------------------------------------------------------------
# Binary missing — fall through cleanly instead of taking the job down.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyzer_missing_binary_raises_filenotfound(tmp_path):
    runner = AnalyzerRunner("/nonexistent/path/to/analyzer-binary")
    with pytest.raises(FileNotFoundError):
        await runner.run_collect("symbols", str(tmp_path))


# ---------------------------------------------------------------------------
# RunRecord shape guards
# ---------------------------------------------------------------------------


def test_runrecord_is_immutable_and_carries_two_fields():
    rec = RunRecord(stream="stdout", payload={"record_type": "symbol"})
    assert rec.stream == "stdout"
    assert rec.payload == {"record_type": "symbol"}
    with pytest.raises(Exception):  # dataclass(frozen=True)
        rec.stream = "stderr"  # type: ignore[misc]
