"""Fail-closed project ownership binding for manual source directories."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from app.config import get_settings


PROJECT_SOURCE_BINDING_CODES = frozenset(
    {
        "source_binding_invalid",
        "source_allowed_root_link_rejected",
        "source_allowed_root_not_configured",
        "source_allowed_root_not_directory",
        "source_allowed_root_not_found",
        "source_path_alias_rejected",
        "source_path_link_rejected",
        "source_path_must_be_absolute",
        "source_path_not_directory",
        "source_path_not_found",
        "source_path_outside_project_root",
        "source_path_parent_rejected",
        "source_project_root_empty",
        "source_project_root_escape_rejected",
        "source_project_root_must_be_relative",
        "source_project_root_not_configured",
        "source_project_root_not_directory",
        "source_project_root_not_found",
        "source_project_root_parent_rejected",
        "source_project_roots_duplicate_key",
        "source_project_roots_duplicate_project_id",
        "source_project_roots_invalid_entry",
        "source_project_roots_invalid_json",
        "source_project_roots_invalid_project_id",
        "source_project_roots_not_configured",
    }
)


class ProjectSourceBindingError(ValueError):
    """Manual source failure carrying one closed, public validation code."""

    def __init__(self, code: str) -> None:
        if code not in PROJECT_SOURCE_BINDING_CODES:
            code = "source_binding_invalid"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ResolvedProjectSource:
    allowed_root: Path
    project_root: Path
    source_path: Path


def _reject_duplicate_object_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectSourceBindingError(
                "source_project_roots_duplicate_key"
            )
        result[key] = value
    return result


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


def _relative_mapping_path(raw: str) -> Path:
    value = raw.strip()
    if not value or "\x00" in value:
        raise ProjectSourceBindingError("source_project_root_empty")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
    ):
        raise ProjectSourceBindingError("source_project_root_must_be_relative")
    if ".." in posix.parts or ".." in windows.parts:
        raise ProjectSourceBindingError("source_project_root_parent_rejected")
    # Forward slashes are portable on both supported worker platforms. Treat
    # a relative Windows separator as the same directory boundary on Linux.
    return Path(value.replace("\\", "/"))


def parse_project_source_roots(raw_json: str) -> dict[uuid.UUID, Path]:
    if not raw_json.strip():
        raise ProjectSourceBindingError("source_project_roots_not_configured")
    try:
        value = json.loads(
            raw_json,
            object_pairs_hook=_reject_duplicate_object_pairs,
        )
    except ProjectSourceBindingError:
        raise
    except (TypeError, ValueError) as exc:
        raise ProjectSourceBindingError(
            "source_project_roots_invalid_json"
        ) from exc
    if not isinstance(value, dict) or not value:
        raise ProjectSourceBindingError("source_project_roots_invalid_json")

    result: dict[uuid.UUID, Path] = {}
    for raw_project_id, raw_path in value.items():
        if not isinstance(raw_project_id, str) or not isinstance(raw_path, str):
            raise ProjectSourceBindingError("source_project_roots_invalid_entry")
        try:
            project_id = uuid.UUID(raw_project_id)
        except (ValueError, TypeError) as exc:
            raise ProjectSourceBindingError(
                "source_project_roots_invalid_project_id"
            ) from exc
        if project_id in result:
            # Catches semantically duplicate spellings of one UUID.
            raise ProjectSourceBindingError(
                "source_project_roots_duplicate_project_id"
            )
        result[project_id] = _relative_mapping_path(raw_path)
    return result


def _assert_no_link_components(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if _is_link_or_junction(current):
            raise ProjectSourceBindingError("source_path_link_rejected")


def resolve_project_source_path(
    project_id: uuid.UUID,
    source_path: str,
    *,
    allowed_root: str | Path | None = None,
    project_roots_json: str | None = None,
) -> ResolvedProjectSource:
    """Resolve one request beneath the operator-bound root for ``project_id``."""

    settings = get_settings()
    configured_allowed = str(
        allowed_root if allowed_root is not None else settings.source_allowed_root
    ).strip()
    if not configured_allowed:
        raise ProjectSourceBindingError("source_allowed_root_not_configured")
    allowed_lexical = Path(configured_allowed).expanduser()
    if _is_link_or_junction(allowed_lexical):
        raise ProjectSourceBindingError("source_allowed_root_link_rejected")
    try:
        resolved_allowed = allowed_lexical.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectSourceBindingError("source_allowed_root_not_found") from exc
    if not resolved_allowed.is_dir():
        raise ProjectSourceBindingError("source_allowed_root_not_directory")

    mappings = parse_project_source_roots(
        project_roots_json
        if project_roots_json is not None
        else settings.source_project_roots
    )
    relative_project = mappings.get(project_id)
    if relative_project is None:
        raise ProjectSourceBindingError("source_project_root_not_configured")
    _assert_no_link_components(resolved_allowed, relative_project)
    project_lexical = resolved_allowed / relative_project
    try:
        project_root = project_lexical.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectSourceBindingError("source_project_root_not_found") from exc
    if not project_root.is_dir():
        raise ProjectSourceBindingError("source_project_root_not_directory")
    if project_root != resolved_allowed and resolved_allowed not in project_root.parents:
        raise ProjectSourceBindingError("source_project_root_escape_rejected")

    if not source_path.strip() or "\x00" in source_path:
        raise ProjectSourceBindingError("source_path_must_be_absolute")
    requested = Path(source_path)
    if not requested.is_absolute():
        raise ProjectSourceBindingError("source_path_must_be_absolute")
    if ".." in PurePosixPath(source_path).parts or ".." in PureWindowsPath(
        source_path
    ).parts:
        raise ProjectSourceBindingError("source_path_parent_rejected")
    try:
        canonical_source = requested.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectSourceBindingError("source_path_not_found") from exc
    if not canonical_source.is_dir():
        raise ProjectSourceBindingError("source_path_not_directory")
    if canonical_source != project_root and project_root not in canonical_source.parents:
        raise ProjectSourceBindingError("source_path_outside_project_root")
    try:
        relative_source = requested.relative_to(project_root)
    except ValueError:
        # A lexical alias (including a symlink before the project root) is not
        # accepted even when it resolves to the right bytes.
        if requested != project_root:
            raise ProjectSourceBindingError("source_path_alias_rejected")
        relative_source = Path(".")
    _assert_no_link_components(project_root, relative_source)
    return ResolvedProjectSource(
        allowed_root=resolved_allowed,
        project_root=project_root,
        source_path=canonical_source,
    )


__all__ = [
    "PROJECT_SOURCE_BINDING_CODES",
    "ProjectSourceBindingError",
    "ResolvedProjectSource",
    "parse_project_source_roots",
    "resolve_project_source_path",
]
