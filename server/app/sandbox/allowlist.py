"""Closed command grammar for plan-worktree verification.

The MCP contract still accepts one command string for compatibility, but the
string is only a transport dialect.  It is parsed once into argv and is never
passed to a shell.  Validation deliberately rejects shell syntax, response
files, network locators, absolute paths, and parent traversal.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

_MAX_COMMAND_CHARS = 4096
_MAX_ARGUMENTS = 128
_MAX_TOKEN_CHARS = 1024
_FORBIDDEN_SHELL_SYNTAX = re.compile(r"[;&|<>`\r\n\x00]|\$\(|\$\{|%[^%\s]+%|![^!\s]+!")
_SAFE_SCRIPT_NAME = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")

_DOTNET_SUBCOMMANDS = frozenset({"build", "test", "run"})
_NODE_SUBCOMMANDS = frozenset({"run", "test"})
_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log"})
_PYTHON_MODULES = frozenset({"compileall", "mypy", "pytest", "ruff", "unittest"})
_PROGRAMS = frozenset({"dotnet", "git", "npm", "pnpm", "pytest", "python", "yarn"})

_FORBIDDEN_OPTIONS = frozenset(
    {
        "--config-env",
        "--exec-path",
        "--ext-diff",
        "--external-diff",
        "--interactive",
        "--output",
        "--paginate",
        "--receive-pack",
        "--textconv",
        "--upload-pack",
    }
)


@dataclass(frozen=True)
class AllowedCommand:
    """Canonical command representation accepted by the local executor."""

    argv: tuple[str, ...]
    program: str
    subcommand: str | None


def _candidate_path_value(token: str) -> str:
    if token.startswith("-") and "=" in token:
        return token.split("=", 1)[1]
    return token


def _token_has_unsafe_path(token: str) -> bool:
    value = _candidate_path_value(token).strip()
    if not value or value == "--":
        return False
    if value.startswith("@") or "://" in value:
        return True
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    return bool(
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
        or ".." in windows.parts
    )


def _has_forbidden_option(argv: tuple[str, ...]) -> bool:
    for token in argv[1:]:
        option = token.split("=", 1)[0]
        if option in _FORBIDDEN_OPTIONS:
            return True
        if token.startswith(("--config-env=", "--exec-path=", "--output=")):
            return True
    return False


def parse_allowed_command(command: str) -> AllowedCommand | None:
    """Normalize the compatibility string into one validated argv tuple.

    POSIX-style quoting is the MCP wire dialect on every host.  The resulting
    argv works with ``create_subprocess_exec`` on both POSIX and Windows and
    avoids platform-specific shell parsing entirely.
    """

    if not isinstance(command, str) or not command.strip():
        return None
    if len(command) > _MAX_COMMAND_CHARS or _FORBIDDEN_SHELL_SYNTAX.search(command):
        return None
    try:
        parsed = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None
    if not parsed or len(parsed) > _MAX_ARGUMENTS:
        return None
    if any(not token or len(token) > _MAX_TOKEN_CHARS for token in parsed):
        return None
    argv = tuple(parsed)
    program = argv[0].lower()
    if program not in _PROGRAMS or argv[0] != program:
        return None
    if any(_FORBIDDEN_SHELL_SYNTAX.search(token) for token in argv):
        return None
    if any(_token_has_unsafe_path(token) for token in argv[1:]):
        return None
    if _has_forbidden_option(argv):
        return None

    subcommand: str | None = None
    if program == "pytest":
        if "-c" in argv[1:]:
            return None
    elif program == "python":
        if len(argv) < 3 or argv[1] != "-m" or argv[2] not in _PYTHON_MODULES:
            return None
        subcommand = argv[2]
        if subcommand == "pytest" and "-c" in argv[3:]:
            return None
    elif program == "dotnet":
        if len(argv) < 2 or argv[1] not in _DOTNET_SUBCOMMANDS:
            return None
        subcommand = argv[1]
    elif program in {"npm", "pnpm", "yarn"}:
        if len(argv) < 2 or argv[1] not in _NODE_SUBCOMMANDS:
            return None
        subcommand = argv[1]
        if subcommand == "run" and (
            len(argv) < 3 or not _SAFE_SCRIPT_NAME.fullmatch(argv[2])
        ):
            return None
    elif program == "git":
        if len(argv) < 2 or argv[1] not in _GIT_SUBCOMMANDS:
            return None
        subcommand = argv[1]
        if "-c" in argv[2:]:
            return None

    return AllowedCommand(argv=argv, program=program, subcommand=subcommand)


def is_allowed(command: str) -> bool:
    """Compatibility predicate backed by the canonical argv validator."""

    return parse_allowed_command(command) is not None
