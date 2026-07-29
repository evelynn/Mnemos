"""Deterministic source fingerprints used to make refreshes scale-safe.

The graph analyzers still own parsing.  This module performs only a cheap,
LLM-free content pass so an incremental run can answer two questions before
spawning any analyzer process:

* did any source observed by a given analyzer actually change?
* which analyzer families can be skipped without risking stale facts?

Fingerprints are grouped by analyzer binary rather than by project.  A Python
change therefore does not make the TypeScript, Java, and C++ analyzers walk the
tree again.  Content (not mtime) is hashed, so touching a file is a no-op while
deleting or renaming it invalidates the owning analyzer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from app.analyzers.registry import SOURCE_ANALYZER_LANGUAGES, binary_for
from app.analyzers.runner import inrepo_script


INDEX_CONTRACT_VERSION = "mnemos.source-index.v5"
_GIT_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40,64}$")
_GIT_TIMEOUT_SEC = 60.0
_MAX_TSCONFIG_BYTES = 1024 * 1024
_MAX_TSCONFIG_FILES = 64
_MAX_TSCONFIG_DEPTH = 32
_MAX_TSCONFIG_EXTENDS = 64
_MAX_TSCONFIG_SPECIFIER_BYTES = 4096
_MAX_TSCONFIG_RESOLUTIONS = 256
_MAX_TSCONFIG_PROBES = 512

# Keep this aligned with the deterministic analyzer contracts.  The value is
# deliberately public/tested: adding a language without declaring its source
# suffixes would make incremental refresh unsafe and must be visible in review.
LANGUAGE_EXTENSIONS: dict[str, frozenset[str]] = {
    "csharp": frozenset({".cs", ".csproj", ".sln", ".props", ".targets"}),
    "typescript": frozenset(
        {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue"}
    ),
    "javascript": frozenset(
        {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue"}
    ),
    "python": frozenset({".py"}),
    "cpp": frozenset({".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx"}),
    "java": frozenset({".java"}),
    "html": frozenset({".html", ".htm", ".xhtml"}),
    "css": frozenset({".css", ".sass", ".less"}),
    "scss": frozenset({".scss"}),
    "kotlin": frozenset({".kt", ".kts"}),
    "go": frozenset({".go"}),
    "rust": frozenset({".rs"}),
    "ruby": frozenset({".rb"}),
    "php": frozenset({".php"}),
    "scala": frozenset({".scala", ".sc"}),
    "swift": frozenset({".swift"}),
    "dotnet_binary": frozenset({".dll", ".exe"}),
}

# Source analyzers do not all ignore the same trees.  A single global denylist
# caused unsafe false no-ops (for example ggoss-py used to scan ``vendor``
# while the manifest ignored it).  These sets are an explicit mirror of each
# producer's discovery contract.  A false positive merely runs an analyzer
# again; a false negative can leave stale source facts and is not acceptable.
_EXCLUDED_BY_ANALYZER: dict[str, frozenset[str]] = {
    "ggoss-csharp": frozenset({
        "node_modules", "bin", "obj", "build", "dist", "out", "target",
        "vendor", "third_party", "__pycache__",
    }),
    "ggoss-py": frozenset({
        "__pycache__", ".venv", "venv", "env", ".git", "node_modules",
        "build", "dist", ".pytest_cache", ".tox", ".eggs", "site-packages",
    }),
    "ggoss-ts": frozenset({"node_modules", "dist", "build", "coverage", "out"}),
    "ggoss-cpp": frozenset({
        ".git", ".mnemos", ".svn", "__pycache__", "node_modules", "build",
        "dist", "out", "cmake-build-debug", "cmake-build-release", "vendored",
        "vendor", "third_party", "thirdparty", "external",
    }),
    "ggoss-java": frozenset({
        ".git", ".mnemos", ".idea", ".gradle", ".mvn", "target", "build",
        "out", "bin", "generated", "generated-sources", "node_modules",
    }),
    "ggoss-web": frozenset({
        ".git", ".mnemos", "node_modules", "target", "build", "dist", "out",
        "bower_components", "vendor",
    }),
    "ggoss-kotlin": frozenset({
        ".git", ".mnemos", ".idea", ".gradle", "build", "out", "target",
        "node_modules",
    }),
    "ggoss-treesitter": frozenset({
        ".git", ".mnemos", "node_modules", "target", "build", "dist", "out",
        "vendor", "vendored", "third_party", ".venv", "__pycache__",
    }),
}

_AGENT_EXCLUDED_DIRECTORIES = frozenset({
    ".git", "node_modules", "build", "dist", "out", "bin", "obj",
    "third_party", "thirdparty", "external", "vendor", "deps", "__pycache__",
    ".vs", ".vscode", "cmake-build-debug",
})

# ggoss-ts reads these root configuration files in addition to source suffixes.
# Hashing a nested copy is a safe extra invalidation and avoids path-special
# cases in this intentionally cheap pass.
_EXTRA_FILENAMES: dict[str, frozenset[str]] = {
    "ggoss-ts": frozenset({"package.json", "tsconfig.json"}),
    "ggoss-csharp": frozenset({
        "global.json",
        "NuGet.Config",
        "nuget.config",
        "packages.lock.json",
    }),
}
_TS_GENERATED_FILE = re.compile(
    r"(?:\.(?:min|bundle|iife|umd)|^(?:bundle|iife|umd))"
    r"\.(?:[cm]?js)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceManifest:
    """Content snapshot grouped by deterministic analyzer identity."""

    fingerprints: dict[str, str]
    file_counts: dict[str, int]
    total_bytes: dict[str, int]
    non_cacheable_analyzers: frozenset[str] = frozenset()

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": INDEX_CONTRACT_VERSION,
            "fingerprints": dict(sorted(self.fingerprints.items())),
            "file_counts": dict(sorted(self.file_counts.items())),
            "total_bytes": dict(sorted(self.total_bytes.items())),
            "non_cacheable_analyzers": sorted(self.non_cacheable_analyzers),
        }


@dataclass(frozen=True)
class _ConfigInput:
    """One config file that can change a deterministic analyzer result."""

    identity: str
    size: int
    marker: bytes


@dataclass
class _ConfigResolutionBudget:
    resolutions: int = 0
    probes: int = 0
    external_dependency: bool = False

    def add_resolutions(self, count: int) -> None:
        self.resolutions += count
        if self.resolutions > _MAX_TSCONFIG_RESOLUTIONS:
            raise ValueError("source_manifest_tsconfig_resolution_limit_exceeded")

    def add_probe(self) -> None:
        self.probes += 1
        if self.probes > _MAX_TSCONFIG_PROBES:
            raise ValueError("source_manifest_tsconfig_probe_limit_exceeded")


def analyzer_extensions(
    languages: Iterable[str],
    *,
    producer_options: dict[str, Any] | None = None,
) -> dict[str, frozenset[str]]:
    """Return analyzer -> relevant suffixes for registered languages."""

    capabilities: dict[str, set[str]] = {}
    for language, suffixes in LANGUAGE_EXTENSIONS.items():
        if language not in SOURCE_ANALYZER_LANGUAGES:
            continue
        if (binary := binary_for(language)) is not None:
            capabilities.setdefault(binary, set()).update(suffixes)

    normalized_languages = sorted({str(item).lower() for item in languages})
    selected: set[str] = set()
    agent_only: dict[str, frozenset[str]] = {}
    from app.extractor.agent_extract import AGENT_LANGUAGE_EXTENSIONS

    for language in normalized_languages:
        binary = binary_for(language)
        suffixes = LANGUAGE_EXTENSIONS.get(language)
        # Registered deterministic producers keep one stable logical manifest
        # key even when their executable is temporarily unavailable and an
        # agent fallback is enabled. Availability transitions are not source
        # removal and must never grant deletion authority over last-good facts.
        if binary is None:
            fallback_suffixes = AGENT_LANGUAGE_EXTENSIONS.get(language)
            if fallback_suffixes:
                agent_only[f"agent:{language}"] = frozenset(fallback_suffixes)
                continue
            continue
        if not suffixes:
            continue
        selected.add(binary)
    # One binary walks one tree and may cover several registered language
    # labels (ggoss-web, ggoss-ts, ggoss-treesitter).  Fingerprint its complete
    # file contract so a CSS change cannot hide merely because Project listed
    # only "html" while ggoss-web still emitted CSS facts.
    deterministic = {
        key: frozenset(capabilities[key])
        for key in sorted(selected)
    }
    return {**deterministic, **agent_only}


def deterministic_source_names() -> frozenset[str]:
    """All file-backed deterministic sources safe for a full-run sweep.

    Live database analyzers are intentionally absent: a maintenance-window
    skip must retain the last verified schema instead of interpreting "not
    probed" as "database is empty".
    """

    return frozenset(
        binary
        for language in SOURCE_ANALYZER_LANGUAGES
        if (binary := binary_for(language)) is not None
    )


def _path_allowed(analyzer: str, relative_parts: tuple[str, ...]) -> bool:
    directories = relative_parts[:-1]
    # Hidden directories are tooling/cache metadata in the analyzer contract.
    # Excluding them prevents local .uv/.npm/.IDE caches from multiplying the
    # first deterministic pass by orders of magnitude.
    if any(part.startswith(".") for part in directories):
        return False
    excluded = (
        _AGENT_EXCLUDED_DIRECTORIES
        if analyzer.startswith("agent:")
        else _EXCLUDED_BY_ANALYZER.get(analyzer, frozenset())
    )
    if any(part in excluded for part in directories):
        return False
    if (
        analyzer == "ggoss-web"
        and len(relative_parts) >= 3
        and tuple(part.lower() for part in relative_parts[:2])
        == ("docs", "mockup")
    ):
        return False
    if (
        analyzer == "ggoss-ts"
        and relative_parts[-1].startswith(".")
    ):
        return False
    if analyzer == "ggoss-ts" and _TS_GENERATED_FILE.search(relative_parts[-1]):
        return False
    return True


def _is_link_or_junction(path: Path) -> bool:
    """Keep fingerprint discovery aligned with analyzer tree walkers."""

    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


def _strip_jsonc_comments(text: str) -> str:
    """Remove JSONC comments without changing quoted string contents."""

    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        character = text[index]
        if in_string:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            result.append(character)
            index += 1
            continue
        if character == "/" and index + 1 < len(text):
            following = text[index + 1]
            if following == "/":
                index += 2
                while index < len(text) and text[index] not in "\r\n":
                    index += 1
                continue
            if following == "*":
                end = text.find("*/", index + 2)
                if end < 0:
                    raise ValueError("source_manifest_tsconfig_jsonc_unterminated_comment")
                # Preserve line boundaries for deterministic diagnostics.
                result.extend(
                    character
                    for character in text[index : end + 2]
                    if character in "\r\n"
                )
                index = end + 2
                continue
        result.append(character)
        index += 1
    if in_string:
        raise ValueError("source_manifest_tsconfig_jsonc_unterminated_string")
    return "".join(result)


def _remove_jsonc_trailing_commas(text: str) -> str:
    """Remove commas followed only by whitespace and a closing bracket."""

    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        character = text[index]
        if in_string:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            result.append(character)
            index += 1
            continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        result.append(character)
        index += 1
    return "".join(result)


def _tsconfig_extends(raw: bytes, identity: str) -> tuple[str, ...]:
    """Parse the bounded TypeScript JSONC dialect into canonical specifiers."""

    if len(raw) > _MAX_TSCONFIG_BYTES:
        raise ValueError(f"source_manifest_tsconfig_too_large:{identity}")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
        normalized = _remove_jsonc_trailing_commas(_strip_jsonc_comments(text))
        payload = json.loads(normalized)
    except UnicodeDecodeError as exc:
        raise ValueError(f"source_manifest_tsconfig_parse_unsupported:{identity}") from exc
    except json.JSONDecodeError as exc:
        # TypeScript's parser is error-tolerant.  Guessing which properties it
        # recovered could omit a dependency and authorize a stale skip.  A
        # malformed config without an ``extends`` property cannot add such a
        # dependency, and its own bytes are already part of the fingerprint.
        if re.search(r'"extends"\s*:', normalized):
            raise ValueError(
                f"source_manifest_tsconfig_parse_unsupported:{identity}"
            ) from exc
        return ()
    if not isinstance(payload, dict):
        raise ValueError(f"source_manifest_tsconfig_not_object:{identity}")
    extends = payload.get("extends")
    if extends is None:
        return ()
    values = (extends,) if isinstance(extends, str) else extends
    if not isinstance(values, list | tuple) or not all(
        isinstance(value, str) and value.strip() for value in values
    ):
        raise ValueError(f"source_manifest_tsconfig_extends_invalid:{identity}")
    if len(values) > _MAX_TSCONFIG_EXTENDS:
        raise ValueError(f"source_manifest_tsconfig_extends_limit_exceeded:{identity}")
    normalized_values = tuple(value.strip() for value in values)
    if any(
        len(value.encode("utf-8", errors="surrogatepass"))
        > _MAX_TSCONFIG_SPECIFIER_BYTES
        for value in normalized_values
    ):
        raise ValueError(f"source_manifest_tsconfig_specifier_too_long:{identity}")
    return normalized_values


def _package_specifier_parts(specifier: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized = specifier.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise ValueError("source_manifest_tsconfig_extends_invalid_specifier")
    parts = tuple(normalized.split("/"))
    package_count = 2 if parts[0].startswith("@") else 1
    if len(parts) < package_count:
        raise ValueError("source_manifest_tsconfig_extends_invalid_specifier")
    return parts[:package_count], parts[package_count:]


def _config_path_candidates(base: Path) -> tuple[Path, ...]:
    if base.suffix.lower() == ".json":
        return (base,)
    return (base.with_name(base.name + ".json"), base / "tsconfig.json")


def _package_json_tsconfig(raw: bytes, identity: str) -> tuple[str, ...] | None:
    """Return a bounded package.json ``tsconfig`` path, if declared."""

    if len(raw) > _MAX_TSCONFIG_BYTES:
        raise ValueError(f"source_manifest_tsconfig_package_json_too_large:{identity}")
    try:
        payload = json.loads(raw.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Node treats a malformed package manifest as unusable. Its own bytes
        # remain in the fingerprint so repairing it invalidates this state.
        return None
    if not isinstance(payload, dict) or payload.get("tsconfig") is None:
        return None
    value = payload["tsconfig"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"source_manifest_tsconfig_package_field_invalid:{identity}")
    normalized = value.strip().replace("\\", "/")
    if len(normalized.encode("utf-8", errors="surrogatepass")) > (
        _MAX_TSCONFIG_SPECIFIER_BYTES
    ):
        raise ValueError(f"source_manifest_tsconfig_package_field_too_long:{identity}")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"source_manifest_tsconfig_package_field_unsafe:{identity}")
    parts = tuple(
        part for part in PurePosixPath(normalized).parts if part not in {"", "."}
    )
    if not parts or ".." in parts:
        raise ValueError(f"source_manifest_tsconfig_package_field_unsafe:{identity}")
    return parts


def _mutable_tsconfig_candidates(
    source_root: Path,
    base: Path,
    specifier: str,
    budget: _ConfigResolutionBudget,
) -> tuple[tuple[Path, bool], ...]:
    normalized = specifier.replace("\\", "/")
    if normalized.startswith(("./", "../")):
        if "\x00" in normalized or re.match(r"^[A-Za-z]:", normalized):
            raise ValueError("source_manifest_tsconfig_extends_invalid_specifier")
        return tuple(
            (candidate, True)
            for candidate in _config_path_candidates(
                base.joinpath(*PurePosixPath(normalized).parts)
            )
        )

    package_parts, subpath_parts = _package_specifier_parts(normalized)
    search = base
    while True:
        package_root = search.joinpath("node_modules", *package_parts)
        budget.add_probe()
        if os.path.lexists(package_root):
            try:
                resolved_package = package_root.resolve(strict=True)
            except OSError as exc:
                raise ValueError(
                    f"source_manifest_tsconfig_package_unreadable:{normalized}"
                ) from exc
            if (
                resolved_package != source_root
                and source_root not in resolved_package.parents
            ):
                # The analyzer can resolve packages from an enclosing
                # node_modules directory, but the manifest must not read
                # outside its authority. The caller marks this analyzer
                # non-cacheable, so treating the base as unresolved here still
                # forces a real analyzer pass on every run.
                budget.external_dependency = True
                return ()
            if not resolved_package.is_dir():
                raise ValueError("source_manifest_tsconfig_package_not_directory")
            package_json = package_root / "package.json"
            package_inputs: list[tuple[Path, bool]] = []
            package_field: tuple[str, ...] | None = None
            budget.add_probe()
            if os.path.lexists(package_json):
                if _is_link_or_junction(package_json):
                    raise ValueError(
                        f"source_manifest_tsconfig_link_rejected:{package_json}"
                    )
                try:
                    package_raw = package_json.read_bytes()
                except OSError as exc:
                    raise ValueError(
                        f"source_manifest_tsconfig_package_unreadable:{normalized}"
                    ) from exc
                package_field = _package_json_tsconfig(
                    package_raw, package_json.as_posix()
                )
                package_inputs.append((package_json, False))
            if subpath_parts:
                target = package_root.joinpath(*subpath_parts)
            elif package_field is not None:
                target = package_root.joinpath(*package_field)
            else:
                target = package_root / "tsconfig.json"
            return (
                *package_inputs,
                *((candidate, True) for candidate in _config_path_candidates(target)),
            )
        if search.parent == search:
            break
        search = search.parent
    # This matches the analyzer's current behaviour when an npm dependency is
    # absent from the clean source checkout: TypeScript reports the unresolved
    # base and continues with the local compiler options.  If the package later
    # appears, the resolver above adds its blob/content marker and invalidates
    # the previous fingerprint.
    return ()


def _mutable_tsconfig_inputs(
    source_root: Path,
) -> tuple[list[_ConfigInput], bool]:
    """Resolve the root TypeScript config closure without leaving ``root``."""

    root_config = source_root / "tsconfig.json"
    if not os.path.lexists(root_config):
        return [], False
    pending: list[tuple[Path, int, bool]] = [(root_config, 0, True)]
    visited: dict[Path, bool] = {}
    inputs: list[_ConfigInput] = []
    budget = _ConfigResolutionBudget()
    while pending:
        lexical, depth, parse_extends = pending.pop()
        if depth > _MAX_TSCONFIG_DEPTH:
            raise ValueError("source_manifest_tsconfig_depth_exceeded")
        if _is_link_or_junction(lexical):
            raise ValueError(f"source_manifest_tsconfig_link_rejected:{lexical}")
        try:
            resolved = lexical.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"source_manifest_tsconfig_unreadable:{lexical}") from exc
        if resolved != source_root and source_root not in resolved.parents:
            raise ValueError("source_manifest_tsconfig_extends_outside_root")
        previous_parse = visited.get(resolved)
        if previous_parse is True or (previous_parse is False and not parse_extends):
            continue
        if not resolved.is_file():
            raise ValueError(f"source_manifest_tsconfig_not_file:{resolved}")
        first_visit = previous_parse is None
        visited[resolved] = bool(previous_parse or parse_extends)
        if len(visited) > _MAX_TSCONFIG_FILES:
            raise ValueError("source_manifest_tsconfig_file_limit_exceeded")
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            raise ValueError(f"source_manifest_tsconfig_unreadable:{resolved}") from exc
        identity = resolved.relative_to(source_root).as_posix()
        if first_visit:
            digest = hashlib.sha256(raw).digest()
            marker = (
                identity.encode("utf-8", errors="surrogateescape")
                + b"\0"
                + str(len(raw)).encode()
                + b"\0"
                + digest
            )
            inputs.append(_ConfigInput(identity=identity, size=len(raw), marker=marker))
        if not parse_extends:
            continue
        specifiers = _tsconfig_extends(raw, identity)
        budget.add_resolutions(len(specifiers))
        children: list[tuple[Path, bool]] = []
        for specifier in specifiers:
            for candidate, child_parse_extends in _mutable_tsconfig_candidates(
                source_root, resolved.parent, specifier, budget
            ):
                budget.add_probe()
                if os.path.lexists(candidate):
                    children.append((candidate, child_parse_extends))
                    if child_parse_extends:
                        break
        pending.extend(
            (child, depth + 1, child_parse_extends)
            for child, child_parse_extends in reversed(children)
        )
    return (
        sorted(inputs, key=lambda item: item.identity),
        budget.external_dependency,
    )


def _collapse_git_path(base: PurePosixPath, specifier: str) -> PurePosixPath:
    normalized = specifier.replace("\\", "/")
    if (
        not normalized.startswith(("./", "../"))
        or "\x00" in normalized
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise ValueError("source_manifest_tsconfig_extends_non_relative")
    parts: list[str] = []
    for part in (*base.parts, *PurePosixPath(normalized).parts):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError("source_manifest_tsconfig_extends_outside_repository")
            parts.pop()
        else:
            parts.append(part)
    return PurePosixPath(*parts)


def _git_config_path_candidates(base: PurePosixPath) -> tuple[PurePosixPath, ...]:
    if base.suffix.lower() == ".json":
        return (base,)
    return (base.with_name(base.name + ".json"), base / "tsconfig.json")


def _git_tsconfig_candidates(
    repository: Path,
    revision: str,
    cache: dict[str, tuple[bytes, bytes, bytes, int] | None],
    base: PurePosixPath,
    specifier: str,
    budget: _ConfigResolutionBudget,
) -> tuple[tuple[PurePosixPath, bool], ...]:
    normalized = specifier.replace("\\", "/")
    if normalized.startswith(("./", "../")):
        candidates = _git_config_path_candidates(
            _collapse_git_path(base, normalized)
        )
        return tuple(
            (candidate, True)
            for candidate in candidates
            if _git_tree_entry(
                repository, revision, candidate, cache, budget
            )
            is not None
        )[:1]

    package_parts, subpath_parts = _package_specifier_parts(normalized)
    ancestors = [
        PurePosixPath(*base.parts[:index])
        for index in range(len(base.parts), -1, -1)
    ]
    for ancestor in ancestors:
        package_root = ancestor.joinpath("node_modules", *package_parts)
        package_json = package_root / "package.json"
        package_entry = _git_tree_entry(
            repository, revision, package_json, cache, budget
        )
        package_inputs: list[tuple[PurePosixPath, bool]] = []
        package_field: tuple[str, ...] | None = None
        if package_entry is not None:
            package_raw = _git_config_blob(
                repository, package_json, package_entry
            )
            package_field = _package_json_tsconfig(
                package_raw, package_json.as_posix()
            )
            package_inputs.append((package_json, False))
        if subpath_parts:
            target = package_root.joinpath(*subpath_parts)
        elif package_field is not None:
            target = package_root.joinpath(*package_field)
        else:
            target = package_root / "tsconfig.json"
        for candidate in _git_config_path_candidates(target):
            if (
                _git_tree_entry(repository, revision, candidate, cache, budget)
                is not None
            ):
                return (*package_inputs, (candidate, True))
        if package_inputs:
            return tuple(package_inputs)
    return ()


def _git_tree_entry(
    repository: Path,
    revision: str,
    path: PurePosixPath,
    cache: dict[str, tuple[bytes, bytes, bytes, int] | None],
    budget: _ConfigResolutionBudget,
) -> tuple[bytes, bytes, bytes, int] | None:
    key = path.as_posix()
    if key in cache:
        return cache[key]
    budget.add_probe()
    raw = _git_output(
        repository,
        "--literal-pathspecs",
        "ls-tree",
        "-lz",
        "--full-tree",
        revision,
        "--",
        key,
    )
    found: tuple[bytes, bytes, bytes, int] | None = None
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id, raw_size = metadata.split()
            size = int(raw_size)
        except ValueError as exc:
            raise ValueError("source_manifest_git_tree_malformed") from exc
        decoded = raw_path.decode("utf-8", errors="surrogateescape")
        if decoded == key:
            found = (mode, object_type, object_id, size)
            break
    cache[key] = found
    return found


def _git_config_blob(
    repository: Path,
    path: PurePosixPath,
    entry: tuple[bytes, bytes, bytes, int],
) -> bytes:
    mode, object_type, object_id, size = entry
    key = path.as_posix()
    if mode == b"120000":
        raise ValueError(f"source_manifest_tsconfig_link_rejected:{key}")
    if object_type != b"blob":
        raise ValueError(f"source_manifest_tsconfig_not_blob:{key}")
    if size > _MAX_TSCONFIG_BYTES:
        raise ValueError(f"source_manifest_tsconfig_too_large:{key}")
    raw = _git_output(repository, "cat-file", "blob", object_id.decode("ascii"))
    if len(raw) != size:
        raise ValueError(f"source_manifest_tsconfig_blob_size_mismatch:{key}")
    return raw


def _git_tsconfig_inputs(
    repository: Path,
    revision: str,
    prefix_parts: tuple[str, ...],
    cache: dict[str, tuple[bytes, bytes, bytes, int] | None],
) -> list[_ConfigInput]:
    """Resolve TypeScript config inputs from exact Git objects."""

    prefix = PurePosixPath(*prefix_parts) if prefix_parts else PurePosixPath()
    root_config = prefix / "tsconfig.json"
    budget = _ConfigResolutionBudget()
    if _git_tree_entry(repository, revision, root_config, cache, budget) is None:
        return []
    pending: list[tuple[PurePosixPath, int, bool]] = [(root_config, 0, True)]
    visited: dict[str, bool] = {}
    inputs: list[_ConfigInput] = []
    while pending:
        path, depth, parse_extends = pending.pop()
        if depth > _MAX_TSCONFIG_DEPTH:
            raise ValueError("source_manifest_tsconfig_depth_exceeded")
        key = path.as_posix()
        previous_parse = visited.get(key)
        if previous_parse is True or (previous_parse is False and not parse_extends):
            continue
        entry = _git_tree_entry(repository, revision, path, cache, budget)
        if entry is None:
            continue
        _mode, _object_type, object_id, size = entry
        raw = _git_config_blob(repository, path, entry)
        first_visit = previous_parse is None
        visited[key] = bool(previous_parse or parse_extends)
        if len(visited) > _MAX_TSCONFIG_FILES:
            raise ValueError("source_manifest_tsconfig_file_limit_exceeded")
        path_parts = path.parts
        if prefix_parts and path_parts[: len(prefix_parts)] == prefix_parts:
            relative_parts = path_parts[len(prefix_parts) :]
            identity = PurePosixPath(*relative_parts).as_posix()
        elif not prefix_parts:
            identity = key
        else:
            identity = f"@repository/{key}"
        if first_visit:
            marker = (
                identity.encode("utf-8", errors="surrogateescape")
                + b"\0"
                + str(size).encode()
                + b"\0git-blob\0"
                + object_id.lower()
            )
            inputs.append(_ConfigInput(identity=identity, size=size, marker=marker))
        if not parse_extends:
            continue
        specifiers = _tsconfig_extends(raw, identity)
        budget.add_resolutions(len(specifiers))
        children: list[tuple[PurePosixPath, bool]] = []
        for specifier in specifiers:
            children.extend(
                _git_tsconfig_candidates(
                    repository,
                    revision,
                    cache,
                    path.parent,
                    specifier,
                    budget,
                )
            )
        pending.extend(
            (child, depth + 1, child_parse_extends)
            for child, child_parse_extends in reversed(children)
        )
    return sorted(inputs, key=lambda item: item.identity)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _producer_runtime_marker(analyzer: str) -> str:
    """Fingerprint the executable implementation, including availability."""

    if analyzer.startswith("agent:"):
        from app.extractor.agent_sdk import is_agent_sdk_available

        return f"agent-sdk:{int(is_agent_sdk_available())}"
    resolved = shutil.which(analyzer)
    candidate = Path(resolved) if resolved else inrepo_script(analyzer)
    if candidate is None or not candidate.is_file():
        return "unavailable"
    try:
        parts = [candidate.resolve().as_posix(), _hash_file(candidate)]
        if analyzer == "ggoss-ts":
            lock = candidate.parent.parent / "package-lock.json"
            if lock.is_file():
                parts.append(_hash_file(lock))
        return ":".join(parts)
    except OSError:
        return "unreadable"


def _git_output(repository: Path, *args: str) -> bytes:
    """Run a bounded, read-only Git query or fail closed.

    Exact-SHA manifests are a correctness boundary: silently falling back to
    the checked-out bytes after a malformed tree response could compare two
    different source snapshots and incorrectly authorize an incremental skip.
    """

    executable = shutil.which("git")
    if executable is None:
        raise ValueError("source_manifest_git_unavailable")
    try:
        completed = subprocess.run(
            [executable, "-C", str(repository), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("source_manifest_git_timeout") from exc
    except OSError as exc:
        raise ValueError("source_manifest_git_unavailable") from exc
    if completed.returncode != 0:
        # Git stderr can contain credential-bearing remotes and host paths.
        # The manifest consumer needs only the closed failure category.
        raise ValueError("source_manifest_git_failed")
    return completed.stdout


def _git_has_visible_node_modules(
    repository: Path,
    revision: str,
    prefix_parts: tuple[str, ...],
) -> bool:
    """Check exact tree entries visible from a TypeScript source subtree."""

    if len(prefix_parts) > _MAX_TSCONFIG_DEPTH:
        # Avoid an unbounded command line for an adversarially deep prefix.
        return True
    candidates = tuple(
        (
            PurePosixPath(*prefix_parts[:depth]) / "node_modules"
        ).as_posix()
        for depth in range(len(prefix_parts), -1, -1)
    )
    return bool(
        _git_output(
            repository,
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            "--full-tree",
            revision,
            "--",
            *candidates,
        )
    )


def _normalize_tree_prefix(tree_prefix: str) -> tuple[str, tuple[str, ...]]:
    raw = tree_prefix.replace("\\", "/").strip("/")
    if not raw:
        return "", ()
    if any(character in raw for character in ("\x00", "\r", "\n")):
        raise ValueError("source_manifest_git_tree_prefix_invalid")
    path = PurePosixPath(raw)
    parts = path.parts
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("source_manifest_git_tree_prefix_invalid")
    return path.as_posix(), parts


def _verify_git_snapshot(
    source_root: Path,
    revision: str,
    tree_prefix: str,
) -> tuple[str, tuple[str, ...]]:
    """Prove that the analyzer checkout and requested Git tree are identical."""

    if not _GIT_OBJECT_ID.fullmatch(revision):
        raise ValueError("source_manifest_git_revision_not_exact")
    normalized_prefix, prefix_parts = _normalize_tree_prefix(tree_prefix)
    checkout = _git_output(
        source_root,
        "rev-parse",
        "--show-prefix",
        "--verify",
        "HEAD^{commit}",
    ).decode("utf-8", errors="surrogateescape").splitlines()
    if len(checkout) != 2 or not _GIT_OBJECT_ID.fullmatch(checkout[1]):
        raise ValueError("source_manifest_git_checkout_identity_malformed")
    checkout_prefix, checkout_head = checkout[0].strip("/"), checkout[1]
    if checkout_head.lower() != revision.lower():
        raise ValueError("source_manifest_git_checkout_revision_mismatch")
    if checkout_prefix != normalized_prefix:
        raise ValueError("source_manifest_git_tree_prefix_mismatch")
    dirty = _git_output(
        source_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored",
        "--",
        ".",
    )
    if dirty:
        raise ValueError("source_manifest_git_checkout_dirty")
    return normalized_prefix, prefix_parts


def build_source_manifest(
    root: str | Path,
    languages: Iterable[str],
    *,
    producer_options: dict[str, Any] | None = None,
    git_repository: str | Path | None = None,
    git_revision: str | None = None,
    tree_prefix: str = "",
    trusted_git_checkout: bool = False,
) -> SourceManifest:
    """Fingerprint relevant analyzer inputs beneath ``root``.

    Immutable Git snapshots use tree metadata (blob OID, path, and size), so an
    incremental no-op does not re-read every source body.  Direct callers get
    a HEAD/subtree/dirty-worktree proof before that metadata is trusted.
    ``trusted_git_checkout`` is reserved for the fresh detached worktree made
    by :func:`prepare_source`; it removes fixed Git-process overhead without
    weakening arbitrary-directory calls.  Mutable/non-Git directories retain
    byte hashing plus the final-run recheck.
    """

    requested_root = Path(root).expanduser()
    if _is_link_or_junction(requested_root):
        raise ValueError(f"source_path_must_not_be_link: {requested_root}")
    source_root = requested_root.resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError(f"source_path_not_directory: {source_root}")
    if (git_repository is None) != (git_revision is None):
        raise ValueError("source_manifest_git_options_incomplete")
    if git_revision is None and tree_prefix:
        raise ValueError("source_manifest_git_tree_prefix_without_revision")
    if git_revision is None and trusted_git_checkout:
        raise ValueError("source_manifest_trusted_git_without_revision")

    grouped = analyzer_extensions(languages, producer_options=producer_options)
    digesters: dict[str, Any] = {}
    file_counts = {key: 0 for key in grouped}
    total_bytes = {key: 0 for key in grouped}
    observed_paths = {key: set() for key in grouped}
    non_cacheable_analyzers: set[str] = set()
    if "ggoss-csharp" in grouped:
        # MSBuildWorkspace resolves SDKs, NuGet packages and imported props /
        # targets beyond the project tree. Until that complete import graph is
        # captured as an immutable input closure, a C# fingerprint may aid
        # diagnostics but must never authorize an incremental skip.
        non_cacheable_analyzers.add("ggoss-csharp")
    if "ggoss-ts" in grouped and any(
        (ancestor / "node_modules").is_dir() for ancestor in source_root.parents
    ):
        # TypeScript automatically consumes visible @types packages from
        # enclosing node_modules directories. Those files are outside the
        # source discovery contract, so their presence disables authoritative
        # incremental reuse instead of pretending they cannot affect symbols.
        non_cacheable_analyzers.add("ggoss-ts")
    suffix_owners: dict[str, set[str]] = {}
    filename_owners: dict[str, set[str]] = {}
    for analyzer, suffixes in grouped.items():
        h = hashlib.sha256()
        h.update(f"{INDEX_CONTRACT_VERSION}\0{analyzer}\0".encode())
        runtime_marker = _producer_runtime_marker(analyzer)
        h.update(runtime_marker.encode("utf-8", errors="replace"))
        h.update(b"\0")
        if analyzer.startswith("agent:") or runtime_marker == "unavailable":
            h.update(
                json.dumps(
                    producer_options or {}, sort_keys=True, separators=(",", ":")
                ).encode()
            )
            h.update(b"\0")
        digesters[analyzer] = h
        for suffix in suffixes:
            suffix_owners.setdefault(suffix, set()).add(analyzer)
        for filename in _EXTRA_FILENAMES.get(analyzer, frozenset()):
            filename_owners.setdefault(filename, set()).add(analyzer)

    if git_revision is not None and git_repository is not None:
        repository = Path(git_repository).expanduser().resolve(strict=True)
        if trusted_git_checkout:
            if not _GIT_OBJECT_ID.fullmatch(git_revision):
                raise ValueError("source_manifest_git_revision_not_exact")
            normalized_prefix, prefix_parts = _normalize_tree_prefix(tree_prefix)
        else:
            normalized_prefix, prefix_parts = _verify_git_snapshot(
                source_root, git_revision, tree_prefix
            )
        if "ggoss-ts" in grouped and _git_has_visible_node_modules(
            repository, git_revision, prefix_parts
        ):
            non_cacheable_analyzers.add("ggoss-ts")
        tree_args = [
            "ls-tree",
            "-rlz",
            "--full-tree",
            git_revision,
            "--",
        ]
        if normalized_prefix:
            tree_args.append(normalized_prefix)
        raw_tree = _git_output(repository, *tree_args)
        git_entry_cache: dict[str, tuple[bytes, bytes, bytes, int] | None] = {}
        for entry in raw_tree.split(b"\0"):
            if not entry:
                continue
            try:
                metadata, raw_path = entry.split(b"\t", 1)
                mode, object_type, object_id, raw_size = metadata.split()
            except ValueError as exc:
                raise ValueError("source_manifest_git_tree_malformed") from exc
            # A symlink blob hashes its target text, not analyzer-visible source.
            # Submodules are ``commit`` entries and their content is outside the
            # proven tree, so neither may authorize analyzer coverage.
            if mode == b"120000" or object_type != b"blob":
                continue
            try:
                size = int(raw_size)
            except ValueError as exc:
                raise ValueError("source_manifest_git_tree_size_invalid") from exc
            full_path = raw_path.decode("utf-8", errors="surrogateescape")
            git_entry_cache[full_path] = (mode, object_type, object_id, size)
            full_parts = PurePosixPath(full_path).parts
            if prefix_parts:
                if full_parts[: len(prefix_parts)] != prefix_parts:
                    raise ValueError("source_manifest_git_tree_path_escape")
                relative_parts = full_parts[len(prefix_parts) :]
            else:
                relative_parts = full_parts
            if "ggoss-ts" in grouped and "node_modules" in relative_parts:
                non_cacheable_analyzers.add("ggoss-ts")
            if not relative_parts or any(
                part in {"", ".", ".."} for part in relative_parts
            ):
                raise ValueError("source_manifest_git_tree_path_invalid")
            filename = relative_parts[-1]
            owners = set(
                suffix_owners.get(PurePosixPath(filename).suffix.lower(), set())
            )
            owners.update(filename_owners.get(filename, set()))
            owners = {
                analyzer
                for analyzer in owners
                if _path_allowed(analyzer, relative_parts)
            }
            if not owners:
                continue
            relative = PurePosixPath(*relative_parts).as_posix()
            marker = (
                relative.encode("utf-8", errors="surrogateescape")
                + b"\0"
                + str(size).encode()
                + b"\0git-blob\0"
                + object_id.lower()
            )
            for analyzer in owners:
                digesters[analyzer].update(marker)
                file_counts[analyzer] += 1
                total_bytes[analyzer] += size
                observed_paths[analyzer].add(relative)
        if "ggoss-ts" in digesters:
            config_inputs = _git_tsconfig_inputs(
                repository,
                git_revision,
                prefix_parts,
                git_entry_cache,
            )
            for config_input in config_inputs:
                if config_input.identity in observed_paths["ggoss-ts"]:
                    continue
                digesters["ggoss-ts"].update(config_input.marker)
                file_counts["ggoss-ts"] += 1
                total_bytes["ggoss-ts"] += config_input.size
                observed_paths["ggoss-ts"].add(config_input.identity)
    else:
        for current, directories, filenames in os.walk(
            source_root, followlinks=False
        ):
            current_path = Path(current)
            current_parts = current_path.relative_to(source_root).parts
            if "ggoss-ts" in grouped and "node_modules" in directories:
                non_cacheable_analyzers.add("ggoss-ts")
            directories[:] = sorted(
                directory
                for directory in directories
                if not _is_link_or_junction(current_path / directory)
                and any(
                    _path_allowed(
                        analyzer, (*current_parts, directory, "__probe__")
                    )
                    for analyzer in grouped
                )
            )
            for filename in sorted(filenames):
                path = current_path / filename
                if _is_link_or_junction(path) or not path.is_file():
                    continue
                owners = set(suffix_owners.get(path.suffix.lower(), set()))
                owners.update(filename_owners.get(filename, set()))
                relative_parts = (*current_parts, filename)
                owners = {
                    analyzer
                    for analyzer in owners
                    if _path_allowed(analyzer, relative_parts)
                }
                if not owners:
                    continue
                relative = path.relative_to(source_root).as_posix()
                try:
                    size = path.stat().st_size
                    content_digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        while chunk := handle.read(1024 * 1024):
                            content_digest.update(chunk)
                    marker = (
                        relative.encode("utf-8", errors="surrogateescape")
                        + b"\0"
                        + str(size).encode()
                        + b"\0"
                        + content_digest.digest()
                    )
                except OSError:
                    # Do not silently reuse an old graph when a relevant file is
                    # unreadable.  The error marker changes the fingerprint and
                    # lets the analyzer surface the actual coverage failure.
                    size = 0
                    marker = (
                        relative.encode("utf-8", errors="surrogateescape")
                        + b"\0unreadable"
                    )
                for analyzer in owners:
                    digesters[analyzer].update(marker)
                    file_counts[analyzer] += 1
                    total_bytes[analyzer] += size
                    observed_paths[analyzer].add(relative)
        if "ggoss-ts" in digesters:
            config_inputs, has_external_dependency = _mutable_tsconfig_inputs(
                source_root
            )
            if has_external_dependency:
                non_cacheable_analyzers.add("ggoss-ts")
            for config_input in config_inputs:
                if config_input.identity in observed_paths["ggoss-ts"]:
                    continue
                digesters["ggoss-ts"].update(config_input.marker)
                file_counts["ggoss-ts"] += 1
                total_bytes["ggoss-ts"] += config_input.size
                observed_paths["ggoss-ts"].add(config_input.identity)

    return SourceManifest(
        fingerprints={key: value.hexdigest() for key, value in digesters.items()},
        file_counts=file_counts,
        total_bytes=total_bytes,
        non_cacheable_analyzers=frozenset(non_cacheable_analyzers),
    )


def changed_analyzers(
    previous_stats: dict[str, Any] | None,
    current: SourceManifest,
) -> tuple[set[str], set[str]]:
    """Return ``(changed_or_new, removed)`` analyzer identities.

    Older runs have no manifest.  They intentionally trigger one authoritative
    pass; only a prior snapshot with the same contract can authorize a skip.
    """

    payload = (previous_stats or {}).get("source_manifest") or {}
    if payload.get("contract_version") != INDEX_CONTRACT_VERSION:
        return set(current.fingerprints), set()
    previous = payload.get("fingerprints")
    if not isinstance(previous, dict):
        return set(current.fingerprints), set()
    coverage = (previous_stats or {}).get("producer_coverage") or {}
    previous_non_cacheable = payload.get("non_cacheable_analyzers") or []
    if not isinstance(previous_non_cacheable, list):
        return set(current.fingerprints), set()
    previous_non_cacheable_set = {
        str(analyzer) for analyzer in previous_non_cacheable
    }
    changed = {
        analyzer
        for analyzer, digest in current.fingerprints.items()
        if analyzer in current.non_cacheable_analyzers
        or analyzer in previous_non_cacheable_set
        or previous.get(analyzer) != digest
        or not isinstance(coverage.get(analyzer), dict)
        or coverage[analyzer].get("authoritative") is not True
    }
    removed = set(previous).difference(current.fingerprints)
    return changed, removed
