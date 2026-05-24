"""PR-98 — analyzer binary-missing graceful degradation.

Investigation toward 9.8 surfaced a real Day-1 architectural gap:
``analyzers/registry.py`` references binaries by bare name
(``ggoss-csharp``, ``ggoss-ts``, ``ggoss-sql-mssql``,
``ggoss-sql-oracle``, ``ggoss-binary-dotnet``) and ``AnalyzerRunner``
calls ``asyncio.create_subprocess_exec(binary)`` — which raises
``FileNotFoundError`` when the binary isn't on the worker container's
PATH. The default docker-compose ships ``analyzer-csharp`` as a
build-target profile and does not install the analyzer binaries
into the worker image, so a fresh deploy that hasn't activated the
``analyzers`` profile crashes every analysis stage with a cryptic
``FileNotFoundError`` propagating up.

The runner now yields a structured ``recoverable: True`` stderr
record naming the missing binary; the orchestrator's existing
per-stage error handling marks the stage skipped rather than
crashing the run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analyzers.runner import AnalyzerRunner


def test_runner_source_handles_filenotfound():
    body = (
        Path(__file__).resolve().parents[1]
        / "app" / "analyzers" / "runner.py"
    ).read_text(encoding="utf-8")
    # The try/except wraps create_subprocess_exec and yields a
    # RunRecord — does NOT re-raise.
    assert "except FileNotFoundError" in body
    assert '"analyzer_binary_not_found' in body
    assert '"recoverable": True' in body


@pytest.mark.asyncio
async def test_missing_binary_does_not_crash_caller(tmp_path):
    """A collector iterating the runner gets a single stderr record
    and exits cleanly — no FileNotFoundError, no zero-record silent
    success (the caller can distinguish the two)."""
    runner = AnalyzerRunner("/definitely/not/a/real/binary/anywhere")
    records = await runner.run_collect("symbols", str(tmp_path))
    assert len(records) == 1
    assert records[0].stream == "stderr"
    payload = records[0].payload
    assert payload["level"] == "error"
    assert payload["recoverable"] is True
    assert "analyzer_binary_not_found" in payload["message"]
    # The binary that failed is named so the operator can install it.
    assert payload["binary"] == "/definitely/not/a/real/binary/anywhere"


@pytest.mark.asyncio
async def test_existing_binary_still_works(tmp_path):
    """Regression — the new try/except mustn't swallow a successful
    spawn. Use /bin/true: it exits 0 with no output, which the
    runner should report as a clean zero-record run."""
    runner = AnalyzerRunner("/bin/true")
    records = await runner.run_collect("schema", str(tmp_path))
    # No stderr error record (binary was found and ran).
    err = [r for r in records if r.stream == "stderr"
           and r.payload.get("level") == "error"
           and "analyzer_binary_not_found" in r.payload.get("message", "")]
    assert err == []
