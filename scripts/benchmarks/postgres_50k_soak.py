"""Deterministic 50K-file PostgreSQL graph-publication soak.

This is an evidence script, not a product code path.  It exercises the real
``AnalyzerRunner`` JSONL boundary and the production staging/seal/promotion
functions inside a disposable PostgreSQL schema.  The controller never keeps
the analyzer output or the generated file list in memory.

The generated corpus intentionally contains one plain function per file.  The
soak therefore measures the deterministic Python symbol/index publication
component; it is not evidence for optional LLM providers or every ggoss-py
verb.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import aclosing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
DEFAULT_FILE_COUNT = 50_000
DEFAULT_STAGE_BATCH_SIZE = 250
MAX_STAGE_BATCH_SIZE = 250
DEFAULT_ANALYZER_QUEUE_SIZE = 64
DEFAULT_ANALYZER_TIMEOUT_SECONDS = 3_600.0
FILES_PER_DIRECTORY = 500
SOURCE_NAME = "ggoss-py"
_TEMP_ROOT_RE = re.compile(r"^mnemos-e4-50k-[a-z0-9_-]+$")
_SHARD_RE = re.compile(r"^shard_\d{4}$")
DEFAULT_CLEANUP_WORKERS = 12


@dataclass(frozen=True)
class GeneratedCorpus:
    files: int
    directories: int
    source_bytes: int
    wall_seconds: float
    content_fingerprint: str


@dataclass(frozen=True)
class StagePassMetrics:
    stdout_records: int
    accepted_records: int
    invalid_records: int
    stderr_records: int
    staged_candidates: int
    stage_rows_changed: int
    stage_batches: int
    max_buffered_candidates: int
    analyzer_queue_maxsize: int
    analyzer_stage_wall_seconds: float
    stage_db_wall_seconds: float
    records_per_second: float


def _round_seconds(value: float) -> float:
    return round(value, 6)


def _normalize_postgres_url(raw: str) -> str:
    value = raw.strip()
    if value.startswith("postgres://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgresql://")
    if value.startswith("postgresql+asyncpg://"):
        return value
    if value.startswith("postgresql+") and "://" in value:
        return "postgresql+asyncpg://" + value.split("://", 1)[1]
    raise ValueError("the soak requires a PostgreSQL URL")


def _generate_source_tree(root: Path, file_count: int) -> GeneratedCorpus:
    """Write ``file_count`` real, parseable files without building a path list."""

    if file_count <= 0:
        raise ValueError("file_count must be positive")
    started = time.perf_counter()
    root.mkdir(parents=True, exist_ok=False)
    digest = hashlib.sha256()
    source_bytes = 0
    directory_count = math.ceil(file_count / FILES_PER_DIRECTORY)
    current_directory: Path | None = None
    for index in range(file_count):
        if index % FILES_PER_DIRECTORY == 0:
            current_directory = root / f"shard_{index // FILES_PER_DIRECTORY:04d}"
            current_directory.mkdir()
        assert current_directory is not None
        relative = (
            f"{current_directory.name}/module_{index:06d}.py"
        )
        source = f"def symbol_{index:06d}():\n    return {index}\n"
        encoded = source.encode("utf-8")
        (current_directory / f"module_{index:06d}.py").write_text(
            source,
            encoding="utf-8",
            newline="\n",
        )
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(encoded)
        source_bytes += len(encoded)
    return GeneratedCorpus(
        files=file_count,
        directories=directory_count,
        source_bytes=source_bytes,
        wall_seconds=_round_seconds(time.perf_counter() - started),
        content_fingerprint=digest.hexdigest(),
    )


def _validated_generated_temp_root(root: Path) -> Path:
    """Accept only one direct child of the OS temp directory owned by this soak."""

    resolved = root.resolve(strict=True)
    system_temp = Path(tempfile.gettempdir()).resolve(strict=True)
    if resolved.parent != system_temp:
        raise ValueError("benchmark cleanup root must be a direct OS-temp child")
    if _TEMP_ROOT_RE.fullmatch(resolved.name) is None:
        raise ValueError("benchmark cleanup root has an unexpected name")
    return resolved


def _rmtree_with_retry(path: Path, *, attempts: int = 8) -> None:
    """Remove one generated tree, retrying transient Windows/AV sharing errors."""

    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(0.05 * (2**attempt), 1.0))
    raise AssertionError("unreachable cleanup retry state")


def _parallel_cleanup_temp_root(
    root: Path,
    *,
    max_workers: int = DEFAULT_CLEANUP_WORKERS,
) -> None:
    """Delete verified generated shards concurrently, then remove their root.

    Shards are independent directories, so this keeps one owner for cleanup
    while avoiding the pathological serial-delete time observed on Windows.
    Unknown files, directories, and symlinks fail closed instead of widening a
    recursive delete target.
    """

    if max_workers <= 0:
        raise ValueError("cleanup max_workers must be positive")
    resolved = _validated_generated_temp_root(root)
    entries = list(resolved.iterdir())
    if not entries:
        resolved.rmdir()
        return
    if len(entries) != 1 or entries[0].name != "source":
        raise ValueError("benchmark temp root contains an unexpected entry")
    source = entries[0]
    if source.is_symlink() or not source.is_dir():
        raise ValueError("benchmark source root must be a real directory")

    shards: list[Path] = []
    for child in source.iterdir():
        if (
            child.is_symlink()
            or not child.is_dir()
            or _SHARD_RE.fullmatch(child.name) is None
        ):
            raise ValueError("benchmark source root contains an unexpected entry")
        shards.append(child)
    shards.sort(key=lambda path: path.name)

    if shards:
        workers = min(max_workers, len(shards))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_rmtree_with_retry, shard) for shard in shards]
            for future in concurrent.futures.as_completed(futures):
                future.result()
    _rmtree_with_retry(source)
    _rmtree_with_retry(resolved)
    if resolved.exists():
        raise RuntimeError("verified benchmark temp root remains after cleanup")


class _WindowsRSSProbe:
    """Sample this process plus direct children with the Windows process API."""

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        handle = ctypes.c_void_p
        self.kernel32.GetCurrentProcess.restype = handle
        self.kernel32.CreateToolhelp32Snapshot.argtypes = [
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        self.kernel32.CreateToolhelp32Snapshot.restype = handle
        self.kernel32.Process32FirstW.argtypes = [
            handle,
            ctypes.POINTER(self.PROCESSENTRY32W),
        ]
        self.kernel32.Process32FirstW.restype = ctypes.c_int
        self.kernel32.Process32NextW.argtypes = [
            handle,
            ctypes.POINTER(self.PROCESSENTRY32W),
        ]
        self.kernel32.Process32NextW.restype = ctypes.c_int
        self.kernel32.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self.kernel32.OpenProcess.restype = handle
        self.kernel32.CloseHandle.argtypes = [handle]
        self.kernel32.CloseHandle.restype = ctypes.c_int
        self.psapi.GetProcessMemoryInfo.argtypes = [
            handle,
            ctypes.POINTER(self.PROCESS_MEMORY_COUNTERS_EX),
            ctypes.c_ulong,
        ]
        self.psapi.GetProcessMemoryInfo.restype = ctypes.c_int

    def _memory(self, handle: int | None) -> tuple[int, int]:
        if not handle:
            return 0, 0
        counters = self.PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        ok = self.psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(counters),
            counters.cb,
        )
        if not ok:
            return 0, 0
        return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)

    def sample(self) -> tuple[int, int, int]:
        current_handle = self.kernel32.GetCurrentProcess()
        controller_rss, controller_peak = self._memory(current_handle)
        snapshot = self.kernel32.CreateToolhelp32Snapshot(
            self.TH32CS_SNAPPROCESS,
            0,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if not snapshot or snapshot == invalid_handle:
            return controller_peak, controller_rss, 0
        child_rss = 0
        child_count = 0
        entry = self.PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            has_entry = bool(
                self.kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            )
            while has_entry:
                if int(entry.th32ParentProcessID) == os.getpid():
                    child_handle = self.kernel32.OpenProcess(
                        self.PROCESS_QUERY_INFORMATION | self.PROCESS_VM_READ,
                        0,
                        entry.th32ProcessID,
                    )
                    if child_handle:
                        try:
                            working_set, _ = self._memory(child_handle)
                            child_rss += working_set
                            child_count += 1
                        finally:
                            self.kernel32.CloseHandle(child_handle)
                has_entry = bool(
                    self.kernel32.Process32NextW(snapshot, ctypes.byref(entry))
                )
        finally:
            self.kernel32.CloseHandle(snapshot)
        return controller_peak, controller_rss + child_rss, child_count


class PeakRSSMonitor:
    """Best-effort sampled process-tree working-set high-water mark."""

    def __init__(self, interval_seconds: float = 0.25) -> None:
        self.interval_seconds = interval_seconds
        self.controller_peak_bytes = 0
        self.process_tree_peak_bytes = 0
        self.max_direct_children = 0
        self.samples = 0
        self.method = "unsupported"
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._windows = _WindowsRSSProbe() if os.name == "nt" else None
        if self._windows is not None:
            self.method = "windows_psapi_sampled_controller_plus_direct_children"
        elif Path("/proc/self/status").exists():
            self.method = "linux_proc_sampled_controller"

    @staticmethod
    def _linux_current_rss() -> int:
        try:
            for line in Path("/proc/self/status").read_text(
                encoding="utf-8"
            ).splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return 0
        return 0

    def _sample(self) -> None:
        if self._windows is not None:
            controller_peak, tree_rss, child_count = self._windows.sample()
            self.controller_peak_bytes = max(
                self.controller_peak_bytes,
                controller_peak,
            )
            self.process_tree_peak_bytes = max(
                self.process_tree_peak_bytes,
                tree_rss,
            )
            self.max_direct_children = max(self.max_direct_children, child_count)
        else:
            rss = self._linux_current_rss()
            self.controller_peak_bytes = max(self.controller_peak_bytes, rss)
            self.process_tree_peak_bytes = max(self.process_tree_peak_bytes, rss)
        self.samples += 1

    async def _run(self) -> None:
        while not self._stopping:
            self._sample()
            await asyncio.sleep(self.interval_seconds)
        self._sample()

    async def __aenter__(self) -> PeakRSSMonitor:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._stopping = True
        if self._task is not None:
            await self._task

    def payload(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "sample_interval_seconds": self.interval_seconds,
            "samples": self.samples,
            "controller_peak_rss_bytes": self.controller_peak_bytes,
            "process_tree_peak_rss_bytes": self.process_tree_peak_bytes,
            "max_direct_children": self.max_direct_children,
            "scope_note": (
                "process-tree RSS is sampled and includes the benchmark "
                "controller plus direct analyzer children"
            ),
        }


def _load_app_dependencies(database_url: str) -> dict[str, Any]:
    """Import the server only after the standalone benchmark config is safe."""

    os.environ["MNEMOS_ENV"] = "test"
    os.environ["MNEMOS_INREPO_ANALYZERS"] = "1"
    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault(
        "SECRET_KEY",
        "mnemos-e4-soak-only-secret-key-not-for-production",
    )
    if str(SERVER_ROOT) not in sys.path:
        sys.path.insert(0, str(SERVER_ROOT))

    import app.models.all_models  # noqa: F401
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.analyzers.runner import AnalyzerRunner
    from app.graph_publication import (
        bootstrap_graph_head,
        capture_graph_base_generation,
        promote_staged_graph,
        seal_graph_coverage,
        validate_graph_publication_receipt,
    )
    from app.models import Base
    from app.models.findings import LLMCall
    from app.models.graph import (
        AnalysisRun,
        Edge,
        GraphEdgeStage,
        GraphHead,
        GraphNodeStage,
        Node,
    )
    from app.models.projects import Project
    from app.orchestrator.jobs import _GraphStageBuffer, _record_payload

    return {
        "func": func,
        "select": select,
        "text": text,
        "async_sessionmaker": async_sessionmaker,
        "create_async_engine": create_async_engine,
        "AnalyzerRunner": AnalyzerRunner,
        "bootstrap_graph_head": bootstrap_graph_head,
        "capture_graph_base_generation": capture_graph_base_generation,
        "promote_staged_graph": promote_staged_graph,
        "seal_graph_coverage": seal_graph_coverage,
        "validate_graph_publication_receipt": (
            validate_graph_publication_receipt
        ),
        "Base": Base,
        "LLMCall": LLMCall,
        "AnalysisRun": AnalysisRun,
        "Edge": Edge,
        "GraphEdgeStage": GraphEdgeStage,
        "GraphHead": GraphHead,
        "GraphNodeStage": GraphNodeStage,
        "Node": Node,
        "Project": Project,
        "_GraphStageBuffer": _GraphStageBuffer,
        "_record_payload": _record_payload,
    }


async def _prepare_run(
    deps: dict[str, Any],
    session_factory: Any,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    bootstrap: bool,
    content_fingerprint: str,
) -> int:
    AnalysisRun = deps["AnalysisRun"]
    async with session_factory() as session:
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="running",
                triggered_by="e4-postgres-50k-soak",
                git_sha=content_fingerprint,
                scope="full",
                started_at=datetime.now(tz=timezone.utc),
                stats={"benchmark": "postgres_50k_soak"},
            )
        )
        await session.flush()
        if bootstrap:
            await deps["bootstrap_graph_head"](
                session,
                project_id=project_id,
            )
        generation = await deps["capture_graph_base_generation"](
            session,
            project_id=project_id,
            run_id=run_id,
        )
        await session.commit()
    return int(generation)


async def _stream_analyze_and_stage(
    deps: dict[str, Any],
    session_factory: Any,
    *,
    source_root: Path,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    batch_size: int,
    analyzer_timeout_seconds: float,
) -> StagePassMetrics:
    """Consume one bounded AnalyzerRunner stream into production stage batches."""

    AnalyzerRunner = deps["AnalyzerRunner"]
    GraphStageBuffer = deps["_GraphStageBuffer"]
    record_payload = deps["_record_payload"]
    runner = AnalyzerRunner(
        SOURCE_NAME,
        timeout_s=analyzer_timeout_seconds,
        queue_maxsize=DEFAULT_ANALYZER_QUEUE_SIZE,
    )
    totals = {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0}
    stdout_records = 0
    accepted_records = 0
    invalid_records = 0
    stderr_records = 0
    staged_candidates = 0
    rows_changed = 0
    batches = 0
    max_buffered = 0
    stage_db_seconds = 0.0
    buffer = GraphStageBuffer(project_id=project_id, run_id=run_id)
    started = time.perf_counter()

    async with session_factory() as session:
        stream = runner.run(
            "symbols",
            source_root,
            env={"MNEMOS_PROJECT_ID": str(project_id)},
        )
        async with aclosing(stream):
            async for record in stream:
                if record.stream == "stderr":
                    stderr_records += 1
                    continue
                stdout_records += 1
                accepted = await record_payload(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    payload=record.payload,
                    accept_kinds={"symbol"},
                    totals=totals,
                    source_root=source_root,
                    expected_source_name=SOURCE_NAME,
                    stage_buffer=buffer,
                )
                if accepted:
                    accepted_records += 1
                else:
                    invalid_records += 1
                max_buffered = max(max_buffered, buffer.size)
                if buffer.size >= batch_size:
                    db_started = time.perf_counter()
                    buffered, changed = await buffer.flush(session)
                    await session.commit()
                    stage_db_seconds += time.perf_counter() - db_started
                    staged_candidates += buffered
                    rows_changed += changed
                    batches += 1

        if buffer.size:
            db_started = time.perf_counter()
            buffered, changed = await buffer.flush(session)
            await session.commit()
            stage_db_seconds += time.perf_counter() - db_started
            staged_candidates += buffered
            rows_changed += changed
            batches += 1

    wall_seconds = time.perf_counter() - started
    if stderr_records or invalid_records:
        raise RuntimeError(
            "synthetic analyzer stream was not authoritative: "
            f"stderr={stderr_records}, invalid={invalid_records}"
        )
    if totals["symbols"] != accepted_records:
        raise RuntimeError("accepted analyzer record accounting drifted")
    return StagePassMetrics(
        stdout_records=stdout_records,
        accepted_records=accepted_records,
        invalid_records=invalid_records,
        stderr_records=stderr_records,
        staged_candidates=staged_candidates,
        stage_rows_changed=rows_changed,
        stage_batches=batches,
        max_buffered_candidates=max_buffered,
        analyzer_queue_maxsize=DEFAULT_ANALYZER_QUEUE_SIZE,
        analyzer_stage_wall_seconds=_round_seconds(wall_seconds),
        stage_db_wall_seconds=_round_seconds(stage_db_seconds),
        records_per_second=round(accepted_records / wall_seconds, 3),
    )


async def _stage_counts(
    deps: dict[str, Any],
    session_factory: Any,
    run_id: uuid.UUID,
) -> dict[str, int]:
    select = deps["select"]
    func = deps["func"]
    GraphNodeStage = deps["GraphNodeStage"]
    GraphEdgeStage = deps["GraphEdgeStage"]
    async with session_factory() as session:
        nodes = (
            await session.execute(
                select(func.count()).select_from(GraphNodeStage).where(
                    GraphNodeStage.run_id == run_id
                )
            )
        ).scalar_one()
        edges = (
            await session.execute(
                select(func.count()).select_from(GraphEdgeStage).where(
                    GraphEdgeStage.run_id == run_id
                )
            )
        ).scalar_one()
    return {"nodes": int(nodes), "edges": int(edges)}


async def _graph_counts(
    deps: dict[str, Any],
    session_factory: Any,
    project_id: uuid.UUID,
) -> dict[str, int]:
    select = deps["select"]
    func = deps["func"]
    Node = deps["Node"]
    Edge = deps["Edge"]
    async with session_factory() as session:
        total_nodes = (
            await session.execute(
                select(func.count()).select_from(Node).where(
                    Node.project_id == project_id
                )
            )
        ).scalar_one()
        current_nodes = (
            await session.execute(
                select(func.count()).select_from(Node).where(
                    Node.project_id == project_id,
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one()
        total_edges = (
            await session.execute(
                select(func.count()).select_from(Edge).where(
                    Edge.project_id == project_id
                )
            )
        ).scalar_one()
        current_edges = (
            await session.execute(
                select(func.count()).select_from(Edge).where(
                    Edge.project_id == project_id,
                    Edge.valid_to.is_(None),
                )
            )
        ).scalar_one()
    return {
        "total_nodes": int(total_nodes),
        "current_nodes": int(current_nodes),
        "total_edges": int(total_edges),
        "current_edges": int(current_edges),
    }


async def _llm_usage(
    deps: dict[str, Any],
    session_factory: Any,
    project_id: uuid.UUID,
) -> dict[str, int]:
    select = deps["select"]
    func = deps["func"]
    LLMCall = deps["LLMCall"]
    token_columns = {
        "legacy_tokens": LLMCall.tokens_used,
        "input_tokens": LLMCall.input_tokens,
        "output_tokens": LLMCall.output_tokens,
        "provider_total_tokens": LLMCall.provider_total_tokens,
    }
    expressions = [func.count(LLMCall.id)]
    expressions.extend(
        func.coalesce(func.sum(func.coalesce(column, 0)), 0)
        for column in token_columns.values()
    )
    async with session_factory() as session:
        row = (
            await session.execute(
                select(*expressions).where(LLMCall.project_id == project_id)
            )
        ).one()
    result = {"calls": int(row[0])}
    for index, name in enumerate(token_columns, start=1):
        result[name] = int(row[index])
    result["all_token_fields_zero"] = int(
        all(value == 0 for name, value in result.items() if name != "calls")
    )
    return result


async def _publication_receipt(
    deps: dict[str, Any],
    session_factory: Any,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict[str, Any]:
    AnalysisRun = deps["AnalysisRun"]
    GraphHead = deps["GraphHead"]
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
        if run is None or head is None:
            raise RuntimeError("publication run/head disappeared")
        receipt = deps["validate_graph_publication_receipt"](run, head=head)
    return {
        "generation": receipt.generation,
        "base_generation": receipt.base_generation,
        "authoritative_sources": list(receipt.authoritative_sources),
        "atomic": True,
        "counts": asdict(receipt.counts),
    }


async def _promote(
    deps: dict[str, Any],
    session_factory: Any,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    allow_full_rebuild: bool,
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    async with session_factory() as session:
        result = await deps["promote_staged_graph"](
            session,
            project_id=project_id,
            run_id=run_id,
            allow_full_rebuild=allow_full_rebuild,
        )
    return (
        {
            "generation": result.generation,
            "idempotent_retry": result.idempotent,
            "counts": asdict(result.counts),
        },
        _round_seconds(time.perf_counter() - started),
    )


def _assert_zero_llm(usage: dict[str, int]) -> None:
    if usage["calls"] != 0 or usage["all_token_fields_zero"] != 1:
        raise AssertionError(f"deterministic soak created LLM usage: {usage}")


def _assert_pass_contract(
    metrics: StagePassMetrics,
    stage_counts: dict[str, int],
    file_count: int,
    batch_size: int,
) -> None:
    if metrics.accepted_records != file_count:
        raise AssertionError(
            f"expected {file_count} accepted symbols, got {metrics.accepted_records}"
        )
    if metrics.stdout_records != file_count:
        raise AssertionError("analyzer stdout row count did not match the corpus")
    if metrics.staged_candidates != file_count:
        raise AssertionError("staged candidate count did not match the corpus")
    if stage_counts != {"nodes": file_count, "edges": 0}:
        raise AssertionError(f"unexpected private stage counts: {stage_counts}")
    if metrics.max_buffered_candidates > batch_size:
        raise AssertionError("stage candidate buffer exceeded its configured bound")


def _assert_first_publication(
    result: dict[str, Any],
    graph_counts: dict[str, int],
    file_count: int,
) -> None:
    counts = result["counts"]
    expected_zero = {
        key
        for key in counts
        if key not in {"nodes_inserted"}
    }
    if counts["nodes_inserted"] != file_count or any(
        counts[key] != 0 for key in expected_zero
    ):
        raise AssertionError(f"unexpected initial promotion counts: {counts}")
    expected_graph = {
        "total_nodes": file_count,
        "current_nodes": file_count,
        "total_edges": 0,
        "current_edges": 0,
    }
    if graph_counts != expected_graph:
        raise AssertionError(f"unexpected initial graph counts: {graph_counts}")


def _assert_noop_refresh(
    result: dict[str, Any],
    graph_counts: dict[str, int],
    file_count: int,
) -> None:
    counts = result["counts"]
    if counts["nodes_unchanged"] != file_count:
        raise AssertionError(f"refresh did not preserve all symbols: {counts}")
    if any(
        value != 0
        for name, value in counts.items()
        if name != "nodes_unchanged"
    ):
        raise AssertionError(f"same-content refresh wrote graph versions: {counts}")
    expected_graph = {
        "total_nodes": file_count,
        "current_nodes": file_count,
        "total_edges": 0,
        "current_edges": 0,
    }
    if graph_counts != expected_graph:
        raise AssertionError(
            f"same-content refresh changed bitemporal row counts: {graph_counts}"
        )


async def _run_in_schema(
    database_url: str,
    *,
    source_root: Path,
    corpus: GeneratedCorpus,
    batch_size: int,
    analyzer_timeout_seconds: float,
) -> dict[str, Any]:
    deps = _load_app_dependencies(database_url)
    create_async_engine = deps["create_async_engine"]
    async_sessionmaker = deps["async_sessionmaker"]
    text = deps["text"]
    Base = deps["Base"]
    Project = deps["Project"]

    root_engine = create_async_engine(database_url, pool_pre_ping=True)
    schema = f"mnemos_e4_50k_{uuid.uuid4().hex}"
    schema_engine = None
    schema_dropped = False
    result: dict[str, Any] = {
        "contract": "mnemos.postgres_50k_soak.v1",
        "status": "running",
        "coverage_scope": (
            "deterministic ggoss-py symbols and PostgreSQL graph publication; "
            "no optional LLM provider and no non-symbol analyzer verbs"
        ),
        "private_schema": {"name": schema, "dropped": False},
    }
    setup_started = time.perf_counter()
    try:
        async with root_engine.begin() as connection:
            server_row = (
                await connection.execute(
                    text(
                        "SELECT current_database(), current_setting('server_version'), "
                        "current_setting('server_version_num')"
                    )
                )
            ).one()
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_engine = root_engine.execution_options(
            schema_translate_map={None: schema}
        )
        async with schema_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(schema_engine, expire_on_commit=False)
        result["postgres"] = {
            "database": str(server_row[0]),
            "version": str(server_row[1]),
            "version_num": int(server_row[2]),
            "private_schema": True,
        }
        result["schema_setup_wall_seconds"] = _round_seconds(
            time.perf_counter() - setup_started
        )

        project_id = uuid.uuid4()
        first_run_id = uuid.uuid4()
        refresh_run_id = uuid.uuid4()
        async with session_factory() as session:
            session.add(
                Project(
                    id=project_id,
                    name="Mnemos E4 50K deterministic soak",
                    gitlab_project_id=1,
                    gitlab_url="https://example.invalid/mnemos-e4-50k",
                    default_branch="main",
                    languages=["python"],
                )
            )
            await session.commit()

        initial_generation = await _prepare_run(
            deps,
            session_factory,
            project_id=project_id,
            run_id=first_run_id,
            bootstrap=True,
            content_fingerprint=corpus.content_fingerprint,
        )
        if initial_generation != 0:
            raise AssertionError("fresh private schema did not start at generation zero")

        llm_before = await _llm_usage(deps, session_factory, project_id)
        _assert_zero_llm(llm_before)
        graph_before = await _graph_counts(deps, session_factory, project_id)
        if any(graph_before.values()):
            raise AssertionError("staging target was not an empty private graph")

        first_metrics = await _stream_analyze_and_stage(
            deps,
            session_factory,
            source_root=source_root,
            project_id=project_id,
            run_id=first_run_id,
            batch_size=batch_size,
            analyzer_timeout_seconds=analyzer_timeout_seconds,
        )
        first_stage_counts = await _stage_counts(
            deps,
            session_factory,
            first_run_id,
        )
        _assert_pass_contract(
            first_metrics,
            first_stage_counts,
            corpus.files,
            batch_size,
        )
        graph_while_staged = await _graph_counts(deps, session_factory, project_id)
        if graph_while_staged != graph_before:
            raise AssertionError("staged facts leaked into the published graph")

        async with session_factory() as session:
            sealed = await deps["seal_graph_coverage"](
                session,
                project_id=project_id,
                run_id=first_run_id,
                authoritative_sources={SOURCE_NAME},
            )
            await session.commit()
        if sealed != (SOURCE_NAME,):
            raise AssertionError("coverage seal did not preserve producer identity")

        first_promotion, first_promotion_wall = await _promote(
            deps,
            session_factory,
            project_id=project_id,
            run_id=first_run_id,
            allow_full_rebuild=True,
        )
        first_graph_counts = await _graph_counts(deps, session_factory, project_id)
        first_stage_after = await _stage_counts(
            deps,
            session_factory,
            first_run_id,
        )
        if first_stage_after != {"nodes": 0, "edges": 0}:
            raise AssertionError("successful promotion did not clean private staging")
        _assert_first_publication(
            first_promotion,
            first_graph_counts,
            corpus.files,
        )
        first_receipt = await _publication_receipt(
            deps,
            session_factory,
            project_id=project_id,
            run_id=first_run_id,
        )

        refresh_started = time.perf_counter()
        refresh_base_generation = await _prepare_run(
            deps,
            session_factory,
            project_id=project_id,
            run_id=refresh_run_id,
            bootstrap=False,
            content_fingerprint=corpus.content_fingerprint,
        )
        if refresh_base_generation != 1:
            raise AssertionError("same-content refresh did not capture generation one")
        refresh_metrics = await _stream_analyze_and_stage(
            deps,
            session_factory,
            source_root=source_root,
            project_id=project_id,
            run_id=refresh_run_id,
            batch_size=batch_size,
            analyzer_timeout_seconds=analyzer_timeout_seconds,
        )
        refresh_stage_counts = await _stage_counts(
            deps,
            session_factory,
            refresh_run_id,
        )
        _assert_pass_contract(
            refresh_metrics,
            refresh_stage_counts,
            corpus.files,
            batch_size,
        )
        graph_before_refresh_promotion = await _graph_counts(
            deps,
            session_factory,
            project_id,
        )
        if graph_before_refresh_promotion != first_graph_counts:
            raise AssertionError("refresh staging mutated the published generation")
        async with session_factory() as session:
            sealed_refresh = await deps["seal_graph_coverage"](
                session,
                project_id=project_id,
                run_id=refresh_run_id,
                authoritative_sources={SOURCE_NAME},
            )
            await session.commit()
        if sealed_refresh != (SOURCE_NAME,):
            raise AssertionError("refresh coverage seal drifted")
        refresh_promotion, refresh_promotion_wall = await _promote(
            deps,
            session_factory,
            project_id=project_id,
            run_id=refresh_run_id,
            allow_full_rebuild=False,
        )
        refresh_wall = time.perf_counter() - refresh_started
        refresh_graph_counts = await _graph_counts(
            deps,
            session_factory,
            project_id,
        )
        refresh_stage_after = await _stage_counts(
            deps,
            session_factory,
            refresh_run_id,
        )
        if refresh_stage_after != {"nodes": 0, "edges": 0}:
            raise AssertionError("refresh promotion did not clean private staging")
        _assert_noop_refresh(
            refresh_promotion,
            refresh_graph_counts,
            corpus.files,
        )
        refresh_receipt = await _publication_receipt(
            deps,
            session_factory,
            project_id=project_id,
            run_id=refresh_run_id,
        )

        llm_after = await _llm_usage(deps, session_factory, project_id)
        _assert_zero_llm(llm_after)
        result.update(
            {
                "status": "passed",
                "corpus": asdict(corpus),
                "bounds": {
                    "stage_batch_size": batch_size,
                    "analyzer_queue_maxsize": DEFAULT_ANALYZER_QUEUE_SIZE,
                    "max_buffered_candidates_observed": max(
                        first_metrics.max_buffered_candidates,
                        refresh_metrics.max_buffered_candidates,
                    ),
                    "records_collected_in_memory": 0,
                },
                "initial": {
                    "base_generation": initial_generation,
                    "analyzer_stage": asdict(first_metrics),
                    "stage_rows_before_promotion": first_stage_counts,
                    "published_rows_before_promotion": graph_while_staged,
                    "promotion_wall_seconds": first_promotion_wall,
                    "promotion": first_promotion,
                    "receipt": first_receipt,
                    "graph_rows_after_promotion": first_graph_counts,
                },
                "same_content_refresh": {
                    "base_generation": refresh_base_generation,
                    "analyzer_stage": asdict(refresh_metrics),
                    "stage_rows_before_promotion": refresh_stage_counts,
                    "published_rows_before_promotion": (
                        graph_before_refresh_promotion
                    ),
                    "promotion_wall_seconds": refresh_promotion_wall,
                    "total_refresh_wall_seconds": _round_seconds(refresh_wall),
                    "promotion": refresh_promotion,
                    "receipt": refresh_receipt,
                    "graph_rows_after_promotion": refresh_graph_counts,
                    "semantic_noop": True,
                },
                "llm_usage": {
                    "before": llm_before,
                    "after": llm_after,
                    "default_path_zero_calls_and_tokens": True,
                },
                "limitations": [
                    "The corpus exercises one Python symbol per file; calls, "
                    "contracts, data-access, mixed-language, Git checkout, Redis, "
                    "HTTP/MCP, and optional LLM paths are outside this component soak.",
                    "The coverage seal is justified by the generator invariant "
                    "(function declarations only), not by a signed analyzer terminal "
                    "coverage record; that terminal record is a documented Phase-2 gap.",
                    "RSS is sampled rather than a kernel job-object accounting total.",
                ],
            }
        )
        return result
    finally:
        if schema_engine is not None:
            await schema_engine.dispose()
        try:
            async with root_engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
                remains = (
                    await connection.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_namespace "
                            "WHERE nspname = :schema)"
                        ),
                        {"schema": schema},
                    )
                ).scalar_one()
            schema_dropped = not bool(remains)
            result["private_schema"]["dropped"] = schema_dropped
        finally:
            await root_engine.dispose()


async def run_soak(
    database_url: str,
    *,
    file_count: int = DEFAULT_FILE_COUNT,
    batch_size: int = DEFAULT_STAGE_BATCH_SIZE,
    analyzer_timeout_seconds: float = DEFAULT_ANALYZER_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if not 1 <= batch_size <= MAX_STAGE_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be in [1, {MAX_STAGE_BATCH_SIZE}]"
        )
    if analyzer_timeout_seconds <= 0:
        raise ValueError("analyzer_timeout_seconds must be positive")
    normalized_url = _normalize_postgres_url(database_url)
    temp_path: Path | None = None
    total_started = time.perf_counter()
    monitor = PeakRSSMonitor()
    async with monitor:
        temp_path = Path(tempfile.mkdtemp(prefix="mnemos-e4-50k-"))
        try:
            corpus = await asyncio.to_thread(
                _generate_source_tree,
                temp_path / "source",
                file_count,
            )
            result = await _run_in_schema(
                normalized_url,
                source_root=temp_path / "source",
                corpus=corpus,
                batch_size=batch_size,
                analyzer_timeout_seconds=analyzer_timeout_seconds,
            )
        finally:
            await asyncio.to_thread(_parallel_cleanup_temp_root, temp_path)
    result["peak_rss"] = monitor.payload()
    result["total_wall_seconds"] = _round_seconds(
        time.perf_counter() - total_started
    )
    result["cleanup"] = {
        "private_schema_dropped": bool(result["private_schema"]["dropped"]),
        "temporary_source_tree_removed": bool(
            temp_path is not None and not temp_path.exists()
        ),
    }
    if not all(result["cleanup"].values()):
        raise AssertionError(f"benchmark cleanup was incomplete: {result['cleanup']}")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=(
            os.environ.get("MNEMOS_TEST_POSTGRES_URL")
            or os.environ.get("DATABASE_URL")
        ),
        help=(
            "PostgreSQL URL; defaults to MNEMOS_TEST_POSTGRES_URL or DATABASE_URL"
        ),
    )
    parser.add_argument("--files", type=int, default=DEFAULT_FILE_COUNT)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_STAGE_BATCH_SIZE,
    )
    parser.add_argument(
        "--analyzer-timeout-seconds",
        type=float,
        default=DEFAULT_ANALYZER_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="optional path for an atomically-written copy of the final JSON",
    )
    return parser


def _write_result_json(path: Path, rendered: str) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(rendered + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.database_url:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "postgres_database_url_required",
                },
                sort_keys=True,
            )
        )
        return 2
    try:
        result = asyncio.run(
            run_soak(
                args.database_url,
                file_count=args.files,
                batch_size=args.batch_size,
                analyzer_timeout_seconds=args.analyzer_timeout_seconds,
            )
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary is intentionally terse
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": type(exc).__name__,
                    "detail": "soak failed; inspect the exception in a debugger",
                },
                sort_keys=True,
            )
        )
        return 1
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output_json is not None:
        _write_result_json(args.output_json, rendered)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
