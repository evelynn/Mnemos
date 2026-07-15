"""Bounds and lifecycle guarantees for analyzer subprocesses.

These tests use this Python interpreter as the analyzer executable so they
exercise real subprocess pipes on both Windows and POSIX without relying on
shebang execution.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from app.analyzers.runner import (
    AnalyzerOutputError,
    AnalyzerProcessError,
    AnalyzerRunner,
    AnalyzerTimeoutError,
)


def _script(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "fake_analyzer.py"
    path.write_text(source, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_jsonl_record_larger_than_streamreader_default_is_supported(
    tmp_path: Path,
) -> None:
    """A valid bounded record must not hit asyncio's 64 KiB pipe limit."""

    script = _script(
        tmp_path,
        "import json\nprint(json.dumps({'record_type': 'symbol', 'body': 'x' * 70000}))\n",
    )
    runner = AnalyzerRunner(sys.executable)

    records = await runner.run_collect(str(script), tmp_path)

    assert len(records) == 1
    assert len(records[0].payload["body"]) == 70_000


@pytest.mark.asyncio
async def test_oversized_jsonl_fails_promptly_instead_of_losing_sentinel(
    tmp_path: Path,
) -> None:
    """A pipe read error used to strand the consumer on ``queue.get()``."""

    script = _script(
        tmp_path,
        (
            "import json, time\n"
            "print(json.dumps({'body': 'x' * 4096}), flush=True)\n"
            "time.sleep(10)\n"
        ),
    )
    timeout_s = 5
    runner = AnalyzerRunner(
        sys.executable,
        timeout_s=timeout_s,
        max_record_bytes=1024,
        stream_limit_bytes=2048,
        terminate_grace_s=0.1,
    )

    started = time.monotonic()
    with pytest.raises(AnalyzerOutputError):
        await runner.run_collect(str(script), tmp_path)

    # The regression stranded the queue consumer until the overall analyzer
    # deadline.  Process startup can exceed two seconds on a loaded Windows CI
    # host, so assert the semantic boundary instead of a scheduler-sensitive
    # micro-benchmark: the output error must win before that deadline.
    assert time.monotonic() - started < timeout_s


@pytest.mark.asyncio
async def test_wall_timeout_terminates_long_running_analyzer(tmp_path: Path) -> None:
    script = _script(tmp_path, "import time\ntime.sleep(10)\n")
    runner = AnalyzerRunner(
        sys.executable,
        timeout_s=0.15,
        terminate_grace_s=0.1,
    )

    started = time.monotonic()
    with pytest.raises(AnalyzerTimeoutError) as raised:
        await runner.run_collect(str(script), tmp_path)

    assert raised.value.timeout_s == pytest.approx(0.15)
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_cancellation_does_not_wait_for_natural_child_exit(tmp_path: Path) -> None:
    script = _script(tmp_path, "import time\ntime.sleep(10)\n")
    runner = AnalyzerRunner(
        sys.executable,
        timeout_s=30,
        terminate_grace_s=0.1,
    )
    task = asyncio.create_task(runner.run_collect(str(script), tmp_path))
    await asyncio.sleep(0.1)

    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_nonzero_exit_raises_after_delivering_partial_records(
    tmp_path: Path,
) -> None:
    script = _script(
        tmp_path,
        (
            "import json, sys\n"
            "print(json.dumps({'record_type': 'symbol', 'id': 'partial'}), flush=True)\n"
            "sys.stderr.write('fatal\\n')\n"
            "sys.stderr.flush()\n"
            "raise SystemExit(23)\n"
        ),
    )
    runner = AnalyzerRunner(sys.executable)

    with pytest.raises(AnalyzerProcessError) as raised:
        await runner.run_collect(str(script), tmp_path)

    assert raised.value.exit_code == 23
    assert any(
        r.stream == "stdout" and r.payload.get("id") == "partial"
        for r in raised.value.partial_records
    )
    assert any(r.stream == "stderr" for r in raised.value.partial_records)


@pytest.mark.asyncio
async def test_tiny_bounded_queue_applies_backpressure_without_losing_records(
    tmp_path: Path,
) -> None:
    script = _script(
        tmp_path,
        (
            "import json\n"
            "for i in range(100):\n"
            "    print(json.dumps({'record_type': 'symbol', 'id': i}))\n"
        ),
    )
    runner = AnalyzerRunner(sys.executable, queue_maxsize=2)
    seen: list[int] = []

    async for record in runner.run(str(script), tmp_path):
        seen.append(record.payload["id"])
        await asyncio.sleep(0.001)

    assert seen == list(range(100))
