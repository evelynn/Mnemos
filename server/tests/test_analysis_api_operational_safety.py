from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Update

from app.api import analysis


def test_error_log_is_bounded_in_api_and_terminal_replay():
    long_error = "x" * 10_000
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status="failed",
        triggered_by="user:test",
        git_sha="HEAD",
        scope="full",
        started_at=None,
        completed_at=datetime.now(tz=timezone.utc),
        stats=None,
        error_log=long_error,
        created_at=datetime.now(tz=timezone.utc),
    )
    out = analysis._to_out(run)
    assert out.error_log is not None
    assert len(out.error_log) <= analysis._MAX_ERROR_LOG_CHARS

    payload = analysis._terminal_payload("failed", out.error_log)
    assert payload["error_log"] == out.error_log
    assert payload["error"] == out.error_log


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["get_queue", "enqueue_job"])
async def test_manual_enqueue_exception_terminalizes_run(
    monkeypatch, failure_point
):
    project_id = uuid.uuid4()

    class _Result:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=project_id)

    class _DB:
        def __init__(self):
            self.run = None
            self.commits = 0

        async def execute(self, statement):
            return _Result()

        def add(self, row):
            self.run = row

        async def commit(self):
            self.commits += 1

        async def refresh(self, row):
            row.created_at = datetime.now(tz=timezone.utc)

    class _Queue:
        async def enqueue_job(self, *args, **kwargs):
            raise ConnectionError("redis unavailable")

    async def _broken_queue():
        if failure_point == "get_queue":
            raise ConnectionError("redis unavailable")
        return _Queue()

    async def _audit(**kwargs):
        return None

    monkeypatch.setattr(analysis, "get_queue", _broken_queue)
    monkeypatch.setattr(analysis, "audit_record", _audit)
    monkeypatch.setattr(
        analysis,
        "resolve_project_source_path",
        lambda *_args, **_kwargs: SimpleNamespace(
            source_path=Path("C:/repo")
        ),
    )
    db = _DB()
    out = await analysis.trigger_analysis(
        project_id,
        analysis.AnalysisTriggerRequest(source_path="C:/repo"),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )
    assert out.status == "failed"
    assert out.completed_at is not None
    assert out.error_log == "analysis_enqueue_failed:ConnectionError"
    assert db.commits == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "partial"])
async def test_sse_periodic_db_recheck_replays_lost_terminal(
    monkeypatch, terminal_status
):
    run_id = uuid.uuid4()
    calls = 0
    error = "fatal:" + ("z" * 10_000)

    async def _state(_run_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "running", None
        return terminal_status, analysis._bounded_error_log(error)

    class _Bus:
        async def subscribe(self, _run_id):
            # Simulate a terminal Redis event lost before subscription. The
            # DB recheck must still close the stream with a terminal event.
            import asyncio

            await asyncio.Event().wait()
            yield {}  # pragma: no cover - makes this an async generator

    class _Request:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(analysis, "_load_run_state", _state)
    monkeypatch.setattr(analysis, "ProgressBus", _Bus)
    monkeypatch.setattr(analysis, "_SSE_DB_RECHECK_SEC", 0.001)

    response = await analysis.run_events(run_id, _Request())
    stream = response.body_iterator
    opened_chunk = await anext(stream)
    terminal_chunk = await anext(stream)
    opened = (
        opened_chunk.decode() if isinstance(opened_chunk, bytes) else opened_chunk
    )
    terminal = (
        terminal_chunk.decode() if isinstance(terminal_chunk, bytes) else terminal_chunk
    )
    assert "event: open" in opened
    data_line = next(line for line in terminal.splitlines() if line.startswith("data:"))
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["event"] == f"run_{terminal_status}"
    assert len(payload["error_log"]) <= analysis._MAX_ERROR_LOG_CHARS
    await stream.aclose()


@pytest.mark.asyncio
async def test_cancel_uses_one_conditional_update(monkeypatch):
    run_id = uuid.uuid4()
    project_id = uuid.uuid4()
    statements = []
    aborted = []

    class _Result:
        def one_or_none(self):
            return project_id, {"job_id": "analysis:one"}

    class _DB:
        async def execute(self, statement):
            statements.append(statement)
            return _Result()

        async def commit(self):
            return None

    class _Bus:
        async def publish(self, *args):
            return None

    class _Queue:
        async def abort_job(self, job_id):
            aborted.append(job_id)
            return True

    async def _queue():
        return _Queue()

    async def _audit(**kwargs):
        return None

    monkeypatch.setattr(analysis, "ProgressBus", _Bus)
    monkeypatch.setattr(analysis, "get_queue", _queue)
    monkeypatch.setattr(analysis, "audit_record", _audit)
    result = await analysis.cancel_run(
        run_id,
        SimpleNamespace(id=uuid.uuid4()),
        _DB(),
    )
    assert result == {"status": "cancelled"}
    assert len(statements) == 1
    assert isinstance(statements[0], Update)
    assert "status IN" in str(statements[0])
    assert aborted == ["analysis:one"]


def test_mutating_analysis_routes_require_operator():
    routes = {
        route.path: route
        for route in analysis.router.routes
        if hasattr(route, "dependant")
    }
    for path in (
        "/api/v1/projects/{project_id}/analyze",
        "/api/v1/analysis_runs/{run_id}/cancel",
    ):
        closure_values = []
        for dep in routes[path].dependant.dependencies:
            closure_values.extend(
                cell.cell_contents for cell in (dep.call.__closure__ or ())
            )
        assert "operator" in closure_values
