"""Source discovery must stay inside the real, visible checkout tree."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_PYTHON_ANALYZERS = [
    ("ggoss-py/src/ggoss_py.py", "_iter_py_files", ".py"),
    ("ggoss-cpp/src/ggoss_cpp.py", "_iter_files", ".cpp"),
    ("ggoss-java/src/ggoss_java.py", "_iter_files", ".java"),
    ("ggoss-kotlin/src/ggoss_kotlin.py", "_iter_files", ".kt"),
    ("ggoss-web/src/ggoss_web.py", "_iter_files", ".html"),
    ("ggoss-treesitter/src/ggoss_treesitter.py", "_iter_files", ".go"),
]
_TS_ANALYZER = _ROOT / "analyzers" / "ggoss-ts" / "src" / "index.mjs"
_TS_DEPENDENCY = _ROOT / "analyzers" / "ggoss-ts" / "node_modules" / "typescript"
_NODE = shutil.which("node")


def _load_analyzer(relative: str) -> ModuleType:
    path = _ROOT / "analyzers" / relative
    module_name = "_mnemos_discovery_" + path.parent.parent.name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules while executing.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _iterator(
    relative: str, function_name: str, extension: str
) -> Callable[[Path], Iterable[Path]]:
    function = getattr(_load_analyzer(relative), function_name)
    if relative.startswith("ggoss-web/"):
        return lambda root: function(root, {extension})
    return function


class _JunctionLike:
    """Duck-typed stand-in because Python <3.12 cannot inspect junctions."""

    def is_symlink(self) -> bool:
        return False

    def is_junction(self) -> bool:
        return True


@pytest.mark.parametrize("relative", [item[0] for item in _PYTHON_ANALYZERS])
def test_python_analyzer_link_helper_recognizes_windows_junctions(
    relative: str,
) -> None:
    module = _load_analyzer(relative)

    assert module._is_link_or_junction(_JunctionLike()) is True


def test_manifest_link_helper_recognizes_windows_junctions() -> None:
    from app.orchestrator.source_manifest import _is_link_or_junction

    assert _is_link_or_junction(_JunctionLike()) is True


@pytest.mark.parametrize(
    ("relative", "function_name", "extension"),
    _PYTHON_ANALYZERS,
)
def test_python_analyzers_prune_all_hidden_directories(
    tmp_path: Path,
    relative: str,
    function_name: str,
    extension: str,
) -> None:
    visible = tmp_path / f"visible{extension}"
    hidden = tmp_path / ".tool-cache" / f"cached{extension}"
    visible.write_text("source\n", encoding="utf-8")
    hidden.parent.mkdir()
    hidden.write_text("cached\n", encoding="utf-8")

    found = list(_iterator(relative, function_name, extension)(tmp_path))

    assert [path.name for path in found] == [visible.name]


@pytest.mark.parametrize(
    ("relative", "function_name", "extension"),
    _PYTHON_ANALYZERS,
)
def test_python_analyzers_do_not_follow_file_directory_or_root_symlinks(
    tmp_path: Path,
    relative: str,
    function_name: str,
    extension: str,
) -> None:
    source_root = tmp_path / "source"
    outside_root = tmp_path / "outside"
    source_root.mkdir()
    outside_root.mkdir()
    visible = source_root / f"visible{extension}"
    outside = outside_root / f"outside{extension}"
    visible.write_text("source\n", encoding="utf-8")
    outside.write_text("outside\n", encoding="utf-8")

    linked_file = source_root / f"linked{extension}"
    linked_directory = source_root / "linked-directory"
    linked_root = tmp_path / "linked-root"
    try:
        linked_file.symlink_to(outside)
        linked_directory.symlink_to(outside_root, target_is_directory=True)
        linked_root.symlink_to(outside_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"this host cannot create test symlinks: {exc}")

    iterator = _iterator(relative, function_name, extension)
    found = list(iterator(source_root))

    assert [path.name for path in found] == [visible.name]
    assert list(iterator(linked_root)) == []


def _typescript_inventory(target: Path) -> list[str]:
    completed = subprocess.run(
        [_NODE, str(_TS_ANALYZER), "inventory", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)["files"]


@pytest.mark.skipif(
    _NODE is None or not _TS_DEPENDENCY.exists(),
    reason="node or the TypeScript analyzer dependency is unavailable",
)
def test_typescript_analyzer_prunes_hidden_directories(tmp_path: Path) -> None:
    (tmp_path / "visible.ts").write_text(
        "export const ok = 1;\n", encoding="utf-8"
    )
    hidden = tmp_path / ".npm-cache" / "cached.ts"
    hidden.parent.mkdir()
    hidden.write_text("export const cached = 1;\n", encoding="utf-8")

    assert _typescript_inventory(tmp_path) == ["visible.ts"]


@pytest.mark.skipif(
    _NODE is None or not _TS_DEPENDENCY.exists(),
    reason="node or the TypeScript analyzer dependency is unavailable",
)
def test_typescript_analyzer_does_not_follow_symlinks(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    outside_root = tmp_path / "outside"
    source_root.mkdir()
    outside_root.mkdir()
    (source_root / "visible.ts").write_text(
        "export const ok = 1;\n", encoding="utf-8"
    )
    outside = outside_root / "outside.ts"
    outside.write_text("export const outside = 1;\n", encoding="utf-8")
    try:
        (source_root / "linked.ts").symlink_to(outside)
        (source_root / "linked-directory").symlink_to(
            outside_root, target_is_directory=True
        )
        linked_root = tmp_path / "linked-root"
        linked_root.symlink_to(outside_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"this host cannot create test symlinks: {exc}")

    assert _typescript_inventory(source_root) == ["visible.ts"]
    assert _typescript_inventory(linked_root) == []


def test_agent_discovery_prunes_hidden_directories(tmp_path: Path) -> None:
    from app.extractor.agent_extract import discover_source_files

    visible = tmp_path / "visible.cpp"
    hidden = tmp_path / ".uv-cache" / "cached.cpp"
    visible.write_text("int visible;\n", encoding="utf-8")
    hidden.parent.mkdir()
    hidden.write_text("int cached;\n", encoding="utf-8")

    assert discover_source_files(str(tmp_path), "cpp", limit=10) == [visible]


def test_agent_discovery_does_not_follow_symlinks(tmp_path: Path) -> None:
    from app.extractor.agent_extract import discover_source_files

    source_root = tmp_path / "source"
    outside_root = tmp_path / "outside"
    source_root.mkdir()
    outside_root.mkdir()
    visible = source_root / "visible.cpp"
    outside = outside_root / "outside.cpp"
    visible.write_text("int visible;\n", encoding="utf-8")
    outside.write_text("int outside;\n", encoding="utf-8")
    try:
        (source_root / "linked.cpp").symlink_to(outside)
        (source_root / "linked-directory").symlink_to(
            outside_root, target_is_directory=True
        )
        linked_root = tmp_path / "linked-root"
        linked_root.symlink_to(outside_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"this host cannot create test symlinks: {exc}")

    assert discover_source_files(str(source_root), "cpp", limit=10) == [visible]
    assert discover_source_files(str(linked_root), "cpp", limit=10) == []
