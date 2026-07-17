from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

from app.sandbox.allowlist import is_allowed, parse_allowed_command
from app.sandbox.runner import SandboxResult, redact_sandbox_output, run_in_sandbox


@pytest.fixture
def trusted_local_execution(monkeypatch) -> None:
    """Opt in only tests that intentionally exercise the risky local backend."""

    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.setenv("MNEMOS_TRUSTED_LOCAL_EXECUTION", "1")


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q & set",
        "pytest -q && env",
        "pytest -q | cat",
        "pytest -q; env",
        "pytest -q > secrets.txt",
        "pytest -q\nenv",
        "pytest `env`",
        "pytest $(env)",
        "pytest %DATABASE_URL%",
        "pytest !SECRET_KEY!",
        "pytest ../../outside/test.py",
        "pytest C:/outside/test.py",
        "pytest @outside.args",
        "python -c 'print(1)'",
        "python -m os",
        "npm install",
        "pnpm ci",
        "git push",
        "git -c core.pager=env status",
    ],
)
def test_command_parser_rejects_shell_injection_and_escaping_paths(command: str) -> None:
    assert parse_allowed_command(command) is None
    assert is_allowed(command) is False


@pytest.mark.parametrize(
    ("command", "program", "subcommand"),
    [
        ("pytest -q tests", "pytest", None),
        ('pytest -q -k "fast and not network"', "pytest", None),
        ("python -m pytest -q tests", "python", "pytest"),
        ("python -m compileall src", "python", "compileall"),
        ("dotnet test -c Release", "dotnet", "test"),
        ("npm test -- --runInBand", "npm", "test"),
        ("pnpm run lint", "pnpm", "run"),
        ("yarn run test:unit", "yarn", "run"),
        ("git status --short", "git", "status"),
        ("git diff --check", "git", "diff"),
        ("git log --oneline -n 5", "git", "log"),
    ],
)
def test_command_parser_accepts_compatibility_verification_commands(
    command: str,
    program: str,
    subcommand: str | None,
) -> None:
    parsed = parse_allowed_command(command)

    assert parsed is not None
    assert parsed.program == program
    assert parsed.subcommand == subcommand


@pytest.mark.asyncio
async def test_default_backend_is_unavailable_before_command_inspection_or_spawn(
    tmp_path,
    monkeypatch,
) -> None:
    from app.sandbox import runner

    monkeypatch.delenv("MNEMOS_ENV", raising=False)
    monkeypatch.delenv("MNEMOS_TRUSTED_LOCAL_EXECUTION", raising=False)

    def forbidden_parse(_command: str):
        raise AssertionError("default-disabled backend inspected the command")

    async def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("default-disabled backend spawned a process")

    monkeypatch.setattr(runner, "parse_allowed_command", forbidden_parse)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", forbidden_spawn)

    result = await runner.run_in_sandbox(
        command="pytest -q",
        working_dir=tmp_path,
    )

    assert result.status == "rejected"
    assert result.failure_code == "sandbox_backend_unavailable"
    assert result.program is None


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["production", "prod"])
async def test_production_cannot_bypass_disabled_backend_with_opt_in(
    tmp_path,
    monkeypatch,
    environment: str,
) -> None:
    from app.sandbox import runner

    monkeypatch.setenv("MNEMOS_ENV", environment)
    monkeypatch.setenv("MNEMOS_TRUSTED_LOCAL_EXECUTION", "1")

    def forbidden_resolution(*_args, **_kwargs):
        raise AssertionError("production attempted executable resolution")

    async def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("production spawned a process")

    monkeypatch.setattr(runner, "_resolve_executable", forbidden_resolution)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", forbidden_spawn)

    result = await runner.run_in_sandbox(
        command="pytest -q",
        working_dir=tmp_path,
    )

    assert result.status == "rejected"
    assert result.failure_code == "sandbox_backend_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["development", "local", "test", "staging"])
