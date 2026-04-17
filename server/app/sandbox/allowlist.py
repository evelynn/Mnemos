"""Phase-1 sandbox command allowlist (spec §11.5)."""

from __future__ import annotations

import re
import shlex

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^dotnet\s+(build|test|run)(\s|$)"),
    re.compile(r"^(npm|pnpm|yarn)\s+(run|test|install|ci)(\s|$)"),
    re.compile(r"^pytest(\s|$)"),
    re.compile(r"^python\s+-m\s+[\w\.]+"),
    re.compile(r"^git\s+(status|diff|log)(\s|$)"),
]


def is_allowed(command: str) -> bool:
    command = command.strip()
    if not command:
        return False
    try:
        shlex.split(command)
    except ValueError:
        return False
    return any(p.search(command) for p in _PATTERNS)
