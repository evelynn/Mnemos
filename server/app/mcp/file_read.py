"""Safe file reader over the platform's repo mirror.

Rejects anything that escapes ``/var/lib/mnemos/repos/<project>/``.

For very large files the caller supplies ``start_line`` / ``end_line`` so
Claude Code can stream the file in windows instead of exhausting its
context on one huge payload.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

_REPO_ROOT = Path("/var/lib/mnemos/repos")
_MAX_BYTES = 256 * 1024
_MAX_WINDOW_LINES = 2000


async def read_project_file(
    *,
    project_id: uuid.UUID,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    base = (_REPO_ROOT / str(project_id)).resolve()
    if not base.exists():
        return {"error": "repo_mirror_missing", "base": str(base)}

    candidate = (base / file_path).resolve()
    if not str(candidate).startswith(str(base) + "/") and candidate != base:
        return {"error": "path_escapes_repo"}

    if not candidate.is_file():
        return {"error": "not_a_file", "path": str(candidate)}

    raw = candidate.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "content_base64": raw[:_MAX_BYTES].hex(),
            "encoding": "binary_hex",
            "size": len(raw),
            "truncated": len(raw) > _MAX_BYTES,
        }

    lines = text.splitlines()
    total_lines = len(lines)

    if start_line is not None or end_line is not None:
        s = max(1, start_line or 1)
        e = min(total_lines, end_line if end_line is not None else s + _MAX_WINDOW_LINES - 1)
        if e - s + 1 > _MAX_WINDOW_LINES:
            e = s + _MAX_WINDOW_LINES - 1
        window = "\n".join(lines[s - 1 : e])
        return {
            "content": window,
            "encoding": "utf-8",
            "total_lines": total_lines,
            "start_line": s,
            "end_line": e,
            "truncated": e < total_lines,
        }

    if len(raw) > _MAX_BYTES:
        window = "\n".join(lines[:_MAX_WINDOW_LINES])
        return {
            "content": window,
            "encoding": "utf-8",
            "total_lines": total_lines,
            "start_line": 1,
            "end_line": min(_MAX_WINDOW_LINES, total_lines),
            "truncated": True,
            "hint": "supply start_line/end_line to read a specific window",
        }
    return {
        "content": text,
        "encoding": "utf-8",
        "total_lines": total_lines,
        "start_line": 1,
        "end_line": total_lines,
        "truncated": False,
    }
