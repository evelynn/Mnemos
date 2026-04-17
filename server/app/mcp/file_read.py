"""Safe file reader over the platform's repo mirror.

Rejects anything that escapes ``/var/lib/mnemos/repos/<project>/``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

_REPO_ROOT = Path("/var/lib/mnemos/repos")
_MAX_BYTES = 256 * 1024


async def read_project_file(
    *, project_id: uuid.UUID, file_path: str
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
    if len(raw) > _MAX_BYTES:
        return {
            "error": "file_too_large",
            "size": len(raw),
            "max": _MAX_BYTES,
        }
    try:
        text = raw.decode("utf-8")
        return {"content": text, "encoding": "utf-8", "size": len(raw)}
    except UnicodeDecodeError:
        return {
            "content_base64": raw[:_MAX_BYTES].hex(),
            "encoding": "binary_hex",
            "size": len(raw),
        }
