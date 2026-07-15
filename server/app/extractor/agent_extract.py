"""PR-140 — Claude-Code-driven source extraction for languages that have
no deterministic ggoss analyzer.

Mnemos's design principle #4 is "delegate the conversation & coding loops
to Claude Code". Until now that delegation only covered L1~L3
*summarisation*, which reads nodes the deterministic analyzers already
extracted. So a project written in a language with no ggoss analyzer
(C++, Go, Rust, …) produced an *empty graph* and therefore zero
summaries and zero findings — the platform was effectively blind to it.
That is a fatal gap for a tool that bills itself as polyglot.

This module closes it: for an uncovered language we hand each source
file to the operator's **Claude Code subscription** (the Agent SDK,
no API key required) and ask it to extract a structured symbol/edge
list. The result flows into the same graph ingest path as the
deterministic analyzers, so summaries, findings, MCP queries and the
dashboard all light up.

Honesty: LLM-derived structure is ``certainty="inferred"`` (spec §2
principle #3) — never ``verified``. The deterministic analyzers, when
present, remain the source of truth; this is the fallback that keeps
the platform from going blind.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.extractor.agent_sdk import _parse_json_response, is_agent_sdk_available

log = logging.getLogger(__name__)

# A file larger than this is not prefix-analysed: a partial prefix cannot
# establish authoritative producer coverage.  The orchestrator also uses the
# same constant for a bounded text read, so a giant/generated file cannot be
# loaded in full merely to discover that it is ineligible.
MAX_AGENT_CODE_CHARS = 16_000
MAX_AGENT_ITEMS = 500
MAX_AGENT_EDGES = 1_000
MAX_AGENT_COLUMNS = 200
MAX_AGENT_CALLS = 200
MAX_AGENT_SUMMARY_CHARS = 160

_CODE_TOP_LEVEL_KEYS = frozenset({"symbols", "edges", "data_access"})
_DB_TOP_LEVEL_KEYS = frozenset({"entities", "edges"})
_SYMBOL_KEYS = frozenset(
    {"id", "name", "qualified_name", "kind", "line", "signature", "calls", "summary"}
)
_ENTITY_KEYS = frozenset(
    {"id", "name", "kind", "columns", "is_sensitive", "summary"}
)
_COLUMN_KEYS = frozenset({"name", "type", "pk", "nullable", "sensitive"})
_EDGE_KEYS = frozenset({"source_id", "target_id", "kind"})
_DATA_ACCESS_KEYS = frozenset({"symbol_id", "table", "access"})
_SYMBOL_KINDS = frozenset(
    {
        "function", "method", "class", "interface", "struct", "enum",
        "property", "module", "namespace", "constructor",
    }
)
_CODE_EDGE_KINDS = frozenset(
    {"CALLS", "CONTAINS", "IMPLEMENTS", "EXTENDS", "REFERENCES"}
)


class AgentExtractContractError(ValueError):
    """Safe diagnostic for the provider-to-agent-extraction boundary."""

    def __init__(self, path: str, expected: str, actual: Any, hint: str) -> None:
        self.path = path
        self.expected = expected
        self.actual = _contract_shape(actual)
        self.hint = hint
        super().__init__(
            f"{path}: expected {expected}, got {self.actual}. {hint}"
        )


def _contract_shape(value: Any) -> str:
    if isinstance(value, str):
        return f"str(len={len(value)})"
    if isinstance(value, (Mapping, list, tuple, set)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def _fail(path: str, expected: str, actual: Any, hint: str) -> None:
    raise AgentExtractContractError(path, expected, actual, hint)


def _exact_keys(
    value: Mapping[str, Any],
    *,
    path: str,
    allowed: frozenset[str],
    required: frozenset[str],
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    if missing:
        key = missing[0]
        _fail(
            f"{path}.{key}" if path != "$" else f"$.{key}",
            "required field",
            None,
            "Emit the complete documented object shape.",
        )
    unknown = keys - allowed
    if unknown:
        # Unknown keys are model content and may themselves contain sensitive
        # text.  Report only their count, not their raw names.
        _fail(
            path,
            "only documented fields",
            {"unknown_field_count": len(unknown)},
            "Remove unsupported fields.",
        )


def _text_contract(
    value: Any,
    *,
    path: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        _fail(path, f"string up to {maximum} chars", value, "Emit a JSON string.")
    normalized = value.strip()
    if not normalized and not allow_empty:
        _fail(path, "non-empty string", value, "Emit a concrete source-derived value.")
    if len(normalized) > maximum:
        _fail(
            path,
            f"string up to {maximum} chars",
            value,
            "Condense the value rather than relying on truncation.",
        )
    return normalized


def _list_contract(value: Any, *, path: str, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, f"array with at most {maximum} items", value, "Emit a JSON array.")
    if len(value) > maximum:
        _fail(
            path,
            f"array with at most {maximum} items",
            value,
            "Return only source-defined, high-confidence items.",
        )
    return value


def _mapping_contract(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "object", value, "Emit one JSON object per array item.")
    return value


def normalize_agent_extract_payload(
    payload: Mapping[str, Any], *, language: str
) -> dict[str, Any]:
    """Return the sole strict, bounded extraction representation.

    Code and DB extraction have distinct top-level envelopes.  No aliases are
    accepted, malformed members reject the whole file, and only an explicit
    set of empty required arrays represents a valid source file with no facts.
    The function is pure and idempotent.
    """

    root = _mapping_contract(payload, path="$")
    is_db = language in AGENT_DB_LANGUAGES
    top_keys = _DB_TOP_LEVEL_KEYS if is_db else _CODE_TOP_LEVEL_KEYS
    _exact_keys(root, path="$", allowed=top_keys, required=top_keys)

    item_key = "entities" if is_db else "symbols"
    raw_items = _list_contract(root[item_key], path=f"$.{item_key}", maximum=MAX_AGENT_ITEMS)
    raw_edges = _list_contract(root["edges"], path="$.edges", maximum=MAX_AGENT_EDGES)
    canonical_items: list[dict[str, Any]] = []
    model_ids: set[str] = set()

    if is_db:
        for index, raw in enumerate(raw_items):
            path = f"$.entities[{index}]"
            item = _mapping_contract(raw, path=path)
            _exact_keys(
                item,
                path=path,
                allowed=_ENTITY_KEYS,
                required=frozenset({"id", "name", "kind", "columns", "is_sensitive", "summary"}),
            )
            model_id = _text_contract(item["id"], path=f"{path}.id", maximum=512)
            if not model_id.startswith("data:") or not model_id.removeprefix("data:"):
                _fail(
                    f"{path}.id",
                    "data:<schema.table> identifier",
                    model_id,
                    "Use the documented DB entity ID form.",
                )
            if model_id in model_ids:
                _fail(
                    f"{path}.id",
                    "unique entity id",
                    model_id,
                    "Give every entity exactly one unambiguous ID.",
                )
            model_ids.add(model_id)
            kind = _text_contract(item["kind"], path=f"{path}.kind", maximum=40)
            if kind not in {"table", "view"}:
                _fail(f"{path}.kind", "table|view", kind, "Use a supported entity kind.")
            columns: list[dict[str, Any]] = []
            for column_index, raw_column in enumerate(
                _list_contract(
                    item["columns"], path=f"{path}.columns", maximum=MAX_AGENT_COLUMNS
                )
            ):
                column_path = f"{path}.columns[{column_index}]"
                column = _mapping_contract(raw_column, path=column_path)
                _exact_keys(
                    column,
                    path=column_path,
                    allowed=_COLUMN_KEYS,
                    required=_COLUMN_KEYS,
                )
                for boolean_key in ("pk", "nullable", "sensitive"):
                    if not isinstance(column[boolean_key], bool):
                        _fail(
                            f"{column_path}.{boolean_key}",
                            "boolean",
                            column[boolean_key],
                            "Emit JSON true or false, not a truthy string.",
                        )
                columns.append(
                    {
                        "name": _text_contract(
                            column["name"], path=f"{column_path}.name", maximum=200
                        ),
                        "type": _text_contract(
                            column["type"],
                            path=f"{column_path}.type",
                            maximum=200,
                            allow_empty=True,
                        ),
                        "pk": column["pk"],
                        "nullable": column["nullable"],
                        "sensitive": column["sensitive"],
                    }
                )
            if not isinstance(item["is_sensitive"], bool):
                _fail(
                    f"{path}.is_sensitive",
                    "boolean",
                    item["is_sensitive"],
                    "Emit JSON true or false, not a truthy string.",
                )
            canonical_items.append(
                {
                    "id": model_id,
                    "name": _text_contract(item["name"], path=f"{path}.name", maximum=300),
                    "kind": kind,
                    "columns": columns,
                    "is_sensitive": item["is_sensitive"],
                    "summary": _text_contract(
                        item["summary"],
                        path=f"{path}.summary",
                        maximum=MAX_AGENT_SUMMARY_CHARS,
                        allow_empty=True,
                    ),
                }
            )
    else:
        for index, raw in enumerate(raw_items):
            path = f"$.symbols[{index}]"
            item = _mapping_contract(raw, path=path)
            _exact_keys(
                item,
                path=path,
                allowed=_SYMBOL_KEYS,
                required=_SYMBOL_KEYS,
            )
            model_id = _text_contract(item["id"], path=f"{path}.id", maximum=512)
            if model_id in model_ids:
                _fail(
                    f"{path}.id",
                    "unique symbol id",
                    model_id,
                    "Give every symbol exactly one unambiguous ID.",
                )
            model_ids.add(model_id)
            kind = _text_contract(item["kind"], path=f"{path}.kind", maximum=40)
            if kind not in _SYMBOL_KINDS:
                _fail(
                    f"{path}.kind",
                    "supported symbol kind",
                    kind,
                    "Use a documented symbol kind.",
                )
            line = item["line"]
            if (
                not isinstance(line, int)
                or isinstance(line, bool)
                or not 1 <= line <= 10_000_000
            ):
                _fail(
                    f"{path}.line",
                    "integer in 1..10000000",
                    line,
                    "Copy a real source line number.",
                )
            calls: list[str] = []
            seen_calls: set[str] = set()
            for call_index, raw_call in enumerate(
                _list_contract(item["calls"], path=f"{path}.calls", maximum=MAX_AGENT_CALLS)
            ):
                call = _text_contract(
                    raw_call, path=f"{path}.calls[{call_index}]", maximum=300
                )
                if call in seen_calls:
                    _fail(
                        f"{path}.calls[{call_index}]",
                        "unique callee name",
                        call,
                        "Remove the duplicate call reference.",
                    )
                seen_calls.add(call)
                calls.append(call)
            canonical_items.append(
                {
                    "id": model_id,
                    "name": _text_contract(item["name"], path=f"{path}.name", maximum=300),
                    "qualified_name": _text_contract(
                        item["qualified_name"],
                        path=f"{path}.qualified_name",
                        maximum=300,
                        allow_empty=True,
                    ),
                    "kind": kind,
                    "line": line,
                    "signature": _text_contract(
                        item["signature"],
                        path=f"{path}.signature",
                        maximum=2_000,
                        allow_empty=True,
                    ),
                    "calls": calls,
                    "summary": _text_contract(
                        item["summary"],
                        path=f"{path}.summary",
                        maximum=MAX_AGENT_SUMMARY_CHARS,
                        allow_empty=True,
                    ),
                }
            )

    canonical_edges: list[dict[str, str]] = []
    for index, raw in enumerate(raw_edges):
        path = f"$.edges[{index}]"
        edge = _mapping_contract(raw, path=path)
        _exact_keys(edge, path=path, allowed=_EDGE_KEYS, required=_EDGE_KEYS)
        source_id = _text_contract(edge["source_id"], path=f"{path}.source_id", maximum=512)
        target_id = _text_contract(edge["target_id"], path=f"{path}.target_id", maximum=512)
        kind = _text_contract(edge["kind"], path=f"{path}.kind", maximum=40).upper()
        allowed_kinds = frozenset({"REFERENCES"}) if is_db else _CODE_EDGE_KINDS
        if kind not in allowed_kinds:
            _fail(f"{path}.kind", "supported edge kind", kind, "Use a documented edge kind.")
        for endpoint_key, endpoint in (("source_id", source_id), ("target_id", target_id)):
            if endpoint not in model_ids:
                _fail(
                    f"{path}.{endpoint_key}",
                    f"id declared in $.{item_key}",
                    endpoint,
                    "Only connect nodes declared in this response.",
                )
        canonical_edges.append(
            {"source_id": source_id, "target_id": target_id, "kind": kind}
        )

    if is_db:
        return {"entities": canonical_items, "edges": canonical_edges}

    raw_access = _list_contract(
        root["data_access"], path="$.data_access", maximum=MAX_AGENT_ITEMS
    )
    canonical_access: list[dict[str, str]] = []
    for index, raw in enumerate(raw_access):
        path = f"$.data_access[{index}]"
        access = _mapping_contract(raw, path=path)
        _exact_keys(
            access,
            path=path,
            allowed=_DATA_ACCESS_KEYS,
            required=_DATA_ACCESS_KEYS,
        )
        symbol_id = _text_contract(
            access["symbol_id"], path=f"{path}.symbol_id", maximum=512
        )
        if symbol_id not in model_ids:
            _fail(
                f"{path}.symbol_id",
                "id declared in $.symbols",
                symbol_id,
                "Attach data access only to a symbol in this response.",
            )
        operation = _text_contract(
            access["access"], path=f"{path}.access", maximum=16
        ).upper()
        if operation not in {"READS", "WRITES"}:
            _fail(
                f"{path}.access",
                "READS|WRITES",
                operation,
                "Use the observed access direction.",
            )
        canonical_access.append(
            {
                "symbol_id": symbol_id,
                "table": _text_contract(
                    access["table"], path=f"{path}.table", maximum=300
                ),
                "access": operation,
            }
        )
    return {
        "symbols": canonical_items,
        "edges": canonical_edges,
        "data_access": canonical_access,
    }

# Languages we know how to find on disk. A language absent here simply
# gets skipped by the agent-extraction stage (recorded, not crashed).
AGENT_LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "cpp": (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hxx", ".hh", ".h", ".c"),
    # Languages that also have a deterministic ggoss analyzer — listed here
    # so the Claude-Code fallback can still find their files when the
    # analyzer binary isn't installed (docker-free, PR-144).
    "python": (".py",),
    "csharp": (".cs",),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
    "rust": (".rs",),
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "ruby": (".rb",),
    "php": (".php",),
    "swift": (".swift",),
    "scala": (".scala",),
    # DB schema provided as SQL/DDL files (the "sql 로 제공" form). Any
    # dialect — Claude Code reads Postgres/MySQL/SQLite/T-SQL/PL-SQL alike,
    # so DB knowledge is no longer locked to the mssql/oracle live-schema
    # analyzers. Tables become DataEntity nodes; FKs become edges.
    "sql": (".sql", ".ddl"),
}

# Languages whose extraction yields DB DataEntity nodes (tables/columns)
# rather than code Symbol nodes. Drives the prompt + envelope shape.
AGENT_DB_LANGUAGES = frozenset({"sql"})

# Directories that never hold first-party source — skip to spend the
# (bounded) LLM budget on code that matters.
_SKIP_DIRS = {
    ".git", "node_modules", "build", "dist", "out", "bin", "obj",
    "third_party", "thirdparty", "external", "vendor", "deps",
    "__pycache__", ".vs", ".vscode", "cmake-build-debug",
}


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        )
    except OSError:
        return True


def _iter_safe_source_files(root: Path):
    """Yield regular files without following links outside the source root."""

    if _is_link_or_junction(root):
        return
    try:
        source_root = root.resolve(strict=True)
    except OSError:
        return
    for dirpath, dirnames, filenames in os.walk(source_root, followlinks=False):
        parent = Path(dirpath)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_DIRS
            and not name.startswith(".")
            and not _is_link_or_junction(parent / name)
        )
        for name in sorted(filenames):
            path = parent / name
            if _is_link_or_junction(path):
                continue
            try:
                if path.is_file():
                    yield path
            except OSError:
                continue

_EXTRACT_SYSTEM = (
    "You are a precise source-code structure extractor for a code-knowledge "
    "graph. You receive ONE source file and return ONLY a JSON object — no "
    "prose, no markdown fences. Extract the top-level and member symbols "
    "(functions, methods, classes, structs, namespaces, enums) actually "
    "DEFINED in this file, and the call/containment edges you can see with "
    "high confidence. Do not invent symbols that aren't in the file."
)

_DB_EXTRACT_SYSTEM = (
    "You are a precise database-schema extractor for a code-knowledge graph. "
    "You receive ONE SQL/DDL file (any dialect — Postgres, MySQL, SQLite, "
    "T-SQL, PL/SQL) and return ONLY a JSON object — no prose, no markdown "
    "fences. Extract the tables/views actually DEFINED in this file with "
    "their columns, and the foreign-key relationships between them. Flag a "
    "column or table sensitive when it plausibly holds PII (email, name, "
    "ssn, phone, address, password, card). Do not invent tables not present."
)


def discover_source_files(
    root: str, language: str, *, limit: int, max_bytes: int = 400_000
) -> list[Path]:
    """Return up to ``limit`` source files for ``language`` under ``root``,
    largest first (biggest files carry the most structure). Skips vendor /
    build dirs and absurdly large generated files."""
    exts = AGENT_LANGUAGE_EXTENSIONS.get(language)
    if not exts:
        return []
    root_path = Path(root)
    if not root_path.exists():
        return []
    candidates: list[tuple[int, Path]] = []
    for p in _iter_safe_source_files(root_path):
        if p.suffix.lower() not in exts:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0 or size > max_bytes:
            continue
        candidates.append((size, p))
    candidates.sort(key=lambda t: t[0], reverse=True)
    return [p for _size, p in candidates[:limit]]


# All extensions across the agent-eligible languages (for content search).
_ALL_AGENT_EXTS = {e for exts in AGENT_LANGUAGE_EXTENSIONS.values() for e in exts}


def find_candidate_files(
    root: str,
    terms: list[str],
    *,
    limit: int = 3,
    max_scan_bytes: int = 60_000,
    max_files_scanned: int = 4000,
) -> list[tuple[Path, str]]:
    """Rank source files most likely to *answer* a question, for the
    gap-driven deepen path (PR-149): when the graph can't answer, find the
    files whose path or content best match the question terms, extract those,
    then retry. Score = path-term hits (×5, cheap & strong) + content-term
    hits (×1, first ``max_scan_bytes``). Returns ``[(path, language), …]``
    highest score first, score>0 only. Bounded so a huge repo stays cheap."""
    terms = [t for t in terms if len(t) >= 3]
    if not terms:
        return []
    root_path = Path(root)
    if not root_path.exists():
        return []
    ext_to_lang = {
        e: lang for lang, exts in AGENT_LANGUAGE_EXTENSIONS.items() for e in exts
    }
    scored: list[tuple[int, int, Path, str]] = []
    scanned = 0
    for p in _iter_safe_source_files(root_path):
        if scanned >= max_files_scanned:
            break
        ext = p.suffix.lower()
        if ext not in _ALL_AGENT_EXTS:
            continue
        scanned += 1
        rel = str(p.relative_to(root_path)).lower()
        score = sum(5 for t in terms if t in rel)
        try:
            if p.stat().st_size <= max_scan_bytes:
                body = p.read_text(encoding="utf-8", errors="replace").lower()
                score += sum(body.count(t) for t in terms)
        except OSError:
            continue
        if score > 0:
            # prefer implementation files over headers on ties (smaller ext rank)
            scored.append((score, -len(rel), p, ext_to_lang[ext]))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [(p, lang) for _s, _r, p, lang in scored[:limit]]


def _build_extract_prompt(language: str, file_rel: str, code: str) -> str:
    id_prefix = f"{language}:{file_rel}::"
    return (
        f"Language: {language}\n"
        f"File: {file_rel}\n\n"
        "Return a JSON object with this exact shape:\n"
        "{\n"
        '  "symbols": [\n'
        '    {"id": "<stable id>", "name": "<symbol name>", '
        '"qualified_name": "<namespace/class-qualified name>", '
        '"kind": "function|method|class|interface|struct|enum|property|module|namespace|constructor", '
        '"line": <int>, "signature": "<one-line signature>", '
        '"calls": ["<name of a function/method this symbol invokes>"], '
        '"summary": "<=160 chars on what it does"}\n'
        "  ],\n"
        '  "edges": [\n'
        '    {"source_id": "<symbol id>", "target_id": "<symbol id>", '
        '"kind": "CALLS|CONTAINS|IMPLEMENTS|EXTENDS|REFERENCES"}\n'
        "  ],\n"
        '  "data_access": [\n'
        '    {"symbol_id": "<symbol id>", "table": "<db table name>", '
        '"access": "READS|WRITES"}\n'
        "  ]\n"
        "}\n\n"
        f'Use ids of the form "{id_prefix}<QualifiedName>" so edges can '
        "reference symbols. Only include edges whose endpoints are symbols "
        "you listed. In each symbol's `calls`, list the bare names of the "
        "functions/methods it invokes — INCLUDING ones defined in other "
        "files (the platform resolves those across the project). In "
        "data_access, record every DB table the symbol reads "
        "from or writes to — infer from SQL strings (SELECT/INSERT/UPDATE/"
        "DELETE), ORM calls, or query builders. If the file defines nothing, "
        'return {"symbols": [], "edges": [], "data_access": []}.\n\n'
        "Source:\n"
        "````\n"
        f"{code}\n"
        "````\n"
    )


def _build_db_extract_prompt(file_rel: str, code: str) -> str:
    return (
        f"SQL/DDL file: {file_rel}\n\n"
        "Return a JSON object with this exact shape:\n"
        "{\n"
        '  "entities": [\n'
        '    {"id": "data:<schema.table>", "name": "<table>", '
        '"kind": "table|view", '
        '"columns": [{"name": "<col>", "type": "<sql type>", '
        '"pk": true|false, "nullable": true|false, "sensitive": true|false}], '
        '"is_sensitive": true|false, '
        '"summary": "<=160 chars on what the table holds"}\n'
        "  ],\n"
        '  "edges": [\n'
        '    {"source_id": "data:<schema.table>", '
        '"target_id": "data:<schema.referenced_table>", "kind": "REFERENCES"}\n'
        "  ]\n"
        "}\n\n"
        'Use ids of the form "data:<schema>.<table>" (omit schema if none) so '
        "FK edges can reference tables. Only include edges whose endpoints are "
        "tables you listed. If the file defines no tables, return "
        '{"entities": [], "edges": []}.\n\n'
        "SQL:\n"
        "````\n"
        f"{code}\n"
        "````\n"
    )


async def extract_file_via_agent_sdk(
    *,
    language: str,
    file_rel: str,
    code: str,
    model: str = "claude-sonnet-4-6",
    timeout_s: int = 150,
    max_code_chars: int = MAX_AGENT_CODE_CHARS,
    on_provider_start: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    """Extract ``{"symbols": [...], "edges": [...]}`` from one file via the
    Claude Code subscription. Returns None on any failure (never raises);
    the caller treats None as "this file contributed nothing"."""
    # A prefix-only extraction cannot prove coverage for the omitted portion
    # of a file.  Reject it instead of returning plausible partial facts that
    # the orchestration layer might count as an authoritative file result.
    if len(code) > max_code_chars:
        log.warning(
            "agent_extract: %s input too large (%d > %d chars)",
            file_rel,
            len(code),
            max_code_chars,
        )
        return None
    if not is_agent_sdk_available():
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

    is_db = language in AGENT_DB_LANGUAGES
    snippet = code
    prompt = (
        _build_db_extract_prompt(file_rel, snippet)
        if is_db
        else _build_extract_prompt(language, file_rel, snippet)
    )
    opts = ClaudeAgentOptions(
        allowed_tools=[],
        disallowed_tools=["Bash", "Edit", "Write", "Read", "Task"],
        system_prompt=_DB_EXTRACT_SYSTEM if is_db else _EXTRACT_SYSTEM,
        max_turns=1,
        permission_mode="default",
        cwd=os.environ.get("MNEMOS_AGENT_SDK_CWD", "/tmp"),
        model=model if model and "claude" in model else None,
    )

    collected: list[str] = []
    collected_chars = 0
    is_error = False
    try:
        import asyncio

        async def _drain() -> bool:
            nonlocal collected_chars
            # Accounting boundary: local validation/options work is complete
            # and the next operation dispatches the provider stream.  The
            # synchronous hook adds no cancellation point before iteration.
            if on_provider_start is not None:
                on_provider_start()
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            collected_chars += len(block.text)
                            if collected_chars > 256 * 1024:
                                raise ValueError("agent_extract_output_too_large")
                            collected.append(block.text)
                elif isinstance(msg, ResultMessage):
                    return msg.is_error
            return False

        is_error = await asyncio.wait_for(_drain(), timeout=timeout_s)
    except asyncio.TimeoutError:
        log.warning("agent_extract: %s timed out after %ds", file_rel, timeout_s)
        return None
    except Exception as exc:  # noqa: BLE001
        # SDK exceptions may embed source/prompt fragments.  Keep diagnostics
        # structural and never echo the raw exception into logs.
        log.warning("agent_extract: provider failed: %s", exc.__class__.__name__)
        return None

    if is_error or not collected:
        return None
    parsed = _parse_json_response("\n".join(collected))
    if parsed is None:
        return None
    try:
        return normalize_agent_extract_payload(parsed, language=language)
    except AgentExtractContractError as exc:
        # The diagnostic contains a path and type/size only; raw provider text
        # and source code are deliberately absent.
        log.warning("agent_extract: contract rejected: %s", exc)
        return None


def _bounded_text(value: Any, *, limit: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return value.strip()[:limit]


def _agent_fact_id(
    prefix: str,
    language: str,
    file_rel: str,
    item: dict[str, Any],
) -> str:
    """Generate a stable isolated ID; model-provided IDs are never graph IDs.

    Result ordering is model-controlled and can change between calls, so the
    array index must not participate in identity.  Duplicate declarations with
    the same name are separated by their qualified name, signature, and line;
    exact duplicates intentionally coalesce.
    """

    # DB identity must retain the schema-qualified producer identifier.  Two
    # declarations such as public.users and audit.users can share a short
    # name in one DDL file and must never collapse into one graph node.  The
    # raw model ID remains isolated inside a digest; it never becomes a graph
    # ID or escapes the agent namespace.
    producer_identity = (
        _bounded_text(item.get("id"), limit=512) if prefix == "agent-data" else ""
    )
    identity = "\0".join((
        language,
        file_rel,
        producer_identity,
        _bounded_text(item.get("qualified_name"), limit=300),
        _bounded_text(item.get("name"), limit=300),
        _bounded_text(item.get("signature"), limit=500),
        str(item.get("line") or ""),
    ))
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"{prefix}:{language}:{digest}"


def to_envelopes(
    language: str, file_rel: str, extracted: dict[str, Any]
) -> list[dict[str, Any]]:
    """Convert the LLM extraction into analyzer-contract envelopes so the
    standard graph ingest (``_record_payload``) can consume them. All
    nodes/edges are stamped ``certainty="inferred"`` — LLM-derived."""
    envelopes: list[dict[str, Any]] = []
    id_map: dict[str, str] = {}
    producer_name = f"agent:{language}"
    safe_file = _bounded_text(file_rel.replace("\\", "/"), limit=4096)

    if language in AGENT_DB_LANGUAGES:
        # DB schema → DataEntity nodes (tables) carrying their columns +
        # PII flags, so the data-lookup / masking layer (spec §8) sees them.
        entities = extracted.get("entities", [])
        for ent in entities[:500] if isinstance(entities, list) else []:
            if not isinstance(ent, dict):
                continue
            model_id = _bounded_text(ent.get("id"), limit=512)
            name = _bounded_text(ent.get("name"), limit=300)
            if not model_id or not name:
                continue
            eid = _agent_fact_id("agent-data", language, safe_file, ent)
            if model_id in id_map:
                raise AgentExtractContractError(
                    "$.entities[].id",
                    "unique entity id",
                    model_id,
                    "Reject ambiguous references instead of selecting the first entity.",
                )
            id_map[model_id] = eid
            columns = ent.get("columns")
            safe_columns = []
            if isinstance(columns, list):
                for column in columns[:200]:
                    if not isinstance(column, dict):
                        continue
                    column_name = _bounded_text(column.get("name"), limit=200)
                    if column_name:
                        safe_columns.append({
                            "name": column_name,
                            "type": _bounded_text(column.get("type"), limit=200),
                            "pk": column.get("pk") if isinstance(column.get("pk"), bool) else False,
                            "nullable": (
                                column.get("nullable")
                                if isinstance(column.get("nullable"), bool)
                                else True
                            ),
                            "sensitive": (
                                column.get("sensitive")
                                if isinstance(column.get("sensitive"), bool)
                                else False
                            ),
                        })
            envelopes.append(
                {
                    "record_type": "data_entity",
                    "source_name": producer_name,
                    "data": {
                        "id": eid,
                        "name": name,
                        "kind": "view" if ent.get("kind") == "view" else "table",
                        "columns": safe_columns,
                        "is_sensitive": (
                            ent.get("is_sensitive")
                            if isinstance(ent.get("is_sensitive"), bool)
                            else False
                        ),
                        "file": safe_file,
                        "source_file": safe_file,
                        "summary": _bounded_text(
                            ent.get("summary"), limit=MAX_AGENT_SUMMARY_CHARS
                        ),
                        "certainty": "inferred",
                        "extractor": "claude_code",
                    },
                }
            )
    else:
        symbols = extracted.get("symbols", [])
        allowed_kinds = {
            "function", "method", "class", "interface", "struct", "enum",
            "property", "module", "namespace", "constructor",
        }
        for sym in symbols[:500] if isinstance(symbols, list) else []:
            if not isinstance(sym, dict):
                continue
            model_id = _bounded_text(sym.get("id"), limit=512)
            name = _bounded_text(sym.get("name"), limit=300)
            if not model_id or not name:
                continue
            sid = _agent_fact_id("agent-symbol", language, safe_file, sym)
            if model_id in id_map:
                raise AgentExtractContractError(
                    "$.symbols[].id",
                    "unique symbol id",
                    model_id,
                    "Reject ambiguous references instead of selecting the first symbol.",
                )
            id_map[model_id] = sid
            raw_kind = _bounded_text(sym.get("kind"), limit=40, default="function")
            kind = raw_kind if raw_kind in allowed_kinds else "function"
            line = sym.get("line")
            line = line if isinstance(line, int) and 1 <= line <= 10_000_000 else None
            envelopes.append(
                {
                    "record_type": "symbol",
                    "source_name": producer_name,
                    "data": {
                        "id": sid,
                        "name": name,
                        "kind": kind,
                        "language": language,
                        "file": safe_file,
                        "source_file": safe_file,
                        "line": line,
                        "signature": _bounded_text(sym.get("signature"), limit=2000),
                        "summary": _bounded_text(
                            sym.get("summary"), limit=MAX_AGENT_SUMMARY_CHARS
                        ),
                        # PR-154 — callee names (possibly cross-file); a
                        # post-extraction pass resolves them to CALLS edges.
                        "calls_out": [
                            c[:300] for c in (sym.get("calls") or [])[:200]
                            if isinstance(c, str)
                        ] if isinstance(sym.get("calls"), list) else [],
                        "certainty": "inferred",
                        "extractor": "claude_code",
                    },
                }
            )
        # Function → DB-table READS/WRITES edges (PR-145). Target is a
        # DataEntity (often created by the SQL stage), so it isn't a local
        # symbol — emit directly without the symbol dangling-filter. This is
        # what lets get_data_access answer "what DB does this touch".
        data_access = extracted.get("data_access", [])
        for da in data_access[:500] if isinstance(data_access, list) else []:
            if not isinstance(da, dict):
                continue
            sid = id_map.get(_bounded_text(da.get("symbol_id"), limit=512))
            table = _bounded_text(da.get("table"), limit=300)
            if not sid or not table:
                continue
            access = str(da.get("access", "READS")).upper()
            if access not in ("READS", "WRITES"):
                access = "READS"
            entity_id = table if table.startswith("data:") else f"data:{table.lower()}"
            envelopes.append(
                {
                    "record_type": "edge",
                    "source_name": producer_name,
                    "data": {
                        "source_id": sid,
                        "target_id": entity_id,
                        "kind": access,
                        "certainty": "inferred",
                        "metadata": {"extractor": "claude_code", "access_site": safe_file},
                    },
                }
            )

    edges = extracted.get("edges", [])
    allowed_edge_kinds = {"CALLS", "CONTAINS", "IMPLEMENTS", "EXTENDS", "REFERENCES"}
    for edge in edges[:1000] if isinstance(edges, list) else []:
        if not isinstance(edge, dict):
            continue
        src = id_map.get(_bounded_text(edge.get("source_id"), limit=512))
        tgt = id_map.get(_bounded_text(edge.get("target_id"), limit=512))
        # Only keep edges whose endpoints we actually emitted as nodes —
        # avoids dangling references the LLM may hallucinate.
        if not src or not tgt:
            continue
        raw_edge_kind = str(edge.get("kind", "CALLS")).upper()
        edge_kind = raw_edge_kind if raw_edge_kind in allowed_edge_kinds else "REFERENCES"
        envelopes.append(
            {
                "record_type": "edge",
                "source_name": producer_name,
                "data": {
                    "source_id": src,
                    "target_id": tgt,
                    "kind": edge_kind,
                    "certainty": "inferred",
                    "metadata": {"extractor": "claude_code"},
                },
            }
        )
    return envelopes
