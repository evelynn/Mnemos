"""Run allowlisted commands inside a project worktree.

Phase-1 implementation invokes commands directly in the platform container's
workdir with a strict timeout. Docker-based isolation can be layered on in
Phase 2 when the sandbox image set stabilises.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.sandbox.allowlist import is_allowed


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


async def run_in_sandbox(
    *,
    command: str,
    working_dir: str | Path,
    timeout_sec: int = 300,
) -> SandboxResult:
    if not is_allowed(command):
        return SandboxResult(
            exit_code=126,
            stdout="",
            stderr=f"command_not_allowed: {command}",
            duration_ms=0,
        )
    loop = asyncio.get_running_loop()
    start = loop.time()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(working_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return SandboxResult(
            exit_code=124,
            stdout="",
            stderr="timeout",
            duration_ms=int((loop.time() - start) * 1000),
        )
    return SandboxResult(
        exit_code=proc.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        duration_ms=int((loop.time() - start) * 1000),
    )
