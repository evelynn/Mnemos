"""Resolve immutable source snapshots for source-reference tools.

The graph says *which* source revision was analysed.  This module turns that
revision into a repository object without consulting a mutable checkout.  A
legacy webhook run is safe because webhook ingestion can only complete after
``prepare_source`` materialises its exact commit from the project mirror.
Manual runs need an explicit provenance contract in ``AnalysisRun.stats``;
older manual runs did not retain enough information to prove that their
``git_sha`` and source directory described the same bytes.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.graph_publication import (
    GraphGenerationChanged,
    GraphPublicationError,
    GraphReadStamp,
    read_graph_stamp,
    revalidate_graph_stamp,
    validate_graph_publication_receipt,
)
from app.models.graph import AnalysisRun
from app.orchestrator.source_manifest import INDEX_CONTRACT_VERSION

SOURCE_SNAPSHOT_CONTRACT = "mnemos.source-snapshot.v1"
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_MAX_PROJECT_PATH_CHARS = 4096
_UNPUBLISHED_REFRESH_STATUSES = frozenset({"running", "failed", "cancelled"})

# This is returned with provenance errors so an operator/producer knows what
# future manual analyses must persist.  ``tree_prefix`` is essential for a
# manual analysis rooted below the Git repository root.  A non-Git source must
# first be copied into an immutable, content-addressed store; its live path is
# intentionally not a valid snapshot locator.
REQUIRED_SOURCE_SNAPSHOT_STATS: dict[str, Any] = {
    "stats_path": "analysis_run.stats.source_snapshot",
    "git_commit": {
        "contract_version": SOURCE_SNAPSHOT_CONTRACT,
        "kind": "git_commit",
        "revision": "<immutable full commit sha>",
        "repository_key": "<project UUID>",
        "repository_locator": {
            "kind": "allowed_root_relative",
            "path": "<repository path below SOURCE_ALLOWED_ROOT>",
        },
        "tree_prefix": "<repo-relative analysed root, empty for repo root>",
        "immutable": True,
    },
    "non_git": {
        "contract_version": SOURCE_SNAPSHOT_CONTRACT,
        "kind": "content_archive",
        "snapshot_id": "sha256:<content digest>",
        "storage_key": "<immutable content-store key>",
        "tree_prefix": "",
        "immutable": True,
    },
}


class SourceSnapshotError(RuntimeError):
    """A source revision cannot be read without guessing or drifting."""

    def __init__(
        self,
        code: str,
        *,
        reason: str,
        run_id: uuid.UUID | None = None,
        revision: str | None = None,
        include_required_stats: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.run_id = run_id
        self.revision = revision
        self.include_required_stats = include_required_stats


@dataclass(frozen=True)
class GitSourceSnapshot:
    """A completed analysis run pinned to a project mirror and tree prefix."""

    run_id: uuid.UUID
    revision: str
    mirrors: tuple[Path, ...]
    tree_prefix: str
    # Present only for an implicit "current graph" resolution.  Consumers
    # that perform I/O after resolution must revalidate this stamp after that
    # I/O, because PostgreSQL READ COMMITTED can publish a new generation in
    # between. Explicit historical reads intentionally carry no stamp.
    graph_stamp: GraphReadStamp | None = None


async def revalidate_current_source_snapshot(
    db: AsyncSession, *, snapshot: GitSourceSnapshot
) -> None:
    """Discard a current-source result that crossed graph publication."""

    if snapshot.graph_stamp is None:
        return
    try:
        await revalidate_graph_stamp(db, stamp=snapshot.graph_stamp)
    except GraphGenerationChanged as exc:
        raise SourceSnapshotError(
            "current_graph_snapshot_changed_retry",
            reason=(
                "the published graph changed while resolving or reading source; "
                "retry so graph facts and source bytes use one generation"
            ),
            run_id=snapshot.run_id,
            revision=snapshot.revision,
        ) from exc


@dataclass(frozen=True)
class UnpublishedRefresh:
    """Legacy diagnostic describing a post-baseline non-successful run.

    Phase-B readers must use ``GraphHead`` and never treat this timestamp
    heuristic as publication truth.  It remains only for lifecycle diagnostics
    and backwards-compatible operator tests.
    """

    run_id: uuid.UUID
    status: str
    started_at: datetime
    latest_completed_at: datetime | None


async def find_unpublished_refresh(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    exclude_run_id: uuid.UUID | None = None,
) -> UnpublishedRefresh | None:
    """Return the newest legacy run matching the old unsafe-write heuristic.

    This function is deliberately not a current-graph read guard.  Phase-B
    staging means a running, failed, or cancelled run cannot mutate published
    rows, while ``GraphHead`` is the sole publication authority.  Retaining the
    query lets operators identify pre-Phase-B incidents without reintroducing
    timestamp inference into source or graph consumers.
    """

    latest_completed_at = (
        await db.execute(
            select(AnalysisRun.completed_at)
            .where(
                AnalysisRun.project_id == project_id,
                AnalysisRun.status == "completed",
                AnalysisRun.completed_at.is_not(None),
            )
            .order_by(AnalysisRun.completed_at.desc(), AnalysisRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    candidate_stmt = select(
        AnalysisRun.id,
        AnalysisRun.status,
        AnalysisRun.started_at,
    ).where(
        AnalysisRun.project_id == project_id,
        AnalysisRun.started_at.is_not(None),
        AnalysisRun.status.in_(_UNPUBLISHED_REFRESH_STATUSES),
    )
    if exclude_run_id is not None:
        candidate_stmt = candidate_stmt.where(AnalysisRun.id != exclude_run_id)
    if latest_completed_at is not None:
        candidate_stmt = candidate_stmt.where(
            # Start time alone misses an overlapping writer that began before
            # the baseline but failed/cancelled after it. Any active writer is
            # unsafe; a terminal non-success is safe only when both its start
            # and terminal timestamp prove it ended before publication. Equal
            # ticks remain unproven on low-resolution databases.
            or_(
                AnalysisRun.status == "running",
                AnalysisRun.started_at >= latest_completed_at,
                AnalysisRun.completed_at.is_(None),
                AnalysisRun.completed_at >= latest_completed_at,
            )
        )
    candidate = (
        await db.execute(
            candidate_stmt.order_by(
                AnalysisRun.started_at.desc(), AnalysisRun.created_at.desc()
            ).limit(1)
        )
    ).first()
    if candidate is None:
        return None

    candidate_id, status, started_at = candidate
    return UnpublishedRefresh(
        run_id=(
            candidate_id
            if isinstance(candidate_id, uuid.UUID)
            else uuid.UUID(str(candidate_id))
        ),
        status=str(status),
        started_at=started_at,
        latest_completed_at=latest_completed_at,
    )


def normalize_project_path(file_path: str) -> str:
    """Return one safe Git-tree path or raise a path-specific error.

    Both POSIX and Windows absolute forms are rejected regardless of the host
    OS.  Backslashes are accepted only as relative separators and normalized
    before the path becomes part of a Git object expression.
    """

    if not isinstance(file_path, str) or not file_path or "\x00" in file_path:
        raise SourceSnapshotError(
            "invalid_source_path",
            reason="file_path must be a non-empty, NUL-free relative path",
        )
    if len(file_path) > _MAX_PROJECT_PATH_CHARS:
        raise SourceSnapshotError(
            "invalid_source_path",
            reason=f"file_path exceeds the {_MAX_PROJECT_PATH_CHARS}-character limit",
        )
    if PurePosixPath(file_path).is_absolute() or PureWindowsPath(file_path).is_absolute():
        raise SourceSnapshotError(
            "absolute_source_path_rejected",
            reason="read_file accepts only project-relative paths",
        )

    normalized = PurePosixPath(file_path.replace("\\", "/"))
    if ".." in normalized.parts:
        raise SourceSnapshotError(
            "source_path_escape_rejected",
            reason="file_path must not contain a parent-directory segment",
        )
    canonical = normalized.as_posix()
    if canonical in {"", "."}:
        raise SourceSnapshotError(
            "invalid_source_path",
            reason="file_path must identify a file below the project root",
        )
    return canonical


def _normalize_tree_prefix(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return normalize_project_path(str(value)).rstrip("/")
    except SourceSnapshotError as exc:
        raise SourceSnapshotError(
            "source_snapshot_provenance_invalid",
            reason=f"stored tree_prefix is unsafe: {exc.reason}",
            include_required_stats=True,
        ) from exc


def _configured_mirrors(project_id: uuid.UUID, mirror_root: str | Path | None) -> tuple[Path, ...]:
    configured = str(
        mirror_root if mirror_root is not None else get_settings().source_mirror_root
    ).strip()
    if not configured:
        raise SourceSnapshotError(
            "source_snapshot_unavailable",
            reason="SOURCE_MIRROR_ROOT is not configured",
        )
    try:
        root = Path(configured).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SourceSnapshotError(
            "source_snapshot_unavailable",
            reason="configured SOURCE_MIRROR_ROOT does not exist",
        ) from exc
    if not root.is_dir():
        raise SourceSnapshotError(
            "source_snapshot_unavailable",
            reason="configured SOURCE_MIRROR_ROOT is not a directory",
        )

    candidates = tuple(
        candidate
        for candidate in (root / str(project_id), root / f"{project_id}.git")
        if candidate.is_dir()
    )
    if not candidates:
        raise SourceSnapshotError(
            "source_snapshot_unavailable",
            reason="project mirror is missing below SOURCE_MIRROR_ROOT",
        )
    return candidates


def _allowed_root_repository(
    locator: Any,
    allowed_root: str | Path | None,
) -> tuple[Path, ...]:
    """Resolve a trusted run-produced repository locator without host paths."""

    if not isinstance(locator, dict) or locator.get("kind") != "allowed_root_relative":
        raise SourceSnapshotError(
            "source_snapshot_provenance_invalid",
            reason="stored repository_locator kind is unsupported",
            include_required_stats=True,
        )
    raw_relative = locator.get("path")
    if not isinstance(raw_relative, str):
        raise SourceSnapshotError(
            "source_snapshot_provenance_invalid",
            reason="stored repository_locator path must be a string",
            include_required_stats=True,
        )
    if raw_relative in {"", "."}:
        relative = ""
    else:
        try:
            relative = normalize_project_path(raw_relative)
        except SourceSnapshotError as exc:
            raise SourceSnapshotError(
                "source_snapshot_provenance_invalid",
                reason=f"stored repository_locator path is unsafe: {exc.reason}",
                include_required_stats=True,
            ) from exc

    configured = str(
        allowed_root
        if allowed_root is not None
        else get_settings().source_allowed_root
    ).strip()
    if not configured:
        raise SourceSnapshotError(
            "source_snapshot_unavailable",
            reason="SOURCE_ALLOWED_ROOT is not configured for this manual Git run",
        )
    try:
        root = Path(configured).expanduser().resolve(strict=True)
        repository = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise SourceSnapshotError(
            "source_snapshot_unavailable",
            reason="stored manual Git repository is no longer available",
        ) from exc
    if repository != root and root not in repository.parents:
        raise SourceSnapshotError(
            "source_snapshot_provenance_invalid",
            reason="stored repository_locator escapes SOURCE_ALLOWED_ROOT",
            include_required_stats=True,
        )
    if not repository.is_dir():
        raise SourceSnapshotError(
            "source_snapshot_unavailable",
            reason="stored manual Git repository is not a directory",
        )
    return (repository,)


async def resolve_git_source_snapshot(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID | str | None = None,
    mirror_root: str | Path | None = None,
    allowed_root: str | Path | None = None,
) -> GitSourceSnapshot:
    """Resolve a completed run to immutable Git provenance.

    With no ``run_id`` the run identified by the ready ``GraphHead`` is used.
    A merely newer completed run can be a no-op/continuation and is not proof
    that its source revision owns the current graph. Explicit historical reads
    remain pinned to the requested completed run.
    """

    requested_run_id: uuid.UUID | None = None
    if run_id is not None:
        try:
            requested_run_id = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
        except (TypeError, ValueError) as exc:
            raise SourceSnapshotError(
                "invalid_analysis_run_id",
                reason="run_id must be a UUID",
            ) from exc

    graph_stamp: GraphReadStamp | None = None
    if requested_run_id is None:
        try:
            graph_stamp = await read_graph_stamp(db, project_id=project_id)
        except GraphPublicationError as exc:
            raise SourceSnapshotError(
                "current_graph_snapshot_unavailable",
                reason=(
                    "no atomically published graph is ready; run one staged "
                    "full source analysis before reading current source"
                ),
            ) from exc
        selected_run_id = graph_stamp.current_run_id
    else:
        selected_run_id = requested_run_id

    stmt = select(AnalysisRun).where(
        AnalysisRun.project_id == project_id,
        AnalysisRun.id == selected_run_id,
    )
    run = (await db.execute(stmt)).scalar_one_or_none()
    stats = run.stats if run is not None and isinstance(run.stats, dict) else {}
    try:
        publication = (
            validate_graph_publication_receipt(run)
            if run is not None
            else None
        )
    except GraphPublicationError:
        publication = None
    if run is None or publication is None or (
        graph_stamp is not None
        and (
            publication.generation != graph_stamp.generation
            or publication.published_at != graph_stamp.published_at
        )
    ):
        raise SourceSnapshotError(
            "published_analysis_run_not_found",
            reason=(
                "no matching analysis run has an atomic source publication receipt"
            ),
            run_id=requested_run_id,
        )

    revision = str(run.git_sha or "")
    if not _COMMIT_RE.fullmatch(revision):
        raise SourceSnapshotError(
            "source_snapshot_unavailable",
            reason="analysis revision is symbolic or unpinned; exact source bytes cannot be proven",
            run_id=run.id,
            revision=revision or None,
            include_required_stats=True,
        )

    provenance = stats.get("source_snapshot")
    repository_locator: Any = None
    if provenance is None:
        refresh = stats.get("refresh") if isinstance(stats.get("refresh"), dict) else {}
        manifest = (
            stats.get("source_manifest")
            if isinstance(stats.get("source_manifest"), dict)
            else {}
        )
        safe_legacy_webhook = (
            str(run.triggered_by or "").startswith("webhook:")
            and manifest.get("contract_version") == INDEX_CONTRACT_VERSION
            and refresh.get("authoritative_root") is True
        )
        if not safe_legacy_webhook:
            raise SourceSnapshotError(
                "source_snapshot_unavailable",
                reason=(
                    "analysis run did not retain immutable source provenance; "
                    "reading a live checkout would risk revision drift"
                ),
                run_id=run.id,
                revision=revision,
                include_required_stats=True,
            )
        # Legacy webhook runs are rooted at a detached worktree created from
        # the project mirror, so their implicit prefix is the repository root.
        tree_prefix = ""
    else:
        if not isinstance(provenance, dict):
            raise SourceSnapshotError(
                "source_snapshot_provenance_invalid",
                reason="analysis_run.stats.source_snapshot must be an object",
                run_id=run.id,
                revision=revision,
                include_required_stats=True,
            )
        expected = {
            "contract_version": SOURCE_SNAPSHOT_CONTRACT,
            "kind": "git_commit",
            "immutable": True,
            "repository_key": str(project_id),
        }
        mismatches = [
            key for key, value in expected.items() if provenance.get(key) != value
        ]
        if provenance.get("revision") != revision:
            mismatches.append("revision")
        if mismatches:
            raise SourceSnapshotError(
                "source_snapshot_provenance_invalid",
                reason=(
                    "analysis_run.stats.source_snapshot violates fields: "
                    + ", ".join(sorted(set(mismatches)))
                ),
                run_id=run.id,
                revision=revision,
                include_required_stats=True,
            )
        tree_prefix = _normalize_tree_prefix(provenance.get("tree_prefix"))
        repository_locator = provenance.get("repository_locator")

    try:
        mirrors = (
            _allowed_root_repository(repository_locator, allowed_root)
            if repository_locator is not None
            else _configured_mirrors(project_id, mirror_root)
        )
    except SourceSnapshotError as exc:
        exc.run_id = run.id
        exc.revision = revision
        raise
    snapshot = GitSourceSnapshot(
        run_id=run.id,
        revision=revision,
        mirrors=mirrors,
        tree_prefix=tree_prefix,
        graph_stamp=graph_stamp,
    )
    # Abort before downstream Git I/O when publication already crossed the
    # resolver itself. The final consumer must revalidate once more after its
    # last I/O/query; ``read_project_file`` does so before serialization.
    await revalidate_current_source_snapshot(db, snapshot=snapshot)
    return snapshot


def snapshot_error_fields(exc: SourceSnapshotError) -> dict[str, Any]:
    """Serialize a snapshot failure without exposing host filesystem paths."""

    result: dict[str, Any] = {
        "error": exc.code,
        "reason": exc.reason,
        "run_id": str(exc.run_id) if exc.run_id is not None else None,
        "revision": exc.revision,
    }
    if exc.include_required_stats:
        result["required_stats_contract"] = REQUIRED_SOURCE_SNAPSHOT_STATS
    return result
