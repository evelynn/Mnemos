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


# Analyzer output is intentionally bounded at the subprocess boundary.  The
# queue prevents a fast analyzer from making the platform retain an entire
# repository's JSONL in memory, while the stream limit is large enough for a
# single source symbol with rich metadata.  Records larger than this are a
# contract violation: analyzers should split them instead of asking the
# orchestrator to accept an unbounded line.
DEFAULT_QUEUE_MAXSIZE = 256
DEFAULT_MAX_RECORD_BYTES = 1024 * 1024
DEFAULT_STREAM_LIMIT_BYTES = DEFAULT_MAX_RECORD_BYTES + 64 * 1024
DEFAULT_TIMEOUT_S = 30 * 60.0
DEFAULT_TERMINATE_GRACE_S = 1.0


class AnalyzerError(RuntimeError):
    """Base class for explicit analyzer execution failures.

    ``partial_records`` is populated by :meth:`AnalyzerRunner.run_collect`.
    Streaming callers already received those records before the exception and
    therefore do not need the runner to retain another copy.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.partial_records: tuple[RunRecord, ...] = ()


class AnalyzerProcessError(AnalyzerError):
    """The analyzer process exited with a non-zero status."""

    def __init__(self, binary: str, exit_code: int) -> None:
        self.binary = binary
        self.exit_code = exit_code
        super().__init__(
            f"analyzer_process_failed: {binary!r} exited with code {exit_code}"
        )


class AnalyzerTimeoutError(AnalyzerError):
    """The analyzer exceeded its wall-clock execution budget."""

    def __init__(self, binary: str, timeout_s: float) -> None:
        self.binary = binary
        self.timeout_s = timeout_s
        super().__init__(
            f"analyzer_timeout: {binary!r} exceeded {timeout_s:g}s wall timeout"
        )


class AnalyzerOutputError(AnalyzerError):
    """The analyzer violated the bounded JSONL stream contract."""

    def __init__(self, binary: str, stream: str, detail: str) -> None:
        self.binary = binary
        self.stream = stream
        super().__init__(f"analyzer_output_error: {binary!r} {stream}: {detail}")


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
    # PR-191 — C/C++ (stdlib-only regex/brace scanner); closes the eval
    # doc's P0 "dominant language skipped as no_analyzer" gap.
    "ggoss-cpp": (sys.executable,
                  _REPO_ROOT / "analyzers" / "ggoss-cpp" / "src" / "ggoss_cpp.py"),
    # PR-192 — Java (same stdlib regex/brace approach); closes the web+Java
    # eval's empty-graph gap on Spring backends.
    "ggoss-java": (sys.executable,
                   _REPO_ROOT / "analyzers" / "ggoss-java" / "src" / "ggoss_java.py"),
    # PR-193 — web templates (HTML routes → contracts); stdlib regex.
    "ggoss-web": (sys.executable,
                  _REPO_ROOT / "analyzers" / "ggoss-web" / "src" / "ggoss_web.py"),
    # PR-194 — Kotlin (JVM web); stdlib regex + brace scanner.
    "ggoss-kotlin": (sys.executable,
                     _REPO_ROOT / "analyzers" / "ggoss-kotlin" / "src" / "ggoss_kotlin.py"),
    # PR-195 — tree-sitter multi-language (Go/Rust/Ruby PoC). Needs the
    # tree-sitter-language-pack package in the interpreter that runs it.
    "ggoss-treesitter": (sys.executable,
                         _REPO_ROOT / "analyzers" / "ggoss-treesitter" / "src"
                         / "ggoss_treesitter.py"),
}


def _inrepo_deps_ok(binary: str, script: Path) -> bool:
    """Whether an in-repo analyzer's non-stdlib dependencies are present.

    ggoss-ts is the only stdlib-exception among the docker-free analyzers: it
    imports the ``typescript`` npm package, so it needs ``node_modules`` built
    (``npm ci`` in ``analyzers/ggoss-ts``). Without it ``node`` crashes with
    ERR_MODULE_NOT_FOUND — an opaque failure. Gating availability here makes
    the orchestrator record a clean ``no_analyzer`` skip / agent fallback
    instead, and keeps the analyzer honest about needing setup. The stdlib
    analyzers (py/cpp/java/kotlin/web) have no such deps; ggoss-treesitter is
    gated separately in the registry on its Python package."""
    if binary == "ggoss-ts":
        # script = analyzers/ggoss-ts/src/index.mjs → node_modules is a sibling
        # of ``src``.
        return (script.parent.parent / "node_modules" / "typescript").exists()
    return True


def _inrepo_entry(binary: str) -> tuple[str, Path] | None:
    """(resolved_interpreter, script) for a docker-free in-repo analyzer, or
    None when the path is disabled, the interpreter is missing (e.g. no
    ``node``), the script is absent, or a required dependency isn't built."""
    if os.environ.get("MNEMOS_INREPO_ANALYZERS") not in ("1", "true", "True"):
        return None
    entry = _INREPO_ANALYZERS.get(binary)
    if entry is None:
        return None
    interp, script = entry
    if not script.exists():
        return None
    if not _inrepo_deps_ok(binary, script):
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


