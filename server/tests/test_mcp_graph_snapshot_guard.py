from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from mcp.types import CallToolRequest, CallToolRequestParams

from app.graph_publication import (
    GraphGenerationChanged,
    GraphHeadNeedsRebuild,
    GraphReadStamp,
)
from app.mcp import server as mcp_server
from app.mcp.auth import MCPAuthContext


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


async def _call(server, name: str, arguments: dict) -> dict:
    handler = server.request_handlers[CallToolRequest]
    response = await handler(
        CallToolRequest(
            params=CallToolRequestParams(name=name, arguments=arguments)
        )
    )
    return json.loads(response.root.content[0].text)


def _stamp(project_id: uuid.UUID) -> GraphReadStamp:
    return GraphReadStamp(
        project_id=project_id,
        generation=3,
        overlay_generation=7,
        current_run_id=uuid.uuid4(),
        published_at=datetime.now(tz=timezone.utc),
    )


def _auth_context(project_id: uuid.UUID) -> MCPAuthContext:
    return MCPAuthContext(
        api_key_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        username="snapshot-test",
        role="operator",
        organization_id=uuid.uuid4(),
        project_id=project_id,
        token_hash="0" * 64,
    )


async def _allow_auth(*_args, **_kwargs):
    return None


def test_only_index_and_explicit_historical_file_read_bypass_current_graph_guard():
    for name in mcp_server._KNOWN_TOOL_NAMES:
        expected = name not in {"get_project_index"}
        assert mcp_server._requires_published_graph(name, {}) is expected

    assert not mcp_server._requires_published_graph(
        "read_file", {"run_id": str(uuid.uuid4())}
    )
    assert mcp_server._requires_published_graph("read_file", {"run_id": None})
    assert not mcp_server._requires_published_graph("unknown", {})


async def test_current_graph_tool_returns_structured_full_repair_error(monkeypatch):
    project_id = uuid.uuid4()
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: _SessionContext())
    monkeypatch.setattr(mcp_server, "revalidate_mcp_auth_context", _allow_auth)

    async def read_unavailable(_db, *, project_id):
        assert project_id == project_id_under_test
        raise GraphHeadNeedsRebuild("full staged rebuild required")

    async def no_audit(**_kwargs):
        return None

    async def must_not_query(*_args, **_kwargs):
        raise AssertionError("unsafe current graph query was executed")

    project_id_under_test = project_id
    monkeypatch.setattr(mcp_server, "read_graph_stamp", read_unavailable)
    monkeypatch.setattr(mcp_server, "audit_record", no_audit)
    monkeypatch.setattr(mcp_server, "search_symbols", must_not_query)

    result = await _call(
        mcp_server.build_server(_auth_context(project_id)),
        "search_symbols",
        {"query": "target"},
    )

    assert result["schema"] == "mnemos.error.v1"
    assert result["error"] == mcp_server.GRAPH_SNAPSHOT_UNAVAILABLE_CODE
    assert result["repair"]["scope"] == "full"


async def test_current_graph_tool_discards_payload_if_head_changes(monkeypatch):
    project_id = uuid.uuid4()
    stamp = _stamp(project_id)
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: _SessionContext())
    monkeypatch.setattr(mcp_server, "revalidate_mcp_auth_context", _allow_auth)

    async def read_stamp(_db, *, project_id):
        assert project_id == stamp.project_id
        return stamp

    async def generation_changed(_db, *, stamp):
        raise GraphGenerationChanged(f"generation {stamp.generation} changed")

    async def search(*_args, **_kwargs):
        return {"results": [{"id": "must-not-escape"}]}

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(mcp_server, "read_graph_stamp", read_stamp)
    monkeypatch.setattr(mcp_server, "revalidate_graph_stamp", generation_changed)
    monkeypatch.setattr(mcp_server, "search_symbols", search)
    monkeypatch.setattr(mcp_server, "audit_record", no_audit)

    result = await _call(
        mcp_server.build_server(_auth_context(project_id)),
        "search_symbols",
        {"query": "target"},
    )
    assert result["error"] == mcp_server.GRAPH_SNAPSHOT_CHANGED_CODE
    assert result["retryable"] is True
    assert result["snapshot"]["overlay_generation"] == 7
    assert "results" not in result


async def test_project_index_remains_available_for_repair_diagnosis(monkeypatch):
    project_id = uuid.uuid4()
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: _SessionContext())
    monkeypatch.setattr(mcp_server, "revalidate_mcp_auth_context", _allow_auth)

    async def guard_must_not_run(*_args, **_kwargs):
        raise AssertionError("get_project_index must remain available")

    async def build_index(_db, *, project_id, top_k):
        return {"project_id": str(project_id), "top_k": top_k}

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(mcp_server, "read_graph_stamp", guard_must_not_run)
    monkeypatch.setattr(mcp_server, "build_project_index", build_index)
    monkeypatch.setattr(mcp_server, "audit_record", no_audit)

    result = await _call(
        mcp_server.build_server(_auth_context(project_id)),
        "get_project_index",
        {"top_k": 7},
    )
    assert result == {"project_id": str(project_id), "top_k": 7}


async def test_explicit_historical_file_read_bypasses_current_graph_guard(
    monkeypatch,
):
    from app.mcp import file_read

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: _SessionContext())
    monkeypatch.setattr(mcp_server, "revalidate_mcp_auth_context", _allow_auth)

    async def guard_must_not_run(*_args, **_kwargs):
        raise AssertionError("historical read_file must bypass the current graph")

    async def read_historical(
        _db,
        *,
        project_id,
        file_path,
        start_line,
        end_line,
        run_id,
    ):
        return {
            "project_id": str(project_id),
            "run_id": str(run_id),
            "path": file_path,
            "range": [start_line, end_line],
        }

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(mcp_server, "read_graph_stamp", guard_must_not_run)
    monkeypatch.setattr(file_read, "read_project_file", read_historical)
    monkeypatch.setattr(mcp_server, "audit_record", no_audit)

    result = await _call(
        mcp_server.build_server(_auth_context(project_id)),
        "read_file",
        {
            "run_id": str(run_id),
            "file_path": "src/main.py",
            "start_line": 2,
            "end_line": 4,
        },
    )
    assert result == {
        "project_id": str(project_id),
        "run_id": str(run_id),
        "path": "src/main.py",
        "range": [2, 4],
    }