async def test_nonproduction_backend_stays_disabled_without_explicit_opt_in(
    tmp_path,
    monkeypatch,
    environment: str,
) -> None:
    from app.sandbox import runner

    monkeypatch.setenv("MNEMOS_ENV", environment)
    monkeypatch.delenv("MNEMOS_TRUSTED_LOCAL_EXECUTION", raising=False)

    def forbidden_resolution(*_args, **_kwargs):
        raise AssertionError("disabled backend attempted executable resolution")

    monkeypatch.setattr(runner, "_resolve_executable", forbidden_resolution)

    result = await runner.run_in_sandbox(
        command="pytest -q",
        working_dir=tmp_path,
    )

    assert result.failure_code == "sandbox_backend_unavailable"


@pytest.mark.asyncio
async def test_development_trusted_repo_opt_in_reaches_local_argv_backend(
    tmp_path,
    monkeypatch,
) -> None:
    from app.sandbox import runner

    attempts = 0

    async def fail_spawn(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError("static test failure")

    monkeypatch.setenv("MNEMOS_ENV", "development")
    monkeypatch.setenv("MNEMOS_TRUSTED_LOCAL_EXECUTION", "1")
    monkeypatch.setattr(
        runner,
        "_resolve_executable",
        lambda *_args, **_kwargs: Path(sys.executable).resolve(),
    )
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fail_spawn)

    result = await runner.run_in_sandbox(
        command="pytest -q",
        working_dir=tmp_path,
    )

    assert attempts == 1
    assert result.failure_code == "sandbox_spawn_failed"


@pytest.mark.asyncio
async def test_rejected_command_never_echoes_secret_input(
    tmp_path,
    trusted_local_execution,
) -> None:
    secret = "sandbox-command-secret-value"

    result = await run_in_sandbox(
        command=f"pytest -q & echo {secret}",
        working_dir=tmp_path,
    )

    assert result.status == "rejected"
    assert result.failure_code == "sandbox_command_not_allowed"
    assert secret not in repr(result)


@pytest.mark.asyncio
async def test_spawn_failure_returns_static_envelope_without_exception_text(
    tmp_path,
    monkeypatch,
    trusted_local_execution,
) -> None:
    from app.sandbox import runner

    secret = "spawn-error-secret-value"

    async def fail_spawn(*_args, **_kwargs):
        raise OSError(f"credential={secret}")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fail_spawn)

    result = await runner.run_in_sandbox(
        command="pytest -q",
        working_dir=tmp_path,
    )

    assert result.status == "failed"
    assert result.failure_code == "sandbox_spawn_failed"
    assert secret not in repr(result)


@pytest.mark.asyncio
async def test_child_receives_scrubbed_environment(
    tmp_path,
    monkeypatch,
    trusted_local_execution,
) -> None:
    secret = "postgresql://admin:db-secret@db.invalid/mnemos"
    monkeypatch.setenv("DATABASE_URL", secret)
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-value")
    (tmp_path / "test_environment.py").write_text(
        """
import os


def test_environment_is_scrubbed():
    print(os.environ.get("DATABASE_URL", "database-url-missing"))
    print(os.environ.get("OPENAI_API_KEY", "provider-key-missing"))
    assert "DATABASE_URL" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
""".lstrip(),
        encoding="utf-8",
    )

    result = await run_in_sandbox(
        command="pytest -q -s test_environment.py",
        working_dir=tmp_path,
        timeout_sec=20,
    )

    assert result.status == "completed"
    assert result.exit_code == 0
    assert "database-url-missing" in result.stdout
    assert "provider-key-missing" in result.stdout
    assert "db-secret" not in repr(result)
    assert "provider-secret-value" not in repr(result)


