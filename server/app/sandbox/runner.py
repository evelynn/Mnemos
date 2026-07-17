"""Bounded, privacy-safe argv execution for an approved plan worktree."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.sandbox.allowlist import AllowedCommand, parse_allowed_command

_DEFAULT_MAX_OUTPUT_BYTES = 20_000
_MAX_TIMEOUT_SEC = 1800
_READ_CHUNK_BYTES = 4096
_TRUSTED_LOCAL_EXECUTION_ENV = "MNEMOS_TRUSTED_LOCAL_EXECUTION"
_TRUSTED_LOCAL_ENVIRONMENTS = frozenset({"dev", "development", "local", "test"})
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:API[_-]?KEY|AUTH|CREDENTIAL|DATABASE_URL|DSN|PASSWORD|PASSWD|PRIVATE|"
    r"REDIS_URL|SECRET|TOKEN)",
    re.IGNORECASE,
)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|authorization|credential|password|passwd|private[_-]?key|"
    r"secret|token)\s*([:=])\s*([^\s,;]+)"
)
_JSON_SECRET = re.compile(
    r'(?i)("(?:api[_-]?key|authorization|body|credential|password|prompt|request|'
    r'response|secret|source|token)"\s*:\s*")[^"]*(")'
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_DSN_SECRET = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s/@]+(@)"
)
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"glpat-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,})\b"
)
_PEM_SECRET = re.compile(
    r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----",
    re.DOTALL,
)

SandboxStatus = Literal[
    "completed",
    "failed",
    "output_limit_exceeded",
    "rejected",
    "timeout",
]


@dataclass(frozen=True)
class SandboxResult:
    status: SandboxStatus
    failure_code: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    program: str | None
    subcommand: str | None


@dataclass(frozen=True)
class _CapturedStream:
    value: bytes
    bytes_seen: int
    truncated: bool


def trusted_local_execution_enabled() -> bool:
    """Return whether the explicitly risky local argv backend may run.

    The local runner is not an OS containment boundary: repository code can
    still open host files or sockets through language APIs.  Keep it disabled
    unless a developer opts in for a *trusted* repository in an explicitly
    local/test environment.  Unknown environments fail closed, and production
    cannot be overridden by the opt-in flag.
    """

    environment = os.environ.get("MNEMOS_ENV", "production").strip().casefold()
    opted_in = os.environ.get(_TRUSTED_LOCAL_EXECUTION_ENV, "").strip() == "1"
    return environment in _TRUSTED_LOCAL_ENVIRONMENTS and opted_in


def _rejected(code: str, *, program: str | None = None) -> SandboxResult:
    return SandboxResult(
        status="rejected",
        failure_code=code,
        exit_code=None,
        stdout="",
        stderr="",
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=0,
        program=program,
        subcommand=None,
    )


def _spawn_failed(command: AllowedCommand, *, duration_ms: int) -> SandboxResult:
    return SandboxResult(
        status="failed",
        failure_code="sandbox_spawn_failed",
        exit_code=None,
        stdout="",
        stderr="",
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=duration_ms,
        program=command.program,
        subcommand=command.subcommand,
    )


def _known_parent_secrets() -> tuple[str, ...]:
    values = {
        value
        for name, value in os.environ.items()
        if value and len(value) >= 4 and _SENSITIVE_ENV_NAME.search(name)
    }
    return tuple(sorted(values, key=len, reverse=True))


def redact_sandbox_output(
    value: str,
    *,
    known_secrets: tuple[str, ...] = (),
    private_paths: tuple[str, ...] = (),
) -> str:
    """Redact bounded output before it enters an MCP/provider context."""

    redacted = value
    for secret in known_secrets:
        redacted = redacted.replace(secret, "[REDACTED]")
    for private_path in sorted(
        {path for path in private_paths if path}, key=len, reverse=True
    ):
        redacted = redacted.replace(private_path, "<SANDBOX_PATH>")
    redacted = _PEM_SECRET.sub("[REDACTED_PEM]", redacted)
    redacted = _BEARER_SECRET.sub("Bearer [REDACTED]", redacted)
    redacted = _DSN_SECRET.sub(r"\1[REDACTED]\2", redacted)
    redacted = _KNOWN_TOKEN.sub("[REDACTED_TOKEN]", redacted)
    redacted = _KEY_VALUE_SECRET.sub(r"\1\2[REDACTED]", redacted)
    return _JSON_SECRET.sub(r"\1[REDACTED]\2", redacted)


def _safe_search_path(executable: Path) -> str:
    candidates = [executable.parent, Path(sys.executable).resolve().parent]
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        candidates.extend((system_root / "System32", system_root))
    else:
        candidates.extend((Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin")))
    unique: list[str] = []
    for candidate in candidates:
        try:
            resolved = str(candidate.resolve(strict=True))
        except OSError:
            continue
        if resolved not in unique:
            unique.append(resolved)
    return os.pathsep.join(unique)


def _sandbox_environment(executable: Path, scratch: Path) -> dict[str, str]:
    scratch_text = str(scratch)
    env = {
        "CI": "1",
        "DOTNET_CLI_HOME": scratch_text,
        "GIT_CONFIG_GLOBAL": str(scratch / "gitconfig-empty"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_PAGER": "cat",
        "HOME": scratch_text,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_CACHE": str(scratch / "npm-cache"),
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NO_COLOR": "1",
        "PAGER": "cat",
        "PATH": _safe_search_path(executable),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TEMP": scratch_text,
        "TMP": scratch_text,
        "TMPDIR": scratch_text,
    }
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        env.update(
            {
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                "SystemRoot": system_root,
                "USERPROFILE": scratch_text,
                "WINDIR": os.environ.get("WINDIR", system_root),
            }
        )
    return env


def _resolve_executable(command: AllowedCommand, working_dir: Path) -> Path | None:
    raw: str | None
    if command.program == "python":
        raw = sys.executable
    elif command.program == "pytest":
        scripts = Path(sys.executable).resolve().parent
        candidates = (
            scripts / "pytest.exe",
            scripts / "pytest",
        )
        raw = next((str(candidate) for candidate in candidates if candidate.is_file()), None)
    else:
        raw = shutil.which(command.program)
    if raw is None:
        return None
    try:
        executable = Path(raw).resolve(strict=True)
    except OSError:
        return None
    if executable.is_relative_to(working_dir):
        return None
    # Windows batch files are interpreted by cmd.exe even with shell=False.
    # Reject them until a trusted node.exe + CLI-script adapter exists.
    if os.name == "nt" and executable.suffix.lower() in {".bat", ".cmd"}:
        return None
    return executable


def _arguments_stay_within_worktree(
    command: AllowedCommand,
    working_dir: Path,
) -> bool:
    for token in command.argv[1:]:
        if token == "--" or (token.startswith("-") and "=" not in token):
            continue
        value = token.split("=", 1)[1] if token.startswith("-") else token
        # Pytest node ids append ``::test_name`` to a relative file path.
        path_value = value.split("::", 1)[0]
        if not path_value:
            continue
        try:
            candidate = (working_dir / path_value).resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        if not candidate.is_relative_to(working_dir):
            return False
    return True


async def _capture_stream(
    stream: asyncio.StreamReader,
    *,
    max_bytes: int,
    output_limit: asyncio.Event,
) -> _CapturedStream:
    captured = bytearray()
    bytes_seen = 0
    truncated = False
    while chunk := await stream.read(_READ_CHUNK_BYTES):
        bytes_seen += len(chunk)
        remaining = max_bytes - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if bytes_seen > max_bytes:
            truncated = True
            output_limit.set()
    return _CapturedStream(bytes(captured), bytes_seen, truncated)


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        if taskkill.is_file():
            try:
                killer = await asyncio.create_subprocess_exec(
                    str(taskkill),
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                await killer.wait()
            except OSError:
                proc.kill()
        else:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def run_in_sandbox(
    *,
    command: str,
    working_dir: str | Path,
    timeout_sec: int = 300,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> SandboxResult:
    # This check intentionally precedes command parsing, path resolution, and
    # executable discovery.  The default/production path must never spawn (or
    # even inspect) a caller-supplied command as if local argv execution were a
    # real sandbox.
    if not trusted_local_execution_enabled():
        return _rejected("sandbox_backend_unavailable")

    parsed = parse_allowed_command(command)
    if parsed is None:
        return _rejected("sandbox_command_not_allowed")
    try:
        cwd = Path(working_dir).resolve(strict=True)
    except (OSError, RuntimeError):
        return _rejected("sandbox_working_directory_invalid", program=parsed.program)
    if not cwd.is_dir() or Path(working_dir).is_symlink():
        return _rejected("sandbox_working_directory_invalid", program=parsed.program)
    if not _arguments_stay_within_worktree(parsed, cwd):
        return _rejected("sandbox_path_outside_worktree", program=parsed.program)
    executable = _resolve_executable(parsed, cwd)
    if executable is None:
        return _rejected("sandbox_executable_unavailable", program=parsed.program)

    timeout = max(1, min(int(timeout_sec), _MAX_TIMEOUT_SEC))
    output_cap = max(256, min(int(max_output_bytes), _DEFAULT_MAX_OUTPUT_BYTES))
    loop = asyncio.get_running_loop()
    started = loop.time()
    known_secrets = _known_parent_secrets()

    with tempfile.TemporaryDirectory(prefix="mnemos-sandbox-") as scratch_raw:
        scratch = Path(scratch_raw).resolve()
        env = _sandbox_environment(executable, scratch)
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True
        try:
            proc = await asyncio.create_subprocess_exec(
                str(executable),
                *parsed.argv[1:],
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except OSError:
            return _spawn_failed(
                parsed,
                duration_ms=int((loop.time() - started) * 1000),
            )
        assert proc.stdout is not None and proc.stderr is not None
        output_limit = asyncio.Event()
        stdout_task = asyncio.create_task(
            _capture_stream(
                proc.stdout,
                max_bytes=output_cap,
                output_limit=output_limit,
            )
        )
        stderr_task = asyncio.create_task(
            _capture_stream(
                proc.stderr,
                max_bytes=output_cap,
                output_limit=output_limit,
            )
        )
        wait_task = asyncio.create_task(proc.wait())
        limit_task = asyncio.create_task(output_limit.wait())
        status: SandboxStatus = "completed"
        failure_code: str | None = None
        try:
            done, _ = await asyncio.wait(
                {wait_task, limit_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                status = "timeout"
                failure_code = "sandbox_timeout"
                await _terminate_process_tree(proc)
            elif limit_task in done and output_limit.is_set():
                status = "output_limit_exceeded"
                failure_code = "sandbox_output_limit_exceeded"
                await _terminate_process_tree(proc)
            else:
                await wait_task
        except asyncio.CancelledError:
            await asyncio.shield(_terminate_process_tree(proc))
            raise
        finally:
            limit_task.cancel()
            await asyncio.gather(limit_task, return_exceptions=True)
        stdout_capture, stderr_capture = await asyncio.gather(
            stdout_task,
            stderr_task,
        )
        if (stdout_capture.truncated or stderr_capture.truncated) and status == "completed":
            status = "output_limit_exceeded"
            failure_code = "sandbox_output_limit_exceeded"
        exit_code = proc.returncode
        if status == "completed" and exit_code:
            status = "failed"
            failure_code = "sandbox_command_failed"
        private_paths = (str(cwd), str(scratch), str(executable.parent))
        stdout = redact_sandbox_output(
            stdout_capture.value.decode("utf-8", errors="replace"),
            known_secrets=known_secrets,
            private_paths=private_paths,
        )
        stderr = redact_sandbox_output(
            stderr_capture.value.decode("utf-8", errors="replace"),
            known_secrets=known_secrets,
            private_paths=private_paths,
        )
        return SandboxResult(
            status=status,
            failure_code=failure_code,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=stdout_capture.bytes_seen,
            stderr_bytes=stderr_capture.bytes_seen,
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            duration_ms=int((loop.time() - started) * 1000),
            program=parsed.program,
            subcommand=parsed.subcommand,
        )
