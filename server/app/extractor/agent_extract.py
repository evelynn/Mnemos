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

import logging
import os
from pathlib import Path
from typing import Any

from app.extractor.agent_sdk import _parse_json_response, is_agent_sdk_available

log = logging.getLogger(__name__)

# Languages we know how to find on disk. A language absent here simply
# gets skipped by the agent-extraction stage (recorded, not crashed).
AGENT_LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "cpp": (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hxx", ".hh", ".h", ".c"),
    "go": (".go",),
    "rust": (".rs",),
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "ruby": (".rb",),
    "php": (".php",),
    "swift": (".swift",),
    "scala": (".scala",),
}

# Directories that never hold first-party source — skip to spend the
# (bounded) LLM budget on code that matters.
_SKIP_DIRS = {
    ".git", "node_modules", "build", "dist", "out", "bin", "obj",
    "third_party", "thirdparty", "external", "vendor", "deps",
    "__pycache__", ".vs", ".vscode", "cmake-build-debug",
}

_EXTRACT_SYSTEM = (
    "You are a precise source-code structure extractor for a code-knowledge "
    "graph. You receive ONE source file and return ONLY a JSON object — no "
    "prose, no markdown fences. Extract the top-level and member symbols "
    "(functions, methods, classes, structs, namespaces, enums) actually "
    "DEFINED in this file, and the call/containment edges you can see with "
    "high confidence. Do not invent symbols that aren't in the file."
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
    for p in root_path.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
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


def _build_extract_prompt(language: str, file_rel: str, code: str) -> str:
    id_prefix = f"{language}:{file_rel}::"
    return (
        f"Language: {language}\n"
        f"File: {file_rel}\n\n"
        "Return a JSON object with this exact shape:\n"
        "{\n"
        '  "symbols": [\n'
        '    {"id": "<stable id>", "name": "<symbol name>", '
        '"kind": "function|method|class|struct|namespace|enum", '
        '"line": <int>, "signature": "<one-line signature>", '
        '"summary": "<=160 chars on what it does"}\n'
        "  ],\n"
        '  "edges": [\n'
        '    {"source_id": "<symbol id>", "target_id": "<symbol id>", '
        '"kind": "CALLS|CONTAINS"}\n'
        "  ]\n"
        "}\n\n"
        f'Use ids of the form "{id_prefix}<QualifiedName>" so edges can '
        "reference symbols. Only include edges whose endpoints are symbols "
        "you listed. If the file defines nothing, return "
        '{"symbols": [], "edges": []}.\n\n'
        "Source:\n"
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
    timeout_s: int = 120,
    max_code_chars: int = 16_000,
) -> dict[str, Any] | None:
    """Extract ``{"symbols": [...], "edges": [...]}`` from one file via the
    Claude Code subscription. Returns None on any failure (never raises);
    the caller treats None as "this file contributed nothing"."""
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

    snippet = code if len(code) <= max_code_chars else code[:max_code_chars]
    prompt = _build_extract_prompt(language, file_rel, snippet)
    opts = ClaudeAgentOptions(
        allowed_tools=[],
        disallowed_tools=["Bash", "Edit", "Write", "Read", "Task"],
        system_prompt=_EXTRACT_SYSTEM,
        max_turns=1,
        permission_mode="default",
        cwd=os.environ.get("MNEMOS_AGENT_SDK_CWD", "/tmp"),
        model=model if model and "claude" in model else None,
    )

    collected: list[str] = []
    is_error = False
    try:
        import asyncio

        async def _drain() -> bool:
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            collected.append(block.text)
                elif isinstance(msg, ResultMessage):
                    return msg.is_error
            return False

        is_error = await asyncio.wait_for(_drain(), timeout=timeout_s)
    except asyncio.TimeoutError:
        log.warning("agent_extract: %s timed out after %ds", file_rel, timeout_s)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_extract: %s: %s: %s", file_rel, exc.__class__.__name__, exc)
        return None

    if is_error or not collected:
        return None
    parsed = _parse_json_response("\n".join(collected))
    if parsed is None:
        return None
    # Normalise shape so the caller can trust it.
    symbols = parsed.get("symbols") or []
    edges = parsed.get("edges") or []
    if not isinstance(symbols, list) or not isinstance(edges, list):
        return None
    return {"symbols": symbols, "edges": edges}


def to_envelopes(
    language: str, file_rel: str, extracted: dict[str, Any]
) -> list[dict[str, Any]]:
    """Convert the LLM extraction into analyzer-contract envelopes so the
    standard graph ingest (``_record_payload``) can consume them. All
    nodes/edges are stamped ``certainty="inferred"`` — LLM-derived."""
    envelopes: list[dict[str, Any]] = []
    valid_ids: set[str] = set()
    for sym in extracted.get("symbols", []):
        if not isinstance(sym, dict):
            continue
        sid = sym.get("id")
        if not sid or not isinstance(sid, str):
            continue
        valid_ids.add(sid)
        envelopes.append(
            {
                "record_type": "symbol",
                "source_name": file_rel,
                "data": {
                    "id": sid,
                    "name": sym.get("name", sid.rsplit("::", 1)[-1]),
                    "kind": sym.get("kind", "function"),
                    "language": language,
                    "file": file_rel,
                    "line": sym.get("line"),
                    "signature": sym.get("signature"),
                    "summary": sym.get("summary"),
                    "certainty": "inferred",
                    "extractor": "claude_code",
                },
            }
        )
    for edge in extracted.get("edges", []):
        if not isinstance(edge, dict):
            continue
        src = edge.get("source_id")
        tgt = edge.get("target_id")
        # Only keep edges whose endpoints we actually emitted as nodes —
        # avoids dangling references the LLM may hallucinate.
        if src not in valid_ids or tgt not in valid_ids:
            continue
        envelopes.append(
            {
                "record_type": "edge",
                "source_name": file_rel,
                "data": {
                    "source_id": src,
                    "target_id": tgt,
                    "kind": edge.get("kind", "CALLS"),
                    "certainty": "inferred",
                    "metadata": {"extractor": "claude_code"},
                },
            }
        )
    return envelopes