def test_output_redactor_removes_credentials_provider_body_and_private_path() -> None:
    raw = "\n".join(
        (
            "DATABASE_URL=postgresql://admin:db-password@db.invalid/mnemos",
            "Authorization: Bearer provider-token-value",
            '"prompt":"private source body"',
            "api_key=sk-ant-abcdefghijklmnop",
            "C:/private/worktree/src/secret.py",
        )
    )

    redacted = redact_sandbox_output(
        raw,
        known_secrets=("provider-token-value",),
        private_paths=("C:/private/worktree",),
    )

    for secret in (
        "db-password",
        "provider-token-value",
        "private source body",
        "sk-ant-abcdefghijklmnop",
        "C:/private/worktree",
    ):
        assert secret not in redacted
    assert "[REDACTED]" in redacted
    assert "<SANDBOX_PATH>" in redacted


@pytest.mark.asyncio
async def test_large_output_is_bounded_before_mcp_projection(
    tmp_path,
    trusted_local_execution,
) -> None:
    (tmp_path / "test_large_output.py").write_text(
        "def test_large_output():\n    print('x' * 100_000)\n",
        encoding="utf-8",
    )

    result = await run_in_sandbox(
        command="pytest -q -s test_large_output.py",
        working_dir=tmp_path,
        timeout_sec=20,
        max_output_bytes=1024,
    )

    assert result.status == "output_limit_exceeded"
    assert result.failure_code == "sandbox_output_limit_exceeded"
    assert result.stdout_truncated is True
    assert len(result.stdout.encode("utf-8")) <= 1024


@pytest.mark.asyncio
async def test_timeout_kills_descendant_process_tree(
    tmp_path,
    trusted_local_execution,
) -> None:
    (tmp_path / "test_timeout_tree.py").write_text(
        """
import subprocess
import sys
import time


def test_timeout_tree():
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib,time; time.sleep(2); pathlib.Path('orphan.txt').write_text('bad')",
        ]
    )
    time.sleep(30)
""".lstrip(),
        encoding="utf-8",
    )

    result = await run_in_sandbox(
        command="pytest -q -s test_timeout_tree.py",
        working_dir=tmp_path,
        timeout_sec=1,
    )
    await asyncio.sleep(3)

    assert result.status == "timeout"
    assert result.failure_code == "sandbox_timeout"
    assert not (tmp_path / "orphan.txt").exists()


def test_sandbox_audit_projection_contains_no_raw_command() -> None:
    from app.mcp.server import _sandbox_audit_details

    secret = "audit-command-secret-value"
    command = f'pytest -q -k "{secret}"'

    details = _sandbox_audit_details(
        {"plan_id": "private-plan", "command": command, "timeout_sec": 20}
    )
    serialized = json.dumps(details, sort_keys=True)

    assert details["schema"] == "mnemos.mcp_audit.v1"
    assert details["argument_count"] == 3
    assert details["recognized_argument_count"] == 3
    assert details["unknown_argument_count"] == 0
    assert details["argument_shapes"]["command"] == {
        "type": "string",
        "chars": len(command),
    }
    assert secret not in serialized
    assert command not in serialized


def test_mcp_sandbox_result_uses_bounded_canonical_envelope() -> None:
    from app.mcp.dev_tools import _sandbox_result_payload
    from app.plan_provenance import PlanSourceRevision

    revision = PlanSourceRevision(
        run_id=uuid.uuid4(),
        git_sha="a" * 40,
        source_generation=3,
        overlay_generation=4,
    )
    result = SandboxResult(
        status="rejected",
        failure_code="sandbox_backend_unavailable",
        exit_code=None,
        stdout="",
        stderr="",
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=25,
        program=None,
        subcommand=None,
    )

    payload = _sandbox_result_payload(result, revision)

    assert payload["schema"] == "mnemos.sandbox_result.v1"
    assert payload["status"] == "rejected"
    assert payload["failure_code"] == "sandbox_backend_unavailable"
    assert payload["exit_code"] is None
    assert payload["stdout"] == {
        "text": "",
        "bytes_seen": 0,
        "truncated": False,
    }
    assert payload["command"] == {"program": None, "subcommand": None}
    assert payload["source_revision"]["run_id"] == str(revision.run_id)
