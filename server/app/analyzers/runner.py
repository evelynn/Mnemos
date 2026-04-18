"""Streaming subprocess runner for analyzer plugins.

The runner treats every analyzer as a black box that implements the CLI
contract documented in ``docs/analyzer-contract.md``. Output records stream
back as they are produced, so downstream merge/summarisation can consume
results incrementally (see spec §2.6 / §2.7).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunRecord:
    """One JSON Lines record emitted by an analyzer, plus a stderr tag."""

    stream: str  # "stdout" | "stderr"
    payload: dict


class AnalyzerRunner:
    """Spawns an analyzer binary (or docker-run wrapper) and yields records.

    The Phase-1 implementation assumes the binary is reachable on PATH inside
    the platform container. Docker-based isolation is introduced alongside the
    sandbox manager in Week 7.
    """

    def __init__(self, binary: str):
        self.binary = binary

    async def run(
        self,
        verb: str,
        path: str | Path,
        *,
        extra_args: list[str] | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> AsyncIterator[RunRecord]:
        args = [self.binary, verb, str(path)]
        if extra_args:
            args.extend(extra_args)

        # Log the binary + verb + path but never extra_args, since the
        # caller may put credentials there (DB live_schema --conn-ref).
        log.info("spawning analyzer: %s %s %s", self.binary, verb, str(path))

        proc_env = None
        if env is not None:
            import os as _os

            proc_env = {**_os.environ, **env}

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=proc_env,
        )

        async def _drain(stream: asyncio.StreamReader | None, tag: str):
            if stream is None:
                return
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"raw": text, "parse_error": True}
                await queue.put(RunRecord(stream=tag, payload=payload))

        queue: asyncio.Queue[RunRecord | None] = asyncio.Queue()
        stdout_task = asyncio.create_task(_drain(proc.stdout, "stdout"))
        stderr_task = asyncio.create_task(_drain(proc.stderr, "stderr"))

        async def _sentinel():
            await asyncio.gather(stdout_task, stderr_task)
            await queue.put(None)

        sentinel_task = asyncio.create_task(_sentinel())

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            exit_code = await proc.wait()
            await sentinel_task
            if exit_code != 0:
                log.warning("analyzer %s exited %d", self.binary, exit_code)

    async def run_collect(self, verb: str, path: str | Path) -> list[RunRecord]:
        return [rec async for rec in self.run(verb, path)]