def _host_binary_prefix(binary: str) -> list[str] | None:
    """Resolve an installed binary into its host execution prefix.

    Windows ``CreateProcess`` does not honour Unix shebangs.  Supporting an
    explicitly configured Python script here keeps the analyzer contract
    portable and mirrors what POSIX does automatically.  We inspect only the
    first line and only for a path the caller already selected as its binary.
    """

    explicit = Path(binary).expanduser()
    resolved = str(explicit.resolve()) if explicit.is_file() else shutil.which(binary)
    if resolved is None:
        return None
    if os.name != "nt":
        return [resolved]

    path = Path(resolved)
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(256).lower()
    except OSError:
        first_line = b""
    if path.suffix.lower() in {".py", ".pyw"} or (
        first_line.startswith(b"#!") and b"python" in first_line
    ):
        return [sys.executable, resolved]
    return [resolved]



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


@dataclass(frozen=True)
class _Terminal:
    """Private queue marker emitted after both pipes have been drained."""

    error: AnalyzerError | None


async def _terminate_process(
    proc: asyncio.subprocess.Process,
    *,
    grace_s: float,
) -> None:
    """Terminate ``proc`` and escalate to kill after a short grace period.

    This helper is deliberately idempotent because timeout, cancellation and
    output-contract failure can race each other.  Waiting indefinitely in an
    async-generator ``finally`` block was the old cancellation bug: a caller
    could cancel analysis but still wait for the analyzer to exit naturally.
    """

    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
        return
    except TimeoutError:
        pass

    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


