"""PR-143 — cross-tier flow / process analysis via Claude Code.

Mnemos's deeper promise (spec §2: "boundaries are joined by contracts")
is to explain how a *process* runs end-to-end across tiers — frontend →
backend → database — not just to list symbols per file. An operator
asking "what happens when a user places an order?" wants the whole
trace: which request crosses each boundary, every field and flag value
in those signals, what each value MEANS, where it is formed, and which
rows it reads/writes.

This module hands the relevant source slices from every tier to the
operator's Claude Code credential context and asks for that structured trace.
The result is persisted as a level-4 Summary (``flow:<slug>``) so it
joins the knowledge graph and is queryable like any other summary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

from app.extractor.agent_sdk import (
    AGENT_SDK_BUDGETED_MODEL,
    AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS,
    AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS,
    AGENT_SDK_TERMINAL_ACCEPTED,
    AGENT_SDK_TERMINAL_OUTPUT_LIMIT,
    AgentSDKAssistantContract,
    AgentSDKAuthContract,
    _parse_json_response,
    agent_sdk_isolation_options,
    inspect_agent_sdk_result,
    is_agent_sdk_available,
)
from app.extractor.cost import LLMRunBudget
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMUsageContractError,
    LLMUsageV1,
    ResultStatus,
    UsageSource,
    normalize_agent_sdk_usage,
    unavailable_usage,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptReplayState,
    AttemptStartMetadata,
    AttemptTicket,
    LLMAttemptReplayBlocked,
    LLMLifecycleError,
    LLMSemanticCandidateCorrupt,
    SemanticCandidate,
    SemanticCandidateReplayRequest,
    fingerprint_input,
    require_attempt_callbacks,
)

log = logging.getLogger(__name__)

FLOW_LEVEL = 4  # L1-L3 are symbol/module/component; L4 is a cross-tier flow.
FLOW_RESULT_CONTRACT = "mnemos.flow_result.v1"
DEFAULT_FLOW_MODEL = "claude-sonnet-4-6"

FLOW_FALLBACK_OUTPUT_LIMIT = "flow_output_limit_exceeded"
FLOW_FALLBACK_PROVIDER_TIMEOUT = "flow_provider_timeout"
FLOW_FALLBACK_RUN_DEADLINE = "run_deadline_exceeded"
FLOW_FALLBACK_PROVIDER_ERROR = "flow_provider_error"
FLOW_FALLBACK_CANCELLED = "flow_call_cancelled"
FLOW_FALLBACK_PROVIDER_REJECTED = "flow_provider_rejected"
FLOW_FALLBACK_EMPTY_RESPONSE = "flow_empty_response"
FLOW_FALLBACK_PARSE_REJECTED = "flow_parse_rejected"
FLOW_FALLBACK_CONTRACT_REJECTED = "flow_contract_rejected"
FLOW_FALLBACK_PREDISPATCH = "flow_pre_dispatch_guard_failed"
FLOW_FALLBACK_USAGE_REJECTED = "flow_usage_contract_invalid"

FlowBeforeProviderCall = Callable[[], Awaitable[None]]
FlowPhysicalCallRecorder = Callable[[str, str | None, str], Awaitable[None]]


def flow_candidate_binding_fingerprint(
    semantic_binding_identity: str,
    source_scope: Mapping[str, Any],
) -> str:
    """Canonical consumer/provider binding shared with product publication."""

    return fingerprint_input(
        "mnemos.flow_result.binding.v1",
        semantic_binding_identity,
        json.dumps(
            source_scope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    )

# The provider response is untrusted input.  These limits are deliberately
# lower than the API/source budgets so one malformed model response cannot
# become an unbounded JSONB row or an oversized MCP response later.
MAX_AGENT_OUTPUT_BYTES = 256 * 1024
MAX_SOURCE_PROMPT_CHARS = 100_000
MAX_SUMMARY_CHARS = 200
MAX_DETAILED_CHARS = 20_000
MAX_STEPS = 100
MAX_FIELDS = 100
MAX_FLAGS = 100
MAX_VALUES = 50
MAX_DATA_TOUCHED = 100
MAX_COLUMNS = 100
MAX_OPEN_QUESTIONS = 50

_TIERS = frozenset({"frontend", "backend", "database"})
_SIGNAL_KINDS = frozenset(
    {"http_request", "http_response", "function_call", "sql"}
)
_DATA_OPERATIONS = frozenset({"INSERT", "UPDATE", "SELECT", "DELETE"})

_FLOW_KEYS = frozenset(
    {"contract", "summary", "detailed", "steps", "flags", "data_touched", "open_questions"}
)
_STEP_KEYS = frozenset({"order", "tier", "component", "action", "signal"})
_SIGNAL_KEYS = frozenset({"kind", "name", "fields"})
_FIELD_KEYS = frozenset({"name", "type", "values", "meaning"})
_FLAG_KEYS = frozenset({"name", "type", "values", "set_at", "used_at"})
_FLAG_VALUE_KEYS = frozenset({"value", "meaning"})
_DATA_KEYS = frozenset({"entity", "op", "columns"})

# ``Summary.claims`` predates L4 and is the physical JSONB slot available for
# persisted structured content.  L4 does *not* store generic source claims in
# that slot: it stores this contract-discriminated, complete section set.  The
# discriminator keeps the two schemas unambiguous and lets every consumer use
# one normalizer instead of guessing between ``{claim, evidence}`` and flow
# sections independently.
FLOW_SUMMARY_SECTION_ORDER = (
    "source_snapshot",
    "steps",
    "flags",
    "data_touched",
)
_FLOW_SUMMARY_SECTION_NAMES = frozenset(FLOW_SUMMARY_SECTION_ORDER)
_FLOW_SUMMARY_SECTION_KEYS = frozenset({"contract", "section", "data"})
_SOURCE_SNAPSHOT_KEYS = frozenset(
    {"analysis_run_id", "revision", "files", "file_windows", "omitted_files"}
)
_SOURCE_WINDOW_KEYS = frozenset(
    {
        "label",
        "tier",
        "language",
        "provided_chars",
        "included_chars",
        "prompt_truncated",
        "input_truncated",
    }
)
_OMITTED_SOURCE_KEYS = frozenset({"label", "reason"})
_OMITTED_SOURCE_REASONS = frozenset({"source_budget", "empty_source"})
MAX_FLOW_SOURCE_FILES = 30
MAX_FLOW_SOURCE_CHARS = 400_000

_FLOW_SYSTEM = (
    "You are a senior systems analyst tracing one end-to-end process across "
    "a multi-tier system (frontend, backend, database). You receive source "
    "slices from each tier and return ONLY a JSON object — no prose, no "
    "markdown fences. Trace the named process across every boundary. For "
    "each signal that crosses a boundary (HTTP request/response, function "
    "call, SQL statement) enumerate its fields and flags, and for EACH flag "
    "or enumerated value give its concrete values and what each value MEANS "
    "and where it is set/derived. Be precise and grounded in the given "
    "source; do not invent fields that aren't there. Note anything you "
    "cannot determine in open_questions."
)


def _build_flow_prompt(entry: str, sources: list[dict[str, Any]]) -> str:
    parts = [
        f"Process to trace: {entry}\n",
        "Return a JSON object with this exact shape:\n",
        "{\n"
        '  "summary": "<=200 chars one-line of the whole flow",\n'
        '  "detailed": "step-by-step prose of the end-to-end flow",\n'
        '  "steps": [\n'
        '    {"order": <int>, "tier": "frontend|backend|database",\n'
        '     "component": "<file or symbol>", "action": "<what happens>",\n'
        '     "signal": {"kind": "http_request|http_response|function_call|sql",\n'
        '                "name": "<e.g. POST /api/orders>",\n'
        '                "fields": [{"name": "<field>", "type": "<type>",\n'
        '                            "values": ["<allowed values if enum/flag>"],\n'
        '                            "meaning": "<what it means / how formed>"}]}}\n'
        "  ],\n"
        '  "flags": [{"name": "<flag>", "type": "<type>",\n'
        '             "values": [{"value": <v>, "meaning": "<meaning>"}],\n'
        '             "set_at": "<tier/component>", "used_at": "<tier/component>"}],\n'
        '  "data_touched": [{"entity": "<table>", "op": "INSERT|UPDATE|SELECT|DELETE",\n'
        '                    "columns": ["<col>"]}],\n'
        '  "open_questions": ["<unknowns>"]\n'
        "}\n\n"
        "Source slices by tier:\n",
    ]
    for s in sources:
        truncation = " source_window=truncated" if s.get("truncated") else ""
        parts.append(
            f"\n----- tier={s.get('tier','?')} file={s.get('label','?')} "
            f"({s.get('language','?')}){truncation} -----\n"
            "````\n"
            f"{s.get('code','')}\n"
            "````\n"
        )
    return "".join(parts)


def _complete_line_prefix(text: str, limit: int) -> str:
    """Return a bounded prefix ending at a complete source line when possible."""

    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    prefix = text[:limit]
    if "\n" in prefix:
        prefix = prefix.rsplit("\n", 1)[0]
    return prefix


def _pack_flow_sources(
    sources: list[dict[str, Any]], *, max_total_chars: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pack source windows fairly and return deterministic prompt provenance.

    Every non-empty input receives a share of the remaining budget, so one
    giant first file cannot silently hide every later tier.  Partial windows
    end on a complete line where possible and are marked in both the prompt
    and returned scope.  The model never owns this metadata.
    """

    budget = min(max(max_total_chars, 0), MAX_SOURCE_PROMPT_CHARS)
    candidates = [
        source
        for source in sources
        if isinstance(source, dict)
        and isinstance(source.get("code"), str)
        and source["code"]
    ]
    packed: list[dict[str, Any]] = []
    scope_files: list[dict[str, Any]] = []
    omitted_files: list[dict[str, str]] = []
    remaining = budget

    for index, source in enumerate(candidates):
        code = source["code"]
        remaining_items = len(candidates) - index
        share = remaining // remaining_items if remaining_items else 0
        excerpt = _complete_line_prefix(code, share)
        label = str(source.get("label") or "?")
        if not excerpt:
            omitted_files.append({"label": label, "reason": "source_budget"})
            continue

        prompt_truncated = len(excerpt) < len(code)
        input_truncated = bool(source.get("truncated"))
        packed_source = {
            **source,
            "code": excerpt,
            "truncated": prompt_truncated or input_truncated,
        }
        packed.append(packed_source)
        scope_files.append(
            {
                "label": label,
                "tier": str(source.get("tier") or "unknown"),
                "language": str(source.get("language") or "unknown"),
                "provided_chars": len(code),
                "included_chars": len(excerpt),
                "prompt_truncated": prompt_truncated,
                "input_truncated": input_truncated,
            }
        )
        remaining -= len(excerpt)

    provided_labels = [
        str(source.get("label") or "?")
        for source in sources
        if isinstance(source, dict)
    ]
    included_labels = [item["label"] for item in scope_files]
    omitted_labels = {item["label"] for item in omitted_files}
    for label in provided_labels:
        if label not in included_labels and label not in omitted_labels:
            omitted_files.append({"label": label, "reason": "empty_source"})

    return packed, {
        "provided_files": provided_labels,
        "files": scope_files,
        "omitted_files": omitted_files,
        "included_source_chars": sum(item["included_chars"] for item in scope_files),
        "max_source_chars": budget,
        "truncated": bool(omitted_files)
        or any(
            item["prompt_truncated"] or item["input_truncated"]
            for item in scope_files
        ),
    }


