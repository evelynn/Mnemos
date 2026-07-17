"""Structured-output boundary and graph-grounding regression tests."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.extractor.agent_sdk import (
    SUMMARY_CANDIDATE_CONTRACT,
    SummaryCandidateContext,
    _summary_candidate,
    normalize_summary_candidate_payload,
)
from app.extractor.schema import (
    MAX_CLAIMS,
    ExtractorSchemaError,
    normalize_extractor_payload,
    parse_and_normalize_extractor_payload,
)
from app.extractor.validator import validate_claims

pytestmark = pytest.mark.usefixtures("fake_llm_attempt_callbacks")


def _payload() -> dict:
    return {
        "summary": "  Reads the current graph.  ",
        "detailed": "  Resolves callers from indexed CALLS edges.  ",
        "claims": [
            {
                "claim": "  The symbol has a caller.  ",
                "evidence": [
                    {
                        "kind": "node",
                        "node_id": "sym:caller",
                        "certainty": "verified",
                    }
                ],
            }
        ],
        "open_questions": ["  Is the runtime edge exercised?  "],
    }


def test_canonical_normalizer_is_pure_and_idempotent():
    raw = _payload()
    before = json.loads(json.dumps(raw))

    canonical = normalize_extractor_payload(raw)

    assert raw == before
    assert canonical == normalize_extractor_payload(canonical)
    assert canonical["summary"] == "Reads the current graph."
    assert canonical["claims"][0]["claim"] == "The symbol has a caller."


def test_summary_candidate_wrapper_is_pure_strict_and_idempotent():
    raw = {"contract": SUMMARY_CANDIDATE_CONTRACT, **_payload()}
    before = json.loads(json.dumps(raw))

    canonical = normalize_summary_candidate_payload(raw)
    candidate = _summary_candidate(
        canonical,
        context=SummaryCandidateContext(
            project_id=uuid.uuid4(),
            binding_fingerprint="a" * 64,
        ),
    )

    assert raw == before
    assert canonical == normalize_summary_candidate_payload(canonical)
    assert canonical["contract"] == SUMMARY_CANDIDATE_CONTRACT
    assert candidate.contract_name == SUMMARY_CANDIDATE_CONTRACT
    assert candidate.binding_fingerprint == "a" * 64
    assert candidate.payload == canonical


@pytest.mark.parametrize(
    "payload",
    [
        {"contract": SUMMARY_CANDIDATE_CONTRACT, "summary": "secret-value"},
        {
            "contract": SUMMARY_CANDIDATE_CONTRACT,
            **_payload(),
            "unexpected": "secret-value",
        },
        {"contract": "mnemos.summary.v9", **_payload()},
    ],
)
def test_summary_candidate_wrapper_rejects_drift_without_echo(payload):
    with pytest.raises((ExtractorSchemaError, ValueError)) as exc_info:
        normalize_summary_candidate_payload(payload)

    assert "secret-value" not in str(exc_info.value)


def test_supported_provider_dialects_converge_to_one_shape():
    canonical_text = json.dumps(_payload())
    dialects = [
        canonical_text,
        f"```json\n{canonical_text}\n```",
        f"Here is the requested result:\n{canonical_text}\nDone.",
    ]

    results = [parse_and_normalize_extractor_payload(text) for text in dialects]

    assert results[0] == results[1] == results[2]


def test_legacy_omissions_receive_explicit_canonical_defaults():
    result = normalize_extractor_payload({"summary": "Concise result"})

    assert result == {
        "summary": "Concise result",
        "detailed": "Concise result",
        "claims": [],
        "open_questions": [],
    }


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        (
            {
                "summary": "s",
                "claims": [{"claim": "ungrounded", "evidence": []}],
            },
            "$.claims[0].evidence",
        ),
        (
            {
                "summary": "s",
                "claims": [
                    {
                        "claim": "conflict",
                        "evidence": [
                            {
                                "kind": "node",
                                "node_id": "n",
                                "edge_id": "e",
                                "certainty": "verified",
                            }
                        ],
                    }
                ],
            },
            "$.claims[0].evidence[0].edge_id",
        ),
        ({"summary": 42}, "$.summary"),
        ({"summary": "db\x00break"}, "$.summary"),
        ({"summary": "invalid-\ud800-surrogate"}, "$.summary"),
        ({"summary": "s", "untrusted_metadata": {}}, "$"),
        (
            {
                "summary": "s",
                "claims": [
                    {
                        "claim": f"claim-{index}",
                        "evidence": [
                            {
                                "kind": "node",
                                "node_id": f"n:{index}",
                                "certainty": "verified",
                            }
                        ],
                    }
                    for index in range(MAX_CLAIMS + 1)
                ],
            },
            "$.claims",
        ),
    ],
)
def test_invalid_or_overloaded_payload_fails_with_actionable_path(payload, path):
    with pytest.raises(ExtractorSchemaError) as error:
        normalize_extractor_payload(payload)

    assert any(issue.path == path for issue in error.value.issues)
    # Diagnostics expose shape/length, not model/source text.
    assert "ungrounded" not in str(error.value)


def test_unknown_field_diagnostics_expose_only_static_path_and_count():
    payload = _payload()
    payload["source-secret-top"] = "must not appear"
    payload["claims"][0]["source-secret-claim"] = "must not appear"
    payload["claims"][0]["evidence"][0]["source-secret-evidence"] = (
        "must not appear"
    )

    with pytest.raises(ExtractorSchemaError) as error:
        normalize_extractor_payload(payload)

    diagnostics = str(error.value)
    assert "source-secret" not in diagnostics
    assert [issue.path for issue in error.value.issues[:3]] == [
        "$",
        "$.claims[0]",
        "$.claims[0].evidence[0]",
    ]
    assert all(
        issue.actual == "unknown_key_count=1"
        for issue in error.value.issues[:3]
    )


def test_multiple_json_objects_are_rejected_instead_of_silently_selected():
    with pytest.raises(ExtractorSchemaError) as error:
        parse_and_normalize_extractor_payload(
            '{"summary":"first"} {"summary":"second"}'
        )

    assert error.value.issues[0].path == "$"


@pytest.mark.parametrize(
    "wrapped",
    [
        '[{"summary":"nested"}]',
        'prefix [{"summary":"nested"}] suffix',
        '{"summary":"object"}]',
        '[{"summary":"object"}',
    ],
)
def test_json_container_tokens_cannot_launder_an_inner_object(wrapped):
    with pytest.raises(ExtractorSchemaError) as error:
        parse_and_normalize_extractor_payload(wrapped)

    assert error.value.issues[0].path == "$"
    assert "exactly one JSON object" in str(error.value)


@pytest.mark.parametrize(
    "malformed",
    [
        "prefix super-secret-token without an object",
        '{"summary":"super-secret-token",}',
        '{"summary":"ok"} {"secret":"super-secret-token"}',
    ],
)
def test_malformed_diagnostics_never_echo_provider_text(malformed):
    with pytest.raises(ExtractorSchemaError) as error:
        parse_and_normalize_extractor_payload(malformed)

    diagnostic = str(error.value)
    assert "super-secret-token" not in diagnostic
    assert "str(len=" in diagnostic


def test_refresh_migration_owns_hash_column_and_uuid_default():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0026_analysis_refresh_indexes.py"
    ).read_text(encoding="utf-8")

    assert 'sa.Column("evidence_hash", sa.String(length=64)' in migration
    assert "'_evidence_hash'" in migration
    assert 'server_default=sa.text("gen_random_uuid()")' in migration
    assert 'op.drop_column("summaries", "evidence_hash")' in migration


@pytest.mark.asyncio
async def test_agent_sdk_mock_envelope_runs_through_real_boundary(monkeypatch):
    from app.extractor import agent_sdk

    payload = json.dumps({"summary": "  SDK summary  "})

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content = content
            self.model = agent_sdk.AGENT_SDK_BUDGETED_MODEL
            self.error = None

    class ResultMessage:
        def __init__(self, is_error=False):
            self.is_error = is_error
            self.subtype = "success"
            self.stop_reason = "end_turn"

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    async def query(**_):
        yield AssistantMessage([TextBlock(f"```json\n{payload}\n```")])
        yield ResultMessage(False)

    fake_module = type(
        "FakeAgentSDK",
        (),
        {
            "AssistantMessage": AssistantMessage,
            "ClaudeAgentOptions": ClaudeAgentOptions,
            "ResultMessage": ResultMessage,
            "TextBlock": TextBlock,
            "query": query,
        },
    )
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    with patch.dict("sys.modules", {"claude_agent_sdk": fake_module}):
            result = await agent_sdk.summarize_via_agent_sdk(
                model=agent_sdk.AGENT_SDK_BUDGETED_MODEL,
            level=1,
            target_id="sym:x",
            evidence=[],
        )

    assert result == {
        "summary": "SDK summary",
        "detailed": "SDK summary",
        "claims": [],
        "open_questions": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "exception_name"),
    [
        ("transport", "AgentSDKTransportError"),
        ("result", "AgentSDKResultError"),
        ("output", "AgentSDKOutputLimitError"),
    ],
)
async def test_agent_sdk_real_generator_failures_remain_typed(
    monkeypatch,
    failure: str,
    exception_name: str,
) -> None:
    from app.extractor import agent_sdk

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class AssistantMessage:
        def __init__(self, content):
            self.content = content
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self, is_error=False):
            self.is_error = is_error
            self.subtype = "success"
            self.stop_reason = "end_turn"

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):
            pass

    async def query(**_kwargs):
        if failure == "transport":
            raise RuntimeError("provider detail must not escape")
        if failure == "result":
            yield ResultMessage(True)
            return
        yield AssistantMessage(
            [TextBlock("x" * (agent_sdk.MAX_AGENT_SUMMARY_OUTPUT_BYTES + 1))]
        )

    fake_module = type(
        "FakeAgentSDK",
        (),
        {
            "AssistantMessage": AssistantMessage,
            "ClaudeAgentOptions": ClaudeAgentOptions,
            "ResultMessage": ResultMessage,
            "TextBlock": TextBlock,
            "query": query,
        },
    )
    events: list[str] = []

    async def reserve() -> float:
        events.append("budget")
        return 30.0

    def dispatch() -> None:
        events.append("dispatch")

    expected = getattr(agent_sdk, exception_name)
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    with patch.dict("sys.modules", {"claude_agent_sdk": fake_module}):
        with pytest.raises(expected):
            await agent_sdk.summarize_via_agent_sdk(
                model="claude-sonnet-4-6",
                level=1,
                target_id="sym:x",
                evidence=[],
                on_provider_start=reserve,
                on_provider_dispatch=dispatch,
            )

    assert events == ["dispatch"]


@pytest.mark.asyncio
async def test_direct_provider_rejects_schema_invalid_reply(monkeypatch):
    from app.extractor.agent import (
        FALLBACK_ANTHROPIC_SCHEMA,
        Extractor,
    )

    response = SimpleNamespace(
        id="msg-schema-invalid",
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        content=[
            SimpleNamespace(
                type="text",
                text=json.dumps(
                    {
                        "summary": "looks plausible",
                        "claims": [{"claim": "but has no evidence", "evidence": []}],
                    }
                )
            )
        ],
        usage=SimpleNamespace(input_tokens=0, output_tokens=10),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=response)
    fake_anthropic = MagicMock(AsyncAnthropic=MagicMock(return_value=fake_client))

    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")
    extractor = Extractor()
    extractor._api_key = "test-key"
    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        result = await extractor.summarize(1, "sym:x", [])

    assert result.fallback_reason == FALLBACK_ANTHROPIC_SCHEMA
    assert result.model_used == f"stub:{FALLBACK_ANTHROPIC_SCHEMA}"


@pytest.mark.asyncio
async def test_failed_remote_response_never_charges_a_second_backend(monkeypatch):
    from app.extractor import agent_sdk
    from app.extractor.agent import Extractor

    response = MagicMock(
        content=[MagicMock(text='{"summary": 42}')],
        usage=MagicMock(input_tokens=100, output_tokens=10),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=response)
    fake_anthropic = MagicMock(AsyncAnthropic=MagicMock(return_value=fake_client))
    second_backend = AsyncMock(return_value={"summary": "second charge"})
    monkeypatch.setattr(agent_sdk, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(agent_sdk, "summarize_via_agent_sdk", second_backend)

    extractor = Extractor()
    extractor._api_key = "test-key"
    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        result = await extractor.summarize(1, "sym:x", [])

    assert result.model_used.startswith("stub:anthropic_")
    second_backend.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_provider_records_input_plus_output_tokens(monkeypatch):
    from app.extractor.agent import Extractor

    response = SimpleNamespace(
        id="msg-grounded",
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"summary": "grounded"}')],
        usage=SimpleNamespace(input_tokens=120, output_tokens=30),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=response)
    fake_anthropic = MagicMock(AsyncAnthropic=MagicMock(return_value=fake_client))
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")

    extractor = Extractor()
    extractor._api_key = "test-key"
    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        result = await extractor.summarize(1, "sym:x", [])

    assert result.tokens_used == 150


@pytest.mark.asyncio
async def test_direct_provider_ignores_non_text_envelope_blocks(monkeypatch):
    from app.extractor.agent import FALLBACK_ANTHROPIC_JSON, Extractor

    tool_block = SimpleNamespace(type="tool_use")
    response = SimpleNamespace(
        id="msg-no-text",
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
        content=[tool_block],
        usage=SimpleNamespace(input_tokens=4, output_tokens=2),
    )
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=response)
    fake_anthropic = MagicMock(AsyncAnthropic=MagicMock(return_value=fake_client))
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")

    extractor = Extractor()
    extractor._api_key = "test-key"
    with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        result = await extractor.summarize(1, "sym:x", [])

    assert result.fallback_reason == FALLBACK_ANTHROPIC_JSON
    assert result.tokens_used == 6


class _RowsResult:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return self._values


class _GroundingSession:
    def __init__(self, *, nodes=None, edges=None):
        self.nodes = dict(nodes or {})
        self.edges = dict(edges or {})
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        table_name = next(iter(statement.selected_columns)).table.name
        if table_name == "nodes":
            return _RowsResult(self.nodes.items())
        return _RowsResult(self.edges.items())


@pytest.mark.asyncio
async def test_grounding_accepts_existing_refs_and_scopes_edges_to_project():
    project_id = uuid.uuid4()
    edge_id = uuid.uuid4()
    session = _GroundingSession(
        nodes={"sym:known": "inferred"},
        edges={edge_id: "verified"},
    )
    claims = [
        {
            "claim": "Both facts exist",
            "evidence": [
                {
                    "kind": "node",
                    "node_id": "sym:known",
                    "certainty": "verified",
                },
                {
                    "kind": "edge",
                    "edge_id": str(edge_id),
                    "certainty": "asserted",
                },
            ],
        }
    ]

    accepted, rejected = await validate_claims(
        session,
        project_id=project_id,
        claims=claims,
        provided_evidence=[
            {"kind": "node", "node_id": "sym:known"},
            {"kind": "edge", "edge_id": str(edge_id)},
        ],
    )

    assert accepted[0]["evidence"][0]["certainty"] == "inferred"
    assert accepted[0]["evidence"][1]["certainty"] == "verified"
    # Producer data is immutable; its claimed certainty is not authoritative.
    assert claims[0]["evidence"][0]["certainty"] == "verified"
    assert rejected == []
    edge_statement = next(
        statement
        for statement in session.statements
        if next(iter(statement.selected_columns)).table.name == "edges"
    )
    assert "edges.project_id" in str(edge_statement)
    assert project_id in edge_statement.compile().params.values()


@pytest.mark.asyncio
async def test_grounding_rejects_existing_but_unprovided_reference():
    session = _GroundingSession(nodes={"sym:known": "asserted"})
    claim = {
        "claim": "Known but outside this prompt",
        "evidence": [
            {
                "kind": "node",
                "node_id": "sym:known",
                "certainty": "asserted",
            }
        ],
    }

    accepted, rejected = await validate_claims(
        session,
        project_id=uuid.uuid4(),
        claims=[claim],
        provided_evidence=[{"kind": "node", "node_id": "sym:other"}],
    )

    assert accepted == []
    assert rejected == [claim]
    assert session.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        None,
        {"claim": "not a list"},
        [None],
        [{"claim": "", "evidence": []}],
        [{"claim": "no evidence", "evidence": []}],
        [{"claim": "bad item", "evidence": [1]}],
        [
            {
                "claim": "missing certainty",
                "evidence": [{"kind": "node", "node_id": "sym:x"}],
            }
        ],
    ],
)
async def test_malformed_claims_never_crash_or_query(claims):
    session = _GroundingSession()

    accepted, rejected = await validate_claims(
        session, project_id=uuid.uuid4(), claims=claims
    )

    assert accepted == []
    assert rejected
    assert session.statements == []


@pytest.mark.asyncio
async def test_l1_zero_limit_is_a_hard_zero_without_db_or_provider_work():
    from app.extractor.runner import summarise_l1

    class NeverSession:
        async def execute(self, _statement):
            raise AssertionError("limit=0 must not query")

    class NeverExtractor:
        async def summarize(self, *_args, **_kwargs):
            raise AssertionError("limit=0 must not call a provider")

    produced = await summarise_l1(
        NeverSession(),  # type: ignore[arg-type]
        NeverExtractor(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        limit=0,
    )

    assert produced == 0


@pytest.mark.asyncio
async def test_mcp_filters_legacy_hash_control_claim():
    from app.mcp.queries import get_module_summary

    row = SimpleNamespace(
        target_id="sym:x",
        level=1,
        summary="summary",
        detailed="detail",
        claims=[
            {
                "claim": "_evidence_hash",
                "evidence": [
                    {
                        "kind": "node",
                        "node_id": "not-a-source-node",
                        "certainty": "asserted",
                    }
                ],
            },
            {
                "claim": "real claim",
                "evidence": [
                    {
                        "kind": "node",
                        "node_id": "sym:x",
                        "certainty": "verified",
                    }
                ],
            },
        ],
        open_questions=[],
        evidence_hash="a" * 64,
        generated_at=datetime.now(tz=timezone.utc),
        model_used="legacy",
    )

    class Result:
        def __init__(self, *, scalar=None, rows=None):
            self.scalar = scalar
            self.rows = rows or []

        def scalar_one_or_none(self):
            return self.scalar

        def all(self):
            return self.rows

    class Session:
        async def execute(self, statement):
            table = next(iter(statement.selected_columns)).table.name
            if table == "summaries":
                return Result(scalar=row)
            assert table == "nodes"
            return Result(rows=[("sym:x", "asserted")])

    result = await get_module_summary(
        Session(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        target_id="sym:x",
        level=1,
    )

    assert result is not None
    assert [claim["claim"] for claim in result["claims"]] == ["real claim"]
    assert result["certainty_breakdown"] == {
        "verified": 0,
        "asserted": 1,
        "inferred": 0,
    }
    assert result["evidence_hash"] == "a" * 64
    assert result["grounding_status"] == "grounded"
    assert result["fallback_reason"] is None
    assert result["narrative_certainty"] == "inferred"


@pytest.mark.asyncio
async def test_get_symbol_keeps_prose_compatibility_and_exposes_fallback_metadata():
    from app.mcp.queries import get_symbol

    node = SimpleNamespace(
        id="sym:x",
        kind="Symbol",
        data={"name": "x"},
        certainty="asserted",
    )
    run_id = uuid.uuid4()
    summary = SimpleNamespace(
        summary="explicit fallback prose",
        claims=[],
        model_used="stub:ungrounded_response",
        fallback_reason="ungrounded_response",
        analysis_run_id=run_id,
    )

    class Result:
        def __init__(self, *, scalar=None, rows=None):
            self.scalar = scalar
            self.rows = rows or []

        def scalar_one_or_none(self):
            return self.scalar

        def all(self):
            return self.rows

        def scalars(self):
            return self

    class Session:
        def __init__(self):
            self.results = iter(
                [
                    Result(scalar=node),
                    Result(rows=[]),
                    Result(rows=[]),
                    Result(scalar=summary),
                    Result(rows=[]),
                ]
            )

        async def execute(self, _statement):
            return next(self.results)

    result = await get_symbol(
        Session(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        symbol_id=node.id,
    )

    assert result is not None
    assert result["l1_summary"] == "explicit fallback prose"
    assert result["l1_summary_meta"] == {
        "model_used": "stub:ungrounded_response",
        "fallback_reason": "ungrounded_response",
        "grounding_status": "fallback",
        "narrative_certainty": "inferred",
        "claims": [],
        "analysis_run_id": str(run_id),
    }


@pytest.mark.asyncio
async def test_summary_api_marks_fallback_and_hides_legacy_control_claim():
    from app.api.analysis import list_summaries

    run_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        target_id="sym:x",
        level=1,
        summary="fallback",
        detailed="fallback",
        claims=[
            {
                "claim": "_evidence_hash",
                "evidence": [
                    {
                        "kind": "node",
                        "node_id": "control-only",
                        "certainty": "asserted",
                    }
                ],
            }
        ],
        open_questions=[],
        model_used="stub:ungrounded_response",
        fallback_reason="ungrounded_response",
        analysis_run_id=run_id,
        tokens_used=7,
        generated_at=datetime.now(tz=timezone.utc),
    )

    class ScalarRows:
        def all(self):
            return [row]

    class Result:
        def scalars(self):
            return ScalarRows()

    class Session:
        async def execute(self, _statement):
            return Result()

    result = await list_summaries(
        uuid.uuid4(),
        None,  # type: ignore[arg-type]
        db=Session(),  # type: ignore[arg-type]
    )

    assert result[0]["claims"] == []
    assert result[0]["grounding_status"] == "fallback"
    assert result[0]["fallback_reason"] == "ungrounded_response"
    assert result[0]["narrative_certainty"] == "inferred"
    assert result[0]["analysis_run_id"] == str(run_id)


@pytest.mark.asyncio
async def test_summary_api_drops_stale_claims_and_bounds_pagination():
    from app.api.analysis import list_summaries

    row = SimpleNamespace(
        id=uuid.uuid4(),
        target_id="sym:stale",
        level=1,
        summary="Previously grounded prose.",
        detailed="Previously grounded prose.",
        claims=[
            {
                "claim": "The old node existed.",
                "evidence": [
                    {
                        "kind": "node",
                        "node_id": "sym:removed",
                        "certainty": "verified",
                    }
                ],
            }
        ],
        open_questions=[],
        model_used="claude",
        fallback_reason=None,
        analysis_run_id=uuid.uuid4(),
        tokens_used=7,
        generated_at=datetime.now(tz=timezone.utc),
    )

    class ScalarRows:
        def all(self):
            return [row]

    class Result:
        def __init__(self, *, summary_rows=False):
            self.summary_rows = summary_rows

        def scalars(self):
            assert self.summary_rows
            return ScalarRows()

        def all(self):
            assert not self.summary_rows
            return []

    class Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            table = next(iter(statement.selected_columns)).table.name
            return Result(summary_rows=table == "summaries")

    session = Session()
    result = await list_summaries(
        uuid.uuid4(),
        None,  # type: ignore[arg-type]
        limit=500,
        offset=-10,
        db=session,  # type: ignore[arg-type]
    )

    assert result[0]["claims"] == []
    assert result[0]["grounding_status"] == "stale"
    summary_statement = session.statements[0]
    assert summary_statement._limit_clause.value == 50
    assert summary_statement._offset_clause.value == 0


@pytest.mark.asyncio
async def test_persisted_claim_revalidation_uses_real_current_sqlite_graph():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.extractor.validator import current_summary_claim_views
    from app.models.graph import Node
    from app.models.overlays import GraphNodeHumanOverlay
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(GraphNodeHumanOverlay.__table__.create)

    project_id = uuid.uuid4()
    valid_from = datetime.now(tz=timezone.utc)
    summary = SimpleNamespace(
        claims=[
            {
                "claim": "The symbol exists.",
                "evidence": [
                    {
                        "kind": "node",
                        "node_id": "sym:current",
                        "certainty": "verified",
                    }
                ],
            }
        ],
        model_used="claude",
        fallback_reason=None,
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        node = Node(
            id="sym:current",
            project_id=project_id,
            valid_from=valid_from,
            kind="Symbol",
            data={"name": "current"},
            certainty="inferred",
            created_by=["test"],
            valid_to=None,
        )
        session.add(node)
        await session.commit()

        grounded = (
            await current_summary_claim_views(
                session,
                project_id=project_id,
                summaries=[summary],
            )
        )[0]
        node.valid_to = datetime.now(tz=timezone.utc)
        await session.commit()
        stale = (
            await current_summary_claim_views(
                session,
                project_id=project_id,
                summaries=[summary],
            )
        )[0]

    await engine.dispose()
    assert grounded.grounding_status == "grounded"
    assert grounded.claims[0]["evidence"][0]["certainty"] == "inferred"
    assert stale.grounding_status == "stale"
    assert stale.claims == []


@pytest.mark.asyncio
async def test_claim_validation_uses_portable_900_reference_batches():
    from sqlalchemy.dialects import postgresql, sqlite

    from app.extractor.validator import validate_claims

    reference_counts: list[tuple[str, int]] = []

    class EmptyRows:
        def all(self):
            return []

    class RecordingSession:
        async def execute(self, statement):
            for name, dialect in (
                ("sqlite", sqlite.dialect()),
                ("postgresql", postgresql.dialect()),
            ):
                compiled = statement.compile(
                    dialect=dialect,
                    compile_kwargs={"render_postcompile": True},
                )
                # One non-reference bind scopes every lookup to project_id.
                reference_counts.append((name, len(compiled.params) - 1))
            return EmptyRows()

    evidence = [
        {
            "kind": "node",
            "node_id": f"sym:portable-batch:{index}",
            "certainty": "asserted",
        }
        for index in range(1_801)
    ]
    accepted, rejected = await validate_claims(
        RecordingSession(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        claims=[{"claim": "Bounded lookup.", "evidence": evidence}],
    )

    assert accepted == []
    assert rejected
    for dialect_name in ("sqlite", "postgresql"):
        assert [
            count for name, count in reference_counts if name == dialect_name
        ] == [900, 900, 1]


@pytest.mark.asyncio
async def test_summary_revalidation_stays_within_real_sqlite_variable_limit():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.extractor.validator import current_summary_claim_views
    from app.models.graph import Node
    from app.models.overlays import GraphNodeHumanOverlay
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(GraphNodeHumanOverlay.__table__.create)

    next_reference = 0
    summaries = []
    # 17 * 64 * 32 = 34,816 distinct binds.  One unchunked IN predicate
    # exceeds stock SQLite's 32,766-variable ceiling.
    for summary_index in range(17):
        claims = []
        for claim_index in range(64):
            evidence = []
            for _ in range(32):
                evidence.append(
                    {
                        "kind": "node",
                        "node_id": f"sym:sqlite-limit:{next_reference}",
                        "certainty": "asserted",
                    }
                )
                next_reference += 1
            claims.append(
                {
                    "claim": f"Claim {summary_index}-{claim_index}",
                    "evidence": evidence,
                }
            )
        summaries.append(
            SimpleNamespace(
                claims=claims,
                model_used="test-provider",
                fallback_reason=None,
            )
        )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        views = await current_summary_claim_views(
            session,
            project_id=uuid.uuid4(),
            summaries=summaries,
        )

    await engine.dispose()
    assert next_reference == 34_816
    assert len(views) == 17
    assert all(view.grounding_status == "stale" for view in views)
    assert all(view.claims == [] for view in views)
