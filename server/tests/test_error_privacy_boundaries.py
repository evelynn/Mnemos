from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import analysis
from app.artifacts import agent_context
from app.mcp import server as mcp_server
from app.obs.errors import _safe_validation_errors
from app.orchestrator import progress, source_manifest
from app.orchestrator.failure_codes import sanitize_public_failure_code


SECRET_SENTINEL = "privacy-sentinel-postgresql://admin:secret@private.invalid/db"


@pytest.mark.asyncio
async def test_enqueue_exception_never_reaches_db_response_or_caplog(
    monkeypatch,
    caplog,
) -> None:
    project_id = uuid.uuid4()

    class _Result:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=project_id)

    class _DB:
        def __init__(self) -> None:
            self.run = None

        async def execute(self, _statement):
            return _Result()

        def add(self, row) -> None:
            self.run = row

        async def commit(self) -> None:
            return None

        async def refresh(self, row) -> None:
            row.created_at = datetime.now(tz=timezone.utc)

    async def _broken_queue():
        raise ConnectionError(SECRET_SENTINEL)

    async def _audit(**_kwargs) -> None:
        return None

    monkeypatch.setattr(analysis, "get_queue", _broken_queue)
    monkeypatch.setattr(analysis, "audit_record", _audit)
    monkeypatch.setattr(
        analysis,
        "resolve_project_source_path",
        lambda *_args, **_kwargs: SimpleNamespace(source_path=Path("C:/repo")),
    )
    db = _DB()
    with caplog.at_level(logging.ERROR):
        response = await analysis.trigger_analysis(
            project_id,
            analysis.AnalysisTriggerRequest(source_path="C:/repo"),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )

    assert db.run.error_log == "analysis_enqueue_failed"
    assert response.error_log == "analysis_enqueue_failed"
    assert SECRET_SENTINEL not in repr(db.run)
    assert SECRET_SENTINEL not in response.model_dump_json()
    assert SECRET_SENTINEL not in caplog.text


def test_legacy_error_text_never_reaches_api_or_sse_event() -> None:
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status="failed",
        triggered_by="test",
        git_sha="a" * 40,
        scope="full",
        started_at=None,
        completed_at=datetime.now(tz=timezone.utc),
        stats=None,
        error_log=SECRET_SENTINEL,
        created_at=datetime.now(tz=timezone.utc),
    )

    response = analysis._to_out(run)
    event = analysis._terminal_payload("failed", SECRET_SENTINEL)

    assert response.error_log == "analysis_failure_details_unavailable"
    assert event["error"] == "analysis_failure_details_unavailable"
    assert SECRET_SENTINEL not in response.model_dump_json()
    assert SECRET_SENTINEL not in json.dumps(event)


@pytest.mark.parametrize(
    "code",
    [
        "analysis_enqueue_failed",
        "analysis_timeout",
        "postprocess_internal_failure",
        "stage_io_failure",
        "analyzer_output_incomplete",
    ],
)
def test_existing_safe_failure_codes_remain_compatible(code: str) -> None:
    assert sanitize_public_failure_code(code) == code


def test_mcp_audit_and_serializer_never_coerce_or_persist_content() -> None:
    details = mcp_server._mcp_audit_details(
        "submit_plan",
        {
            "spec": {"prompt": SECRET_SENTINEL},
            "tasks": [{"body": SECRET_SENTINEL}],
            "target_component_id": SECRET_SENTINEL,
            f"unknown-{SECRET_SENTINEL}": SECRET_SENTINEL,
        },
    )

    class _SecretObject:
        def __str__(self) -> str:
            return SECRET_SENTINEL

    serialized = mcp_server._cap_response({"value": _SecretObject()})

    assert details["schema"] == "mnemos.mcp_audit.v1"
    assert details["unknown_argument_count"] == 1
    assert SECRET_SENTINEL not in json.dumps(details, sort_keys=True)
    assert json.loads(serialized)["error"] == "mcp_response_contract_invalid"
    assert SECRET_SENTINEL not in serialized


def test_agent_artifact_omits_nested_provider_payload_and_unknown_objects() -> None:
    class _SecretObject:
        def __str__(self) -> str:
            return SECRET_SENTINEL

    projected = agent_context._bounded_value(
        {
            "provider_metadata": {
                "prompt": SECRET_SENTINEL,
                "response_body": SECRET_SENTINEL,
            },
            "unknown": _SecretObject(),
        }
    )
    serialized = json.dumps(projected, sort_keys=True)

    assert SECRET_SENTINEL not in serialized
    assert projected["provider_metadata"]["prompt"]["omitted"] == "raw_payload"
    assert projected["unknown"] == {"omitted": "unsupported_type"}


@pytest.mark.asyncio
async def test_malformed_progress_message_emits_static_event_only() -> None:
    class _PubSub:
        async def subscribe(self, _channel) -> None:
            return None

        async def listen(self):
            yield {"type": "message", "data": f"not-json:{SECRET_SENTINEL}"}

        async def unsubscribe(self, _channel) -> None:
            return None

        async def close(self) -> None:
            return None

    class _Client:
        def pubsub(self):
            return _PubSub()

    bus = progress.ProgressBus()
    bus._client = _Client()
    stream = bus.subscribe(uuid.uuid4())
    event = await anext(stream)
    await stream.aclose()

    assert event == {
        "event": "progress_unavailable",
        "error": "progress_event_invalid",
    }
    assert SECRET_SENTINEL not in json.dumps(event)


def test_source_manifest_failure_diagnostics_are_static(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "analyzer"
    executable.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(source_manifest.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(
        source_manifest,
        "_hash_file",
        lambda _path: (_ for _ in ()).throw(OSError(SECRET_SENTINEL)),
    )

    marker = source_manifest._producer_runtime_marker("ggoss-py")

    assert marker == "unreadable"
    assert SECRET_SENTINEL not in marker


def test_validation_error_projection_keeps_path_but_drops_input_and_context() -> None:
    class _ValidationFailure:
        def errors(self):
            return [
                {
                    "type": "value_error",
                    "loc": ("body", "field"),
                    "msg": SECRET_SENTINEL,
                    "input": SECRET_SENTINEL,
                    "ctx": {"error": ValueError(SECRET_SENTINEL)},
                }
            ]

    projected = _safe_validation_errors(_ValidationFailure())

    assert projected == [
        {
            "type": "value_error",
            "loc": ["body", "field"],
            "msg": "invalid_value",
        }
    ]
    assert SECRET_SENTINEL not in json.dumps(projected)