class AnalyzerRunner:
    """Spawns an analyzer binary (or docker-run wrapper) and yields records.

    The Phase-1 implementation assumes the binary is reachable on PATH inside
    the platform container. Docker-based isolation is introduced alongside the
    sandbox manager in Week 7.
    """

    def __init__(
        self,
        binary: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        stream_limit_bytes: int = DEFAULT_STREAM_LIMIT_BYTES,
        terminate_grace_s: float = DEFAULT_TERMINATE_GRACE_S,
    ):
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if queue_maxsize <= 0:
            raise ValueError("queue_maxsize must be > 0")
        if max_record_bytes <= 0:
            raise ValueError("max_record_bytes must be > 0")
        if stream_limit_bytes <= max_record_bytes:
            raise ValueError(
                "stream_limit_bytes must be greater than max_record_bytes"
            )
        if terminate_grace_s <= 0:
            raise ValueError("terminate_grace_s must be > 0")
        self.binary = binary
        # Resolution and the Windows shebang probe are invariant for the
        # lifetime of a runner.  Analyzer stages reuse one runner across verbs;
        # repeating synchronous path resolution/file reads before every spawn
        # serialized otherwise-concurrent launches and was measurable at scale.
        self._host_prefix = _host_binary_prefix(binary)
        self.timeout_s = float(timeout_s)
        self.queue_maxsize = queue_maxsize
        self.max_record_bytes = max_record_bytes
        self.stream_limit_bytes = stream_limit_bytes
        self.terminate_grace_s = float(terminate_grace_s)

    async def run(
        self,
        verb: str,
        path: str | Path,
        *,
        extra_args: list[str] | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[RunRecord]:
        wall_timeout_s = self.timeout_s if timeout_s is None else float(timeout_s)
        if wall_timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")

        # Prefer the installed binary (production / docker). Fall back to the
        # in-repo Python source when it isn't on PATH (PR-153, docker-free).
        host_prefix = self._host_prefix
        if host_prefix is not None:
            args = [*host_prefix, verb, str(path)]
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
                limit=self.stream_limit_bytes,
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

        queue: asyncio.Queue[RunRecord | _Terminal] = asyncio.Queue(
            maxsize=self.queue_maxsize
        )
        loop = asyncio.get_running_loop()
        failure_signal: asyncio.Future[AnalyzerError] = loop.create_future()

        def _signal_failure(error: AnalyzerError) -> None:
            if not failure_signal.done():
                failure_signal.set_result(error)

        async def _drain(stream: asyncio.StreamReader | None, tag: str) -> None:
            if stream is None:
                return
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        return
                    if len(line) > self.max_record_bytes:
                        raise AnalyzerOutputError(
                            self.binary,
                            tag,
                            (
                                f"JSONL record exceeds "
                                f"{self.max_record_bytes} byte limit"
                            ),
                        )
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        payload = {"raw": text, "parse_error": True}
                    if not isinstance(payload, dict):
                        payload = {
                            "raw": text,
                            "parse_error": True,
                            "message": "JSONL record must be an object",
                        }
                    await queue.put(RunRecord(stream=tag, payload=payload))
            except asyncio.CancelledError:
                raise
            except AnalyzerError as exc:
                _signal_failure(exc)
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # StreamReader.readline raises ValueError when its configured
                # limit is crossed.  Convert it into the same explicit,
                # bounded-output failure instead of losing the sentinel and
                # leaving the consumer blocked forever.
                _signal_failure(
                    AnalyzerOutputError(
                        self.binary,
                        tag,
                        f"JSONL record exceeded stream limit ({exc})",
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive pipe IO
                _signal_failure(
                    AnalyzerOutputError(
                        self.binary,
                        tag,
                        f"stream read failed: {type(exc).__name__}: {exc}",
                    )
                )

        stdout_task = asyncio.create_task(_drain(proc.stdout, "stdout"))
        stderr_task = asyncio.create_task(_drain(proc.stderr, "stderr"))

        async def _wait_for_completion() -> int:
            exit_code = await proc.wait()
            await asyncio.gather(stdout_task, stderr_task)
            return exit_code

        completion_task = asyncio.create_task(_wait_for_completion())

        async def _watch_timeout() -> None:
            await asyncio.sleep(wall_timeout_s)
            if proc.returncode is None:
                _signal_failure(
                    AnalyzerTimeoutError(self.binary, wall_timeout_s)
                )

        watchdog_task = asyncio.create_task(_watch_timeout())

        async def _coordinate() -> None:
            error: AnalyzerError | None = None
            try:
                await asyncio.wait(
                    {completion_task, failure_signal},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # A drain failure or wall timeout wins races with process
                # completion: otherwise a killed process could be misreported
                # merely as a non-zero exit and hide the actual cause.
                if failure_signal.done():
                    error = failure_signal.result()
                    await _terminate_process(
                        proc, grace_s=self.terminate_grace_s
                    )
                    for task in (stdout_task, stderr_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(
                        stdout_task, stderr_task, return_exceptions=True
                    )
                    if not completion_task.done():
                        await completion_task
                else:
                    exit_code = completion_task.result()
                    if exit_code != 0:
                        error = AnalyzerProcessError(self.binary, exit_code)
            finally:
                watchdog_task.cancel()
                await asyncio.gather(watchdog_task, return_exceptions=True)

            await queue.put(_Terminal(error=error))

        coordinator_task = asyncio.create_task(_coordinate())

        try:
            while True:
                item = await queue.get()
                if isinstance(item, _Terminal):
                    if item.error is not None:
                        raise item.error
                    break
                yield item
        finally:
            # Cancellation or an early-closing consumer must never wait for a
            # naturally exiting analyzer.  Stop it first, then tear down all
            # bookkeeping tasks.  The short grace handles cooperative POSIX
            # exits; Windows terminate is immediate.
            if proc.returncode is None:
                await _terminate_process(proc, grace_s=self.terminate_grace_s)

            for task in (
                stdout_task,
                stderr_task,
                completion_task,
                watchdog_task,
                coordinator_task,
            ):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                stdout_task,
                stderr_task,
                completion_task,
                watchdog_task,
                coordinator_task,
                return_exceptions=True,
            )

    async def run_collect(self, verb: str, path: str | Path) -> list[RunRecord]:
        records: list[RunRecord] = []
        try:
            async for rec in self.run(verb, path):
                records.append(rec)
        except AnalyzerError as exc:
            exc.partial_records = tuple(records)
            raise
        return records
