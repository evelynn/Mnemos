"""Bounded source reads from the immutable revision used by analysis.

``read_file`` deliberately does not open a working-tree path.  It resolves a
completed analysis run and streams ``git show <commit>:<path>`` from the
operator-managed project mirror.  This keeps source citations aligned with
the graph even after a checkout, branch, or webhook head moves.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.source_snapshot import (
    GitSourceSnapshot,
    SourceSnapshotError,
    normalize_project_path,
    resolve_git_source_snapshot,
    revalidate_current_source_snapshot,
    snapshot_error_fields,
)

SCHEMA_SOURCE_FILE_WINDOW = "mnemos.source.file_window.v1"

# The MCP server's outer cap is 50 KiB.  Keep the deterministic file envelope
# below it so the generic cap never replaces source content with a scalar
# overflow error.  Raw buffering is separately bounded for binary/base64 and
# giant-line inputs.
MAX_FILE_RESPONSE_BYTES = 44 * 1024
_MAX_RAW_WINDOW_BYTES = 36 * 1024
_MAX_WINDOW_LINES = 2000
_GIT_TIMEOUT_SECONDS = 30.0
_GIT_STDERR_BYTES = 4096
_STREAM_CHUNK_BYTES = 8192


class _GitReadError(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


def _git_executable() -> str:
    """Return Git's real executable, bypassing Git-for-Windows' cmd shim.

    The ``cmd/git.exe`` shim spawns ``mingw64/bin/git.exe``. Terminating only
    the shim after a bounded read leaves its child streaming a giant blob and
    keeps the pipe open indefinitely. Launching the real binary gives asyncio
    ownership of the process that holds the pipe.
    """

    discovered = shutil.which("git")
    if discovered is None:
        raise _GitReadError("git_unavailable", "git executable is not available")
    candidate = Path(discovered)
    if os.name == "nt" and candidate.parent.name.lower() == "cmd":
        real = candidate.parent.parent / "mingw64" / "bin" / "git.exe"
        if real.is_file():
            return str(real)
    return discovered


@dataclass(frozen=True)
class _RawWindow:
    content: bytes
    start_line: int
    end_line: int
    truncated_before: bool
    truncated_after: bool
    output_budget_reached: bool


def _range_or_error(
    start_line: int | None, end_line: int | None
) -> tuple[int, int] | SourceSnapshotError:
    for name, value in (("start_line", start_line), ("end_line", end_line)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            return SourceSnapshotError(
                "invalid_line_range", reason=f"{name} must be an integer"
            )
    start = 1 if start_line is None else start_line
    if start < 1:
        return SourceSnapshotError(
            "invalid_line_range", reason="start_line must be greater than or equal to 1"
        )
    requested_end = start + _MAX_WINDOW_LINES - 1 if end_line is None else end_line
    if requested_end < start:
        return SourceSnapshotError(
            "invalid_line_range", reason="end_line must be greater than or equal to start_line"
        )
    return start, min(requested_end, start + _MAX_WINDOW_LINES - 1)


def _base_response(
    *,
    path: str,
    start_line: int,
    end_line: int,
    run_id: uuid.UUID | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_SOURCE_FILE_WINDOW,
        "run_id": str(run_id) if run_id is not None else None,
        "revision": revision,
        "path": path,
        "range": {"start_line": start_line, "end_line": end_line},
        "truncated": False,
    }


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _drain_limited(
    stream: asyncio.StreamReader | None, limit: int
) -> bytes:
    if stream is None:
        return b""
    result = bytearray()
    while chunk := await stream.read(_STREAM_CHUNK_BYTES):
        remaining = limit - len(result)
        if remaining > 0:
            result.extend(chunk[:remaining])
    return bytes(result)


async def _git_capture(mirror: Path, *arguments: str) -> tuple[int, bytes, bytes]:
    executable = _git_executable()
    process = await asyncio.create_subprocess_exec(
        executable,
        "-C",
        str(mirror),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_GIT_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        await _terminate_process(process)
        raise _GitReadError("git_timeout", "git metadata lookup timed out") from exc
    # Metadata commands used here return one object id/type.  Treat an
    # unexpectedly large response as a malformed repository boundary.
    if len(stdout) > _GIT_STDERR_BYTES:
        raise _GitReadError("git_metadata_overflow", "git metadata response exceeded its cap")
    return int(process.returncode or 0), stdout, stderr[:_GIT_STDERR_BYTES]


async def _resolve_blob(
    snapshot: GitSourceSnapshot, project_path: str
) -> tuple[Path, str, str]:
    """Find a candidate mirror containing the run commit and requested blob."""

    tree_path = (
        PurePosixPath(snapshot.tree_prefix, project_path).as_posix()
        if snapshot.tree_prefix
        else project_path
    )
    revision_missing = True
    for mirror in snapshot.mirrors:
        code, stdout, _ = await _git_capture(
            mirror, "rev-parse", "--verify", f"{snapshot.revision}^{{commit}}"
        )
        if code != 0:
            continue
        revision_missing = False
        resolved_revision = stdout.decode("ascii", errors="ignore").strip()
        if len(resolved_revision) not in {40, 64} or any(
            character not in "0123456789abcdefABCDEF" for character in resolved_revision
        ):
            continue
        code, stdout, _ = await _git_capture(
            mirror, "cat-file", "-t", f"{resolved_revision}:{tree_path}"
        )
        if code != 0:
            continue
        if stdout.strip() != b"blob":
            raise _GitReadError(
                "source_path_not_a_file", "requested path is not a file in the analysis revision"
            )
        return mirror, resolved_revision.lower(), tree_path

    if revision_missing:
        raise _GitReadError(
            "source_revision_unavailable",
            "analysis revision is not present in the configured project mirror",
        )
    raise _GitReadError(
        "source_path_not_found",
        "requested path does not exist in the analysis revision",
    )


def _window_end_line(content: bytes, start_line: int) -> int:
    if not content:
        return start_line - 1
    newline_count = content.count(b"\n")
    return start_line + newline_count - (1 if content.endswith(b"\n") else 0)


async def _collect_raw_window(
    stream: asyncio.StreamReader,
    *,
    start_line: int,
    end_line: int,
) -> _RawWindow:
    """Collect a line window with constant memory, including giant lines."""

    content = bytearray()
    current_line = 1
    saw_before = False
    truncated_after = False
    output_budget_reached = False
    stop = False

    while not stop and (chunk := await stream.read(_STREAM_CHUNK_BYTES)):
        offset = 0
        while offset < len(chunk):
            if current_line < start_line:
                newline = chunk.find(b"\n", offset)
                saw_before = True
                if newline < 0:
                    offset = len(chunk)
                else:
                    current_line += 1
                    offset = newline + 1
                continue

            if current_line > end_line:
                truncated_after = True
                stop = True
                break

            newline = chunk.find(b"\n", offset)
            piece_end = len(chunk) if newline < 0 else newline + 1
            piece = chunk[offset:piece_end]
            remaining = _MAX_RAW_WINDOW_BYTES - len(content)
            if len(piece) > remaining:
                if remaining > 0:
                    content.extend(piece[:remaining])
                output_budget_reached = True
                truncated_after = True
                stop = True
                break
            content.extend(piece)
            offset = piece_end
            if newline >= 0:
                current_line += 1

    return _RawWindow(
        content=bytes(content),
        start_line=start_line,
        end_line=_window_end_line(bytes(content), start_line),
        truncated_before=saw_before,
        truncated_after=truncated_after,
        output_budget_reached=output_budget_reached,
    )


async def _git_show_window(
    mirror: Path,
    *,
    revision: str,
    tree_path: str,
    start_line: int,
    end_line: int,
) -> _RawWindow:
    executable = _git_executable()
    process = await asyncio.create_subprocess_exec(
        executable,
        "-C",
        str(mirror),
        "show",
        "--no-ext-diff",
        "--no-textconv",
        f"{revision}:{tree_path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    stderr_task = asyncio.create_task(_drain_limited(process.stderr, _GIT_STDERR_BYTES))
    stopped_early = False
    try:
        window = await asyncio.wait_for(
            _collect_raw_window(
                process.stdout, start_line=start_line, end_line=end_line
            ),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        stopped_early = window.truncated_after
        if stopped_early:
            await _terminate_process(process)
        else:
            await asyncio.wait_for(process.wait(), timeout=_GIT_TIMEOUT_SECONDS)
        stderr = await stderr_task
    except TimeoutError as exc:
        await _terminate_process(process)
        await stderr_task
        raise _GitReadError("git_timeout", "source snapshot read timed out") from exc
    except BaseException:
        await _terminate_process(process)
        if not stderr_task.done():
            stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)
        raise

    if not stopped_early and process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise _GitReadError(
            "source_read_failed", detail[-500:] or "git show failed"
        )
    return window


def _json_size(value: dict[str, Any]) -> int:
    # Match the MCP server's serializer defaults exactly.
    return len(json.dumps(value, default=str).encode("utf-8"))


def _fit_text_payload(
    base: dict[str, Any], text: str, raw: bytes
) -> tuple[dict[str, Any], bool]:
    def build(length: int) -> dict[str, Any]:
        value = text[:length]
        encoded = value.encode("utf-8")
        return {
            **base,
            "content": value,
            "encoding": "utf-8",
            "byte_count": len(encoded),
            "range": {
                **base["range"],
                "end_line": _window_end_line(encoded, base["range"]["start_line"]),
            },
        }

    full = build(len(text))
    if _json_size(full) <= MAX_FILE_RESPONSE_BYTES:
        return full, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _json_size(build(middle)) <= MAX_FILE_RESPONSE_BYTES:
            low = middle
        else:
            high = middle - 1
    result = build(low)
    result["truncation"]["output_budget"] = True
    result["truncation"]["after"] = True
    result["truncated"] = True
    return result, len(result["content"].encode("utf-8")) < len(raw)


def _fit_binary_payload(
    base: dict[str, Any], raw: bytes
) -> tuple[dict[str, Any], bool]:
    def build(length: int) -> dict[str, Any]:
        value = raw[:length]
        return {
            **base,
            "content_base64": base64.b64encode(value).decode("ascii"),
            "encoding": "base64",
            "byte_count": len(value),
            "range": {
                **base["range"],
                "end_line": _window_end_line(value, base["range"]["start_line"]),
            },
        }

    full = build(len(raw))
    if _json_size(full) <= MAX_FILE_RESPONSE_BYTES:
        return full, False
    low, high = 0, len(raw)
    while low < high:
        middle = (low + high + 1) // 2
        if _json_size(build(middle)) <= MAX_FILE_RESPONSE_BYTES:
            low = middle
        else:
            high = middle - 1
    result = build(low)
    result["truncation"]["output_budget"] = True
    result["truncation"]["after"] = True
    result["truncated"] = True
    return result, low < len(raw)


def _serialize_window(
    *,
    snapshot: GitSourceSnapshot,
    revision: str,
    path: str,
    window: _RawWindow,
) -> dict[str, Any]:
    base = _base_response(
        path=path,
        start_line=window.start_line,
        end_line=window.end_line,
        run_id=snapshot.run_id,
        revision=revision,
    )
    base["truncation"] = {
        "before": window.truncated_before,
        "after": window.truncated_after,
        "output_budget": window.output_budget_reached,
    }
    base["truncated"] = any(base["truncation"].values())

    text_bytes = window.content
    try:
        text = text_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A byte budget can land inside the final UTF-8 code point of an
        # otherwise valid giant line.  Back off only that incomplete suffix;
        # invalid bytes anywhere else correctly select the base64 dialect.
        if (
            window.output_budget_reached
            and exc.end == len(text_bytes)
            and exc.reason == "unexpected end of data"
        ):
            text_bytes = text_bytes[:exc.start]
            text = text_bytes.decode("utf-8")
            result, fitted = _fit_text_payload(base, text, text_bytes)
        else:
            result, fitted = _fit_binary_payload(base, window.content)
    else:
        result, fitted = _fit_text_payload(base, text, window.content)
    if fitted:
        result["truncated"] = True
    return result


async def read_project_file(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    run_id: uuid.UUID | str | None = None,
    mirror_root: str | Path | None = None,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Read a bounded file window from a completed analysis revision."""

    range_result = _range_or_error(start_line, end_line)
    requested_start = start_line if isinstance(start_line, int) else 1
    requested_end = end_line if isinstance(end_line, int) else requested_start
    if isinstance(range_result, SourceSnapshotError):
        return {
            **_base_response(
                path=str(file_path),
                start_line=requested_start,
                end_line=requested_end,
            ),
            **snapshot_error_fields(range_result),
        }
    start, end = range_result

    try:
        path = normalize_project_path(file_path)
    except SourceSnapshotError as exc:
        return {
            **_base_response(path=str(file_path), start_line=start, end_line=end),
            **snapshot_error_fields(exc),
        }

    try:
        snapshot = await resolve_git_source_snapshot(
            db,
            project_id=project_id,
            run_id=run_id,
            mirror_root=mirror_root,
            allowed_root=allowed_root,
        )
        mirror, revision, tree_path = await _resolve_blob(snapshot, path)
        window = await _git_show_window(
            mirror,
            revision=revision,
            tree_path=tree_path,
            start_line=start,
            end_line=end,
        )
        # Resolution alone is not a read fence: promotion can commit while
        # ``git show`` is streaming. Recheck only after the final source I/O
        # and discard the old-revision payload on a generation change.
        await revalidate_current_source_snapshot(db, snapshot=snapshot)
        return _serialize_window(
            snapshot=snapshot,
            revision=revision,
            path=path,
            window=window,
        )
    except SourceSnapshotError as exc:
        return {
            **_base_response(
                path=path,
                start_line=start,
                end_line=end,
                run_id=exc.run_id,
                revision=exc.revision,
            ),
            **snapshot_error_fields(exc),
        }
    except _GitReadError as exc:
        return {
            **_base_response(
                path=path,
                start_line=start,
                end_line=end,
                run_id=snapshot.run_id,
                revision=snapshot.revision,
            ),
            "error": exc.code,
            "reason": exc.reason,
        }