class FlowContractError(ValueError):
    """Safe path/type diagnostic for an invalid provider flow payload."""

    def __init__(self, path: str, expected: str, actual: Any, hint: str) -> None:
        self.path = path
        self.expected = expected
        self.actual = _flow_shape(actual)
        self.hint = hint
        super().__init__(
            f"{path}: expected {expected}, got {self.actual}. {hint}"
        )


def _flow_shape(value: Any) -> str:
    if isinstance(value, str):
        return f"str(len={len(value)})"
    if isinstance(value, (Mapping, list, tuple, set)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def _flow_fail(path: str, expected: str, actual: Any, hint: str) -> None:
    raise FlowContractError(path, expected, actual, hint)


def _flow_mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _flow_fail(path, "object", value, "Emit the documented JSON object.")
    return value


def _flow_exact_keys(
    value: Mapping[str, Any],
    *,
    path: str,
    allowed: frozenset[str],
    required: frozenset[str] | None = None,
) -> None:
    required = allowed if required is None else required
    missing = required - set(value)
    if missing:
        # The key comes from the static schema, never from provider content.
        key = sorted(missing)[0]
        _flow_fail(
            f"{path}.{key}" if path != "$" else f"$.{key}",
            "required field",
            None,
            "Emit the complete documented object shape.",
        )
    unknown = set(value) - allowed
    if unknown:
        _flow_fail(
            path,
            "only documented fields",
            {"unknown_field_count": len(unknown)},
            "Remove unsupported fields.",
        )


def _flow_list(value: Any, *, path: str, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        _flow_fail(path, f"array with at most {maximum} items", value, "Emit a JSON array.")
    if len(value) > maximum:
        _flow_fail(
            path,
            f"array with at most {maximum} items",
            value,
            "Return a smaller complete result; do not rely on truncation.",
        )
    return value


def _flow_text(value: Any, *, path: str, maximum: int) -> str:
    if not isinstance(value, str):
        _flow_fail(path, f"non-empty string up to {maximum} chars", value, "Emit a JSON string.")
    normalized = value.strip()
    if "\x00" in normalized:
        _flow_fail(
            path,
            "NUL-free UTF-8 text",
            value,
            "Remove U+0000; PostgreSQL text and JSONB cannot store it.",
        )
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _flow_fail(
            path,
            "strictly UTF-8-encodable text",
            value,
            "Remove unpaired UTF-16 surrogate code points.",
        )
    if not normalized or len(normalized) > maximum:
        _flow_fail(
            path,
            f"non-empty string up to {maximum} chars",
            value,
            "State one concise source-derived value.",
        )
    return normalized


def _flow_enum(
    value: Any,
    *,
    path: str,
    allowed: frozenset[str],
    upper: bool = False,
) -> str:
    normalized = _flow_text(value, path=path, maximum=64)
    normalized = normalized.upper() if upper else normalized.lower()
    if normalized not in allowed:
        _flow_fail(path, "supported enum value", value, "Use one value from the documented enum.")
    return normalized


def _flow_scalar(value: Any, *, path: str) -> str | int | bool:
    if isinstance(value, str):
        return _flow_text(value, path=path, maximum=300)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and -(2**63) <= value <= 2**63 - 1:
        return value
    _flow_fail(
        path,
        "bounded string, boolean, or signed 64-bit integer",
        value,
        "Encode fractional values as bounded strings to preserve exact JSONB provenance.",
    )


def _flow_scalar_values(value: Any, *, path: str) -> list[str | int | bool]:
    values = _flow_list(value, path=path, maximum=MAX_VALUES)
    result: list[str | int | bool] = []
    seen: set[tuple[type, str | int | bool]] = set()
    for index, raw in enumerate(values):
        scalar = _flow_scalar(raw, path=f"{path}[{index}]")
        marker = (type(scalar), scalar)
        if marker in seen:
            _flow_fail(
                f"{path}[{index}]",
                "unique scalar value",
                raw,
                "Remove the duplicate value.",
            )
        seen.add(marker)
        result.append(scalar)
    return result


def _flow_bool(value: Any, *, path: str) -> bool:
    if not isinstance(value, bool):
        _flow_fail(path, "boolean", value, "Emit true or false.")
    return value


def _flow_count(value: Any, *, path: str, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= maximum
    ):
        _flow_fail(
            path,
            f"integer in 0..{maximum}",
            value,
            "Emit the deterministic bounded character count.",
        )
    return value


def _normalise_field(raw: Any, *, path: str) -> dict[str, Any]:
    item = _flow_mapping(raw, path=path)
    _flow_exact_keys(item, path=path, allowed=_FIELD_KEYS)
    return {
        "name": _flow_text(item["name"], path=f"{path}.name", maximum=300),
        "type": _flow_text(item["type"], path=f"{path}.type", maximum=100),
        "values": _flow_scalar_values(item["values"], path=f"{path}.values"),
        "meaning": _flow_text(item["meaning"], path=f"{path}.meaning", maximum=1_000),
    }


def _normalise_signal(raw: Any, *, path: str) -> dict[str, Any]:
    item = _flow_mapping(raw, path=path)
    _flow_exact_keys(item, path=path, allowed=_SIGNAL_KEYS)
    raw_fields = _flow_list(item["fields"], path=f"{path}.fields", maximum=MAX_FIELDS)
    fields: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, raw_field in enumerate(raw_fields):
        field = _normalise_field(raw_field, path=f"{path}.fields[{index}]")
        marker = field["name"].casefold()
        if marker in seen_names:
            _flow_fail(
                f"{path}.fields[{index}].name",
                "unique field name",
                field["name"],
                "Merge duplicate field descriptions.",
            )
        seen_names.add(marker)
        fields.append(field)
    return {
        "kind": _flow_enum(item["kind"], path=f"{path}.kind", allowed=_SIGNAL_KINDS),
        "name": _flow_text(item["name"], path=f"{path}.name", maximum=500),
        "fields": fields,
    }


def _normalise_steps(value: Any) -> list[dict[str, Any]]:
    raw_steps = _flow_list(value, path="$.steps", maximum=MAX_STEPS)
    if not raw_steps:
        _flow_fail("$.steps", "non-empty step array", raw_steps, "Trace at least one source-grounded step.")
    steps: list[dict[str, Any]] = []
    seen_orders: set[int] = set()
    for index, raw in enumerate(raw_steps):
        path = f"$.steps[{index}]"
        item = _flow_mapping(raw, path=path)
        _flow_exact_keys(item, path=path, allowed=_STEP_KEYS)
        order = item["order"]
        if not isinstance(order, int) or isinstance(order, bool) or not 1 <= order <= MAX_STEPS:
            _flow_fail(f"{path}.order", f"integer in 1..{MAX_STEPS}", order, "Use the real flow sequence number.")
        if order in seen_orders:
            _flow_fail(f"{path}.order", "unique step order", order, "Give each step one distinct order.")
        seen_orders.add(order)
        steps.append(
            {
                "order": order,
                "tier": _flow_enum(item["tier"], path=f"{path}.tier", allowed=_TIERS),
                "component": _flow_text(item["component"], path=f"{path}.component", maximum=500),
                "action": _flow_text(item["action"], path=f"{path}.action", maximum=1_000),
                "signal": _normalise_signal(item["signal"], path=f"{path}.signal"),
            }
        )
    if seen_orders != set(range(1, len(steps) + 1)):
        _flow_fail(
            "$.steps[].order",
            "contiguous orders 1..number_of_steps",
            list(seen_orders),
            "Remove gaps instead of letting consumers renumber semantic metadata.",
        )
    return sorted(steps, key=lambda step: step["order"])


def _normalise_flags(value: Any) -> list[dict[str, Any]]:
    raw_flags = _flow_list(value, path="$.flags", maximum=MAX_FLAGS)
    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_flags):
        path = f"$.flags[{index}]"
        item = _flow_mapping(raw, path=path)
        _flow_exact_keys(item, path=path, allowed=_FLAG_KEYS)
        name = _flow_text(item["name"], path=f"{path}.name", maximum=300)
        marker = name.casefold()
        if marker in seen_names:
            _flow_fail(f"{path}.name", "unique flag name", name, "Merge duplicate flag descriptions.")
        seen_names.add(marker)
        raw_values = _flow_list(item["values"], path=f"{path}.values", maximum=MAX_VALUES)
        values: list[dict[str, Any]] = []
        seen_values: set[tuple[type, str | int | bool]] = set()
        for value_index, raw_value in enumerate(raw_values):
            value_path = f"{path}.values[{value_index}]"
            value_item = _flow_mapping(raw_value, path=value_path)
            _flow_exact_keys(value_item, path=value_path, allowed=_FLAG_VALUE_KEYS)
            flag_value = _flow_scalar(value_item["value"], path=f"{value_path}.value")
            value_marker = (type(flag_value), flag_value)
            if value_marker in seen_values:
                _flow_fail(f"{value_path}.value", "unique flag value", flag_value, "Remove the duplicate value.")
            seen_values.add(value_marker)
            values.append(
                {
                    "value": flag_value,
                    "meaning": _flow_text(value_item["meaning"], path=f"{value_path}.meaning", maximum=1_000),
                }
            )
        result.append(
            {
                "name": name,
                "type": _flow_text(item["type"], path=f"{path}.type", maximum=100),
                "values": values,
                "set_at": _flow_text(item["set_at"], path=f"{path}.set_at", maximum=500),
                "used_at": _flow_text(item["used_at"], path=f"{path}.used_at", maximum=500),
            }
        )
    return result


def _normalise_data_touched(value: Any) -> list[dict[str, Any]]:
    raw_items = _flow_list(value, path="$.data_touched", maximum=MAX_DATA_TOUCHED)
    result: list[dict[str, Any]] = []
    seen_items: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_items):
        path = f"$.data_touched[{index}]"
        item = _flow_mapping(raw, path=path)
        _flow_exact_keys(item, path=path, allowed=_DATA_KEYS)
        entity = _flow_text(item["entity"], path=f"{path}.entity", maximum=300)
        operation = _flow_enum(
            item["op"], path=f"{path}.op", allowed=_DATA_OPERATIONS, upper=True
        )
        marker = (entity.casefold(), operation)
        if marker in seen_items:
            _flow_fail(path, "unique entity/operation pair", item, "Merge duplicate data access rows.")
        seen_items.add(marker)
        raw_columns = _flow_list(item["columns"], path=f"{path}.columns", maximum=MAX_COLUMNS)
        columns: list[str] = []
        seen_columns: set[str] = set()
        for column_index, raw_column in enumerate(raw_columns):
            column = _flow_text(raw_column, path=f"{path}.columns[{column_index}]", maximum=300)
            column_marker = column.casefold()
            if column_marker in seen_columns:
                _flow_fail(
                    f"{path}.columns[{column_index}]",
                    "unique column",
                    raw_column,
                    "Remove the duplicate column.",
                )
            seen_columns.add(column_marker)
            columns.append(column)
        result.append({"entity": entity, "op": operation, "columns": columns})
    return result


