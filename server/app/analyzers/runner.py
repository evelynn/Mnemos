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
import os
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# PR-153 — run the pure-stdlib in-repo analyzers without Docker. The basic
# (non-Docker) configuration ships the analyzer source under ``analyzers/``;
# ggoss-py needs only Python's ``ast`` so it runs directly from source. When
# the binary isn't installed on PATH and ``MNEMOS_INREPO_ANALYZERS`` is set
# (serve_local does this), invoke ``python <script> <verb> <path>`` so a
# docker-free deployment gets *verified* deterministic Python extraction
# instead of falling through to the inferred Claude-Code path.
_REPO_ROOT = Path(__file__).resolve().parents[3]
# In-repo analyzers runnable WITHOUT Docker: binary → (interpreter, script).
# ggoss-py needs only this Python's stdlib ``ast``; ggoss-ts runs under
# ``node`` (PR-153 extended) so a docker-free deploy still gets *verified*
# deterministic extraction for the two most common backend/frontend stacks
# instead of falling through to the slower, ``inferred`` Claude-Code path.
_INREPO_ANALYZERS: dict[str, tuple[str, Path]] = {
    "ggoss-py": (sys.executable,
                 _REPO_ROOT / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"),
    "ggoss-ts": ("node",
                 _REPO_ROOT / "analyzers" / "ggoss-ts" / "src" / "index.mjs"),
}


def _inrepo_entry(binary: str) -> tuple[str, Path] | None:
    """(resolved_interpreter, script) for a docker-free in-repo analyzer, or
    None when the path is disabled, the interpreter is missing (e.g. no
    ``node``), or the script is absent."""
    if os.environ.get("MNEMOS_INREPO_ANALYZERS") not in ("1", "true", "True"):
        return None
    entry = _INREPO_ANALYZERS.get(binary)
    if entry is None:
        return None
    interp, script = entry
    if not script.exists():
        return None
    resolved = interp if interp == sys.executable else shutil.which(interp)
    return (resolved, script) if resolved else None


def inrepo_command(binary: str) -> list[str] | None:
    """``[interpreter, <flags>, script]`` prefix for a docker-free in-repo
    analyzer."""
    entry = _inrepo_entry(binary)
    if entry is None:
        return None
    interp, script = entry
    cmd = [interp]
    if Path(interp).name.lower().startswith("node"):
        # node's default old-space (~2 GB) OOMs the TypeScript type-checker on
        # large repos; ggoss-ts's ``calls`` verb chunks the file set and forces
        # a GC between chunks (needs --expose-gc) so only one chunk's program is
        # resident, and the higher ceiling gives each chunk headroom. These
        # can't go through NODE_OPTIONS — _build_env strips it from the env.
        cmd.append("--max-old-space-size=6144")
        cmd.append("--expose-gc")
    cmd.append(str(script))
    return cmd


def inrepo_script(binary: str) -> Path | None:
    """The in-repo script path when the analyzer is runnable docker-free
    (the registry's availability check uses this)."""
    entry = _inrepo_entry(binary)
    return entry[1] if entry else None



# Subset of the platform's own env that an analyzer subprocess is
# allowed to see. Everything outside this set is stripped so a future
# config addition (Slack webhook URL, OIDC client secret, …) cannot
# leak into a binary we don't fully audit. Team B 2nd-round finding A.
_SAFE_HOST_ENV_KEYS: frozenset[str] = frozenset(
    {
        # Locale / linker / glibc — analyzers genuinely need these.
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        # Python and .NET runtime housekeeping (PYTHONPATH intentionally
        # omitted — we never want platform code to be importable by the
        # analyzer subprocess).
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "DOTNET_ROOT",
        # Mnemos-controlled passthroughs.
        "MNEMOS_DB_CONN",
        "MNEMOS_MAX_ROWS",
        "MNEMOS_REQUEST_ID",
    }
)


def _build_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Return the env dict an analyzer subprocess sees.

    Starts from the allowlisted host env, then layers caller-supplied
    keys on top. Anything not in :data:`_SAFE_HOST_ENV_KEYS` and not
    explicitly passed by the caller is dropped.
    """
    safe: dict[str, str] = {
        k: v for k, v in os.environ.items() if k in _SAFE_HOST_ENV_KEYS
    }
    if extra:
        safe.update(extra)
    return safe


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
        # Prefer the installed binary (production / docker). Fall back to the
        # in-repo Python source when it isn't on PATH (PR-153, docker-free).
        resolved_binary = shutil.which(self.binary)
        if resolved_binary is not None:
            args = [resolved_binary, verb, str(path)]
        else:
            cmd = inrepo_command(self.binary)
            args = (
                [*cmd, verb, str(path)]
                if cmd is not None
                else [self.binary, verb, str(path)]
            )
        if extra_args:
            args.extend(extra_args)

        # Log the binary + verb + path but never extra_args, since the
        # caller may put credentials there (DB live_schema --conn-ref).
        log.info("spawning analyzer: %s %s %s", self.binary, verb, str(path))

        proc_env = _build_env(env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=proc_env,
            )
        except FileNotFoundError as exc:
            # PR-98: graceful degradation — when the analyzer binary
            # isn't on PATH (docker image missing, Phase-1 deploy
            # without the analyzer extra image), yield a structured
            # recoverable error and exit 0-ish from the runner's
            # perspective. The orchestrator marks the stage skipped,
            # never crashes the whole run.
            yield RunRecord(
                stream="stderr",
                payload={
                    "level": "error",
                    "message": (
                        f"analyzer_binary_not_found: {self.binary!r} "
                        f"({exc.strerror or 'No such file or directory'}). "
                        "Install the analyzer image or remove the language "
                        "from the project's stage list."
                    ),
                    "recoverable": True,
                    "binary": self.binary,
                },
            )
            return

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