def normalize_flow_payload(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Return the sole pure, strict, bounded v1 flow representation."""

    root = _flow_mapping(parsed, path="$")
    required = _FLOW_KEYS - {"contract"}
    _flow_exact_keys(root, path="$", allowed=_FLOW_KEYS, required=required)
    if "contract" in root and root["contract"] != FLOW_RESULT_CONTRACT:
        _flow_fail(
            "$.contract",
            FLOW_RESULT_CONTRACT,
            root["contract"],
            "Do not change the persisted flow contract version.",
        )
    raw_questions = _flow_list(
        root["open_questions"], path="$.open_questions", maximum=MAX_OPEN_QUESTIONS
    )
    questions: list[str] = []
    seen_questions: set[str] = set()
    for index, raw in enumerate(raw_questions):
        question = _flow_text(raw, path=f"$.open_questions[{index}]", maximum=1_000)
        marker = " ".join(question.casefold().split())
        if marker in seen_questions:
            _flow_fail(
                f"$.open_questions[{index}]",
                "unique question",
                raw,
                "Remove the duplicate question.",
            )
        seen_questions.add(marker)
        questions.append(question)
    return {
        "contract": FLOW_RESULT_CONTRACT,
        "summary": _flow_text(root["summary"], path="$.summary", maximum=MAX_SUMMARY_CHARS),
        "detailed": _flow_text(root["detailed"], path="$.detailed", maximum=MAX_DETAILED_CHARS),
        "steps": _normalise_steps(root["steps"]),
        "flags": _normalise_flags(root["flags"]),
        "data_touched": _normalise_data_touched(root["data_touched"]),
        "open_questions": questions,
    }


@dataclass(frozen=True)
class FlowSummaryContent:
    """One canonical L4 persistence/consumer view.

    ``flow`` is model-owned, bounded hypothesis content. ``source_snapshot``
    is deterministic provenance owned by Mnemos. ``sections`` is the sole
    serialized representation stored in the legacy ``Summary.claims`` JSONB
    slot; consumers must not reinterpret those records as grounded claims.
    """

    flow: dict[str, Any]
    source_snapshot: dict[str, Any]
    sections: list[dict[str, Any]]


def _normalize_source_window(value: Any, *, path: str) -> dict[str, Any]:
    item = _flow_mapping(value, path=path)
    _flow_exact_keys(item, path=path, allowed=_SOURCE_WINDOW_KEYS)
    provided_chars = _flow_count(
        item["provided_chars"],
        path=f"{path}.provided_chars",
        maximum=MAX_FLOW_SOURCE_CHARS,
    )
    included_chars = _flow_count(
        item["included_chars"],
        path=f"{path}.included_chars",
        maximum=MAX_FLOW_SOURCE_CHARS,
    )
    if included_chars > provided_chars:
        _flow_fail(
            f"{path}.included_chars",
            "integer no greater than provided_chars",
            included_chars,
            "Record only the source characters actually included in the prompt.",
        )
    return {
        "label": _flow_text(item["label"], path=f"{path}.label", maximum=4_096),
        "tier": _flow_enum(item["tier"], path=f"{path}.tier", allowed=_TIERS),
        "language": _flow_text(
            item["language"], path=f"{path}.language", maximum=100
        ).lower(),
        "provided_chars": provided_chars,
        "included_chars": included_chars,
        "prompt_truncated": _flow_bool(
            item["prompt_truncated"], path=f"{path}.prompt_truncated"
        ),
        "input_truncated": _flow_bool(
            item["input_truncated"], path=f"{path}.input_truncated"
        ),
    }


def _normalize_source_snapshot(value: Any) -> dict[str, Any]:
    item = _flow_mapping(value, path="$.source_snapshot")
    _flow_exact_keys(
        item,
        path="$.source_snapshot",
        allowed=_SOURCE_SNAPSHOT_KEYS,
    )

    raw_run_id = _flow_text(
        item["analysis_run_id"],
        path="$.source_snapshot.analysis_run_id",
        maximum=36,
    )
    try:
        analysis_run_id = str(uuid.UUID(raw_run_id))
    except ValueError as exc:
        raise FlowContractError(
            "$.source_snapshot.analysis_run_id",
            "UUID string",
            raw_run_id,
            "Persist the completed analysis run identifier.",
        ) from exc

    revision = _flow_text(
        item["revision"],
        path="$.source_snapshot.revision",
        maximum=64,
    ).lower()
    if len(revision) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in revision
    ):
        _flow_fail(
            "$.source_snapshot.revision",
            "full 40- or 64-character hexadecimal Git object id",
            revision,
            "Persist the immutable completed snapshot revision.",
        )

    raw_files = _flow_list(
        item["files"],
        path="$.source_snapshot.files",
        maximum=MAX_FLOW_SOURCE_FILES,
    )
    if not raw_files:
        _flow_fail(
            "$.source_snapshot.files",
            "non-empty included-file array",
            raw_files,
            "A persisted flow must identify at least one analyzed source window.",
        )
    files: list[str] = []
    seen_files: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        label = _flow_text(
            raw_file,
            path=f"$.source_snapshot.files[{index}]",
            maximum=4_096,
        )
        if label in seen_files:
            _flow_fail(
                f"$.source_snapshot.files[{index}]",
                "unique source label",
                label,
                "Remove the duplicate included file.",
            )
        seen_files.add(label)
        files.append(label)

    raw_windows = _flow_list(
        item["file_windows"],
        path="$.source_snapshot.file_windows",
        maximum=MAX_FLOW_SOURCE_FILES,
    )
    windows = [
        _normalize_source_window(
            raw_window,
            path=f"$.source_snapshot.file_windows[{index}]",
        )
        for index, raw_window in enumerate(raw_windows)
    ]
    window_labels = [window["label"] for window in windows]
    if window_labels != files:
        _flow_fail(
            "$.source_snapshot.file_windows",
            "one ordered window for every included file",
            window_labels,
            "Keep files and file_windows labels identical and in prompt order.",
        )

    raw_omitted = _flow_list(
        item["omitted_files"],
        path="$.source_snapshot.omitted_files",
        maximum=MAX_FLOW_SOURCE_FILES,
    )
    omitted_files: list[dict[str, str]] = []
    seen_omitted: set[str] = set()
    for index, raw_omitted_file in enumerate(raw_omitted):
        path = f"$.source_snapshot.omitted_files[{index}]"
        omitted = _flow_mapping(raw_omitted_file, path=path)
        _flow_exact_keys(omitted, path=path, allowed=_OMITTED_SOURCE_KEYS)
        label = _flow_text(omitted["label"], path=f"{path}.label", maximum=4_096)
        reason = _flow_enum(
            omitted["reason"],
            path=f"{path}.reason",
            allowed=_OMITTED_SOURCE_REASONS,
        )
        if label in seen_files or label in seen_omitted:
            _flow_fail(
                f"{path}.label",
                "unique omitted label not present in included files",
                label,
                "Classify each source label exactly once.",
            )
        seen_omitted.add(label)
        omitted_files.append({"label": label, "reason": reason})

    return {
        "analysis_run_id": analysis_run_id,
        "revision": revision,
        "files": files,
        "file_windows": windows,
        "omitted_files": omitted_files,
    }


def _flow_summary_sections(
    flow: Mapping[str, Any], source_snapshot: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Serialize already-canonical data without a second compatibility pass."""

    return [
        {
            "contract": FLOW_RESULT_CONTRACT,
            "section": "source_snapshot",
            "data": dict(source_snapshot),
        },
        {
            "contract": FLOW_RESULT_CONTRACT,
            "section": "steps",
            "data": flow["steps"],
        },
        {
            "contract": FLOW_RESULT_CONTRACT,
            "section": "flags",
            "data": flow["flags"],
        },
        {
            "contract": FLOW_RESULT_CONTRACT,
            "section": "data_touched",
            "data": flow["data_touched"],
        },
    ]


def build_flow_summary_content(
    flow: Mapping[str, Any],
    *,
    analysis_run_id: uuid.UUID,
    revision: str,
    source_scope: Mapping[str, Any],
) -> FlowSummaryContent:
    """Build persistence content from a trusted canonical provider result.

    The model payload was normalized exactly once in
    :func:`analyze_flow_via_agent_sdk`.  This function only validates and
    normalizes deterministic source provenance, then serializes the canonical
    fields; it deliberately does not run the model compatibility normalizer a
    second time.
    """

    if flow.get("contract") != FLOW_RESULT_CONTRACT or set(flow) != _FLOW_KEYS:
        _flow_fail(
            "$.flow",
            f"canonical {FLOW_RESULT_CONTRACT} object",
            flow,
            "Pass the provider-boundary result without reconstructing it.",
        )
    raw_files = source_scope.get("files")
    raw_omitted = source_scope.get("omitted_files")
    source_snapshot = _normalize_source_snapshot(
        {
            "analysis_run_id": str(analysis_run_id),
            "revision": revision,
            "files": [
                window.get("label")
                for window in raw_files
                if isinstance(window, Mapping)
            ]
            if isinstance(raw_files, list)
            else raw_files,
            "file_windows": raw_files,
            "omitted_files": raw_omitted,
        }
    )
    sections = _flow_summary_sections(flow, source_snapshot)
    return FlowSummaryContent(
        flow=dict(flow),
        source_snapshot=source_snapshot,
        sections=sections,
    )


def normalize_flow_summary_content(
    value: Any,
    *,
    summary: Any,
    detailed: Any,
    open_questions: Any,
) -> FlowSummaryContent:
    """Normalize one persisted L4 section dialect into its canonical view.

    This is the single compatibility boundary for database consumers.  It
    accepts only the documented v1 section records, rejects duplicate/mixed
    contracts, reconstructs the complete flow, and returns fixed section
    order.  Generic ``{claim, evidence}`` rows are intentionally not aliases.
    """

    raw_sections = _flow_list(
        value,
        path="$.sections",
        maximum=len(FLOW_SUMMARY_SECTION_ORDER),
    )
    if len(raw_sections) != len(FLOW_SUMMARY_SECTION_ORDER):
        _flow_fail(
            "$.sections",
            f"exactly {len(FLOW_SUMMARY_SECTION_ORDER)} flow sections",
            raw_sections,
            "Persist the complete source_snapshot/steps/flags/data_touched set.",
        )

    by_name: dict[str, Any] = {}
    for index, raw_section in enumerate(raw_sections):
        path = f"$.sections[{index}]"
        section = _flow_mapping(raw_section, path=path)
        _flow_exact_keys(
            section,
            path=path,
            allowed=_FLOW_SUMMARY_SECTION_KEYS,
        )
        if section["contract"] != FLOW_RESULT_CONTRACT:
            _flow_fail(
                f"{path}.contract",
                FLOW_RESULT_CONTRACT,
                section["contract"],
                "Do not mix summary content contracts.",
            )
        section_name = section["section"]
        if section_name not in _FLOW_SUMMARY_SECTION_NAMES:
            _flow_fail(
                f"{path}.section",
                "source_snapshot|steps|flags|data_touched",
                section_name,
                "Use one documented L4 section discriminator.",
            )
        if section_name in by_name:
            _flow_fail(
                f"{path}.section",
                "unique flow section",
                section_name,
                "Merge duplicate sections before persistence.",
            )
        by_name[section_name] = section["data"]

    missing = _FLOW_SUMMARY_SECTION_NAMES - set(by_name)
    if missing:
        section_name = sorted(missing)[0]
        _flow_fail(
            f"$.sections.{section_name}",
            "required flow section",
            None,
            "Persist the complete L4 result.",
        )

    flow = normalize_flow_payload(
        {
            "contract": FLOW_RESULT_CONTRACT,
            "summary": summary,
            "detailed": detailed,
            "steps": by_name["steps"],
            "flags": by_name["flags"],
            "data_touched": by_name["data_touched"],
            "open_questions": open_questions,
        }
    )
    source_snapshot = _normalize_source_snapshot(by_name["source_snapshot"])
    sections = _flow_summary_sections(flow, source_snapshot)
    return FlowSummaryContent(
        flow=flow,
        source_snapshot=source_snapshot,
        sections=sections,
    )


def _normalise(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Compatibility name for the strict canonical normalizer."""

    return normalize_flow_payload(parsed)


class _FlowOutputTooLarge(Exception):
    """Internal control signal; provider text is intentionally not retained."""


@dataclass(slots=True)
class FlowAttemptState:
    """Privacy-safe durable-attempt facts returned to the HTTP owner."""

    attempt_id: uuid.UUID | None = None
    usage: LLMUsageV1 | None = None
    resolved_model: str | None = None
    failure_code: str | None = None
    result_status: ResultStatus | None = None
    candidate_binding_fingerprint: str | None = None


async def analyze_flow_via_agent_sdk(
    *,
    entry: str,
    sources: list[dict[str, Any]],
    model: str = DEFAULT_FLOW_MODEL,
    timeout_s: int = 300,
    max_total_chars: int = 28_000,
    run_budget: LLMRunBudget | None = None,
    before_provider_call: FlowBeforeProviderCall | None = None,
    record_physical_call: FlowPhysicalCallRecorder | None = None,
    attempt_state: FlowAttemptState | None = None,
    operation_id: uuid.UUID,
    semantic_binding_identity: str,
) -> dict[str, Any] | None:
    """Trace one cross-tier process via an isolated Agent SDK query.

    The outer adapter envelope deliberately separates model-owned canonical
    flow data from deterministic source provenance::

        {"flow": canonical_v1, "source_scope": deterministic_scope}

    Provider/preflight failures return ``None``.  Budget and durable-ledger
    callbacks are allowed to raise so a caller never reports a successful
    analysis after its accounting transaction failed.
    """
    if not isinstance(operation_id, uuid.UUID):
        raise LLMLifecycleError(
            "flow analysis requires a stable consumer operation identity"
        )
    if (
        not isinstance(semantic_binding_identity, str)
        or re.fullmatch(r"[0-9a-f]{64}", semantic_binding_identity) is None
    ):
        raise LLMLifecycleError(
            "flow analysis requires a revision-bound semantic identity"
        )
    if not is_agent_sdk_available() or not sources:
        return None
    if model != AGENT_SDK_BUDGETED_MODEL:
        log.warning("agent_flow: unsupported budget model refused")
        return None
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError:
        return None

    packed_sources, source_scope = _pack_flow_sources(
        sources, max_total_chars=max_total_chars
    )
    if not packed_sources:
        log.warning("agent_flow: no source remained within the prompt budget")
        return None
    if timeout_s <= 0:
        return None
    prompt = _build_flow_prompt(entry, packed_sources)
    opts = ClaudeAgentOptions(
        **agent_sdk_isolation_options(),
        disallowed_tools=["Bash", "Edit", "Write", "Read", "Task"],
        system_prompt=_FLOW_SYSTEM,
        max_turns=1,
        permission_mode="dontAsk",
        cwd=os.environ.get("MNEMOS_AGENT_SDK_CWD", "/tmp"),
        model=model if model and "claude" in model else None,
    )

    # Everything above is local preflight.  Refuse before mutating even the
    # in-memory run budget when no durable lifecycle owner is installed.
    callbacks = require_attempt_callbacks("Claude Agent SDK flow analysis")
    if callbacks.finish_candidate is None or (
        callbacks.replay_attempt is None
        and callbacks.replay_candidate is None
    ):
        raise LLMLifecycleError(
            "Claude Agent SDK flow analysis requires semantic candidate capabilities"
        )
    if attempt_state is not None:
        attempt_state.attempt_id = None
        attempt_state.usage = None
        attempt_state.resolved_model = None
        attempt_state.failure_code = None
        attempt_state.result_status = None
        attempt_state.candidate_binding_fingerprint = None

    # The durable start callback is the sole owner of a new request's budget
    # reservation.  It first checks for an exact prior attempt, allowing a
    # validated local replay even when today's fresh budget is exhausted.
    active_budget = run_budget or LLMRunBudget.from_env()
    estimated_input_tokens = AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS
    provider_timeout = float(timeout_s)
    deadline_limited = False
    ticket: AttemptTicket | None = None
    auth_contract = AgentSDKAuthContract()

    input_fingerprint = fingerprint_input(_FLOW_SYSTEM, prompt)
    operation_namespace = operation_id
    bound_operation_id = uuid.uuid5(
        operation_namespace,
        "\x00".join(("anthropic", model, input_fingerprint)),
    )
    binding_identity = semantic_binding_identity
    candidate_binding_fingerprint = flow_candidate_binding_fingerprint(
        binding_identity,
        source_scope,
    )
    if attempt_state is not None:
        attempt_state.candidate_binding_fingerprint = (
            candidate_binding_fingerprint
        )
    metadata = AttemptStartMetadata(
        operation_id=bound_operation_id,
        attempt_no=1,
        attempt_kind=AttemptKind.PRIMARY,
        provider="anthropic",
        provider_mode="unknown",
        requested_model=model,
        input_fingerprint=input_fingerprint,
        estimated_input_tokens=estimated_input_tokens,
        input_estimate_method="model_context_ceiling_v1",
        requested_max_output_tokens=AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS,
        usage_source=UsageSource.AGENT_SDK,
        billing_route="claude_agent_sdk",
    )
    try:
        ticket = await callbacks.start(metadata)
    except LLMAttemptReplayBlocked:
        replay_request = SemanticCandidateReplayRequest(
            operation_id=bound_operation_id,
            attempt_no=1,
            input_fingerprint=input_fingerprint,
            contract_name=FLOW_RESULT_CONTRACT,
            binding_fingerprint=candidate_binding_fingerprint,
            normalizer=normalize_flow_payload,
        )
        if callbacks.replay_attempt is not None:
            replay = await callbacks.replay_attempt(replay_request)
            if replay.state == AttemptReplayState.STARTED:
                raise LLMAttemptReplayBlocked(
                    "flow operation is still in progress"
                )
            if replay.state == AttemptReplayState.ABSENT:
                raise LLMSemanticCandidateCorrupt(
                    "flow duplicate attempt disappeared"
                )
            if attempt_state is not None:
                attempt_state.attempt_id = replay.attempt_id
                attempt_state.resolved_model = replay.resolved_model
                attempt_state.result_status = replay.result_status
                attempt_state.failure_code = replay.failure_code
            if replay.state == AttemptReplayState.TERMINAL_WITHOUT_CANDIDATE:
                return None
        else:
            replayer = callbacks.replay_candidate
            if replayer is None:  # pragma: no cover - guarded above
                raise LLMLifecycleError("flow semantic replayer disappeared")
            replay = await replayer(replay_request)
        if attempt_state is not None:
            attempt_state.attempt_id = replay.attempt_id
            attempt_state.resolved_model = replay.resolved_model
            attempt_state.result_status = replay.result_status
            attempt_state.failure_code = (
                "flow_candidate_rejected"
                if replay.candidate_status == "rejected"
                else None
            )
        if replay.candidate_status not in {"candidate", "accepted"}:
            return None
        if replay.result_status not in {
            ResultStatus.PENDING,
            ResultStatus.ACCEPTED,
        }:
            raise LLMSemanticCandidateCorrupt(
                "flow replay candidate classification is inconsistent"
            )
        return {
            "flow": dict(replay.payload),
            "source_scope": source_scope,
        }
    if attempt_state is not None:
        attempt_state.attempt_id = ticket.attempt_id
    if ticket.remaining_seconds <= provider_timeout:
        deadline_limited = True
    provider_timeout = min(provider_timeout, ticket.remaining_seconds)
    provider_deadline = monotonic() + max(0.0, provider_timeout)

    async def _finish(
        status: str,
        fallback_reason: str | None,
        *,
        attempt_status: AttemptStatus,
        result_status: ResultStatus,
        usage: LLMUsageV1,
        resolved_model: str | None = None,
        finish_reason: str | None = None,
        candidate: SemanticCandidate | None = None,
    ) -> None:
        if attempt_state is not None:
            attempt_state.usage = usage
            attempt_state.resolved_model = resolved_model
            attempt_state.failure_code = fallback_reason
            attempt_state.result_status = result_status
        outcome = AttemptOutcome(
            attempt_status=attempt_status,
            result_status=result_status,
            usage=usage,
            resolved_model=resolved_model,
            failure_code=fallback_reason,
            finish_reason=finish_reason,
            provider_mode=auth_contract.provider_mode,
        )
        if candidate is not None:
            if callbacks.finish_candidate is None:  # pragma: no cover - guarded above
                raise LLMLifecycleError(
                    "flow semantic candidate finalizer is unavailable"
                )
            await callbacks.finish_candidate(ticket, outcome, candidate)
        else:
            await callbacks.finish(ticket, outcome)
        # Optional observer only; it cannot authorize dispatch and runs after
        # the durable finalizer succeeds. Retained for adapter telemetry/tests.
        if record_physical_call is not None:
            await record_physical_call(
                status,
                fallback_reason,
                resolved_model or model,
            )

    # Legacy observers may still perform a local policy/readiness check, but
    # they run only for a genuinely new ticket.  A replay never invokes them.
    # Failure is ledgered as a pre-dispatch terminal outcome so the committed
    # STARTED row cannot be stranded or mistaken for provider spend.
    if before_provider_call is not None:
        try:
            await before_provider_call()
        except asyncio.CancelledError:
            await _finish(
                "cancelled",
                FLOW_FALLBACK_CANCELLED,
                attempt_status=AttemptStatus.CANCELLED,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=unavailable_usage(
                    lost_after_dispatch=False,
                    source=UsageSource.AGENT_SDK,
                ),
            )
            raise
        except Exception:
            await _finish(
                "failed",
                FLOW_FALLBACK_PREDISPATCH,
                attempt_status=AttemptStatus.FAILED,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=unavailable_usage(
                    lost_after_dispatch=False,
                    source=UsageSource.AGENT_SDK,
                ),
            )
            raise

    # The optional readiness observer is pre-dispatch work and must consume the
    # same absolute ticket window; it cannot extend a durable deadline merely
    # because the timeout was calculated before awaiting it.
    provider_timeout = provider_deadline - monotonic()

    if provider_timeout <= 0:
        active_budget.stop(FLOW_FALLBACK_RUN_DEADLINE)
        await _finish(
            "timeout",
            FLOW_FALLBACK_RUN_DEADLINE,
            attempt_status=AttemptStatus.TIMEOUT,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=False,
                source=UsageSource.AGENT_SDK,
            ),
        )
        return None

    collected: list[str] = []
    result_message: Any | None = None
    assistant_contract = AgentSDKAssistantContract(expected_model=model)
    if callbacks is not None and callbacks.mark_provider_dispatch is not None:
        callbacks.mark_provider_dispatch()
    try:
        async def _drain() -> Any | None:
            total_bytes = 0
            async for msg in query(prompt=prompt, options=opts):
                auth_contract.observe(msg)
                if isinstance(msg, AssistantMessage):
                    if not assistant_contract.observe(msg):
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            separator_bytes = 1 if collected else 0
                            block_bytes = len(block.text.encode("utf-8"))
                            if (
                                total_bytes + separator_bytes + block_bytes
                                > MAX_AGENT_OUTPUT_BYTES
                            ):
                                raise _FlowOutputTooLarge
                            collected.append(block.text)
                            total_bytes += separator_bytes + block_bytes
                elif isinstance(msg, ResultMessage):
                    return msg
            return None

        result_message = await asyncio.wait_for(
            _drain(), timeout=provider_timeout
        )
    except asyncio.CancelledError:
        await _finish(
            "cancelled",
            FLOW_FALLBACK_CANCELLED,
            attempt_status=AttemptStatus.CANCELLED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.AGENT_SDK,
            ),
        )
        raise
    except _FlowOutputTooLarge:
        log.warning(
            "agent_flow: response exceeded the %d-byte output limit",
            MAX_AGENT_OUTPUT_BYTES,
        )
        await _finish(
            "rejected",
            FLOW_FALLBACK_OUTPUT_LIMIT,
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.OUTPUT_LIMIT_REJECTED,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.AGENT_SDK,
            ),
        )
        return None
    except asyncio.TimeoutError:
        fallback_reason = (
            FLOW_FALLBACK_RUN_DEADLINE
            if deadline_limited
            else FLOW_FALLBACK_PROVIDER_TIMEOUT
        )
        if deadline_limited:
            active_budget.stop(FLOW_FALLBACK_RUN_DEADLINE)
        log.warning("agent_flow: timed out within its bounded call window")
        await _finish(
            "timeout",
            fallback_reason,
            attempt_status=AttemptStatus.TIMEOUT,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.AGENT_SDK,
            ),
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_flow: provider failed: %s", exc.__class__.__name__)
        await _finish(
            "failed",
            FLOW_FALLBACK_PROVIDER_ERROR,
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.AGENT_SDK,
            ),
        )
        return None

    if result_message is None:
        await _finish(
            "rejected",
            FLOW_FALLBACK_PROVIDER_REJECTED,
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.AGENT_SDK,
            ),
            resolved_model=assistant_contract.resolved_model,
        )
        return None

    terminal = inspect_agent_sdk_result(result_message)
    try:
        usage = normalize_agent_sdk_usage(result_message)
    except LLMUsageContractError:
        await _finish(
            "rejected",
            FLOW_FALLBACK_USAGE_REJECTED,
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(source=UsageSource.AGENT_SDK),
            resolved_model=assistant_contract.resolved_model,
            finish_reason=terminal.finish_reason,
        )
        return None
    if terminal.disposition == AGENT_SDK_TERMINAL_OUTPUT_LIMIT:
        await _finish(
            "rejected",
            FLOW_FALLBACK_OUTPUT_LIMIT,
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.OUTPUT_LIMIT_REJECTED,
            usage=usage,
            resolved_model=assistant_contract.resolved_model,
            finish_reason=terminal.finish_reason,
        )
        return None
    if terminal.disposition != AGENT_SDK_TERMINAL_ACCEPTED:
        await _finish(
            "rejected",
            FLOW_FALLBACK_PROVIDER_REJECTED,
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=usage,
            resolved_model=assistant_contract.resolved_model,
            finish_reason=terminal.finish_reason,
        )
        return None
    if not assistant_contract.valid:
        await _finish(
            "rejected",
            FLOW_FALLBACK_PROVIDER_REJECTED,
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=usage,
            resolved_model=assistant_contract.resolved_model,
            finish_reason=terminal.finish_reason,
        )
        return None
    if not collected:
        await _finish(
            "rejected",
            FLOW_FALLBACK_EMPTY_RESPONSE,
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.EMPTY,
            usage=usage,
            resolved_model=assistant_contract.resolved_model,
            finish_reason=terminal.finish_reason,
        )
        return None
    parsed = _parse_json_response("\n".join(collected))
    if parsed is None:
        await _finish(
            "rejected",
            FLOW_FALLBACK_PARSE_REJECTED,
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.PARSE_REJECTED,
            usage=usage,
            resolved_model=assistant_contract.resolved_model,
            finish_reason=terminal.finish_reason,
        )
        return None
    try:
        normalized = normalize_flow_payload(parsed)
    except FlowContractError as exc:
        # Safe diagnostic contains only a static path and type/size shape.
        log.warning("agent_flow: contract rejected: %s", exc)
        await _finish(
            "rejected",
            FLOW_FALLBACK_CONTRACT_REJECTED,
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.SCHEMA_REJECTED,
            usage=usage,
            resolved_model=assistant_contract.resolved_model,
            finish_reason=terminal.finish_reason,
        )
        return None
    await _finish(
        "completed",
        None,
        attempt_status=AttemptStatus.COMPLETED,
        result_status=ResultStatus.PENDING,
        usage=usage,
        resolved_model=assistant_contract.resolved_model,
        finish_reason=terminal.finish_reason,
        candidate=SemanticCandidate(
            contract_name=FLOW_RESULT_CONTRACT,
            binding_fingerprint=candidate_binding_fingerprint,
            payload=normalized,
        ),
    )
    return {"flow": normalized, "source_scope": source_scope}
