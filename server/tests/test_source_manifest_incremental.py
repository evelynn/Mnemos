from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.orchestrator import source_manifest as source_manifest_mod
from app.orchestrator.source_manifest import (
    INDEX_CONTRACT_VERSION,
    build_source_manifest,
    changed_analyzers,
)


_GIT = shutil.which("git")


def _git(repository: Path, *args: str, stdin: bytes | None = None) -> str:
    completed = subprocess.run(
        [_GIT or "git", "-C", str(repository), *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8", errors="replace"
    )
    return completed.stdout.decode("ascii", errors="strict").strip()


def _git_repository(tmp_path: Path) -> tuple[Path, str]:
    if _GIT is None:
        pytest.skip("git is unavailable")
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "manifest@example.invalid")
    _git(repository, "config", "user.name", "Manifest Test")
    (repository / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "ui.ts").write_text("export const value = 1;\n", encoding="utf-8")
    hidden = repository / ".cache" / "ignored.py"
    hidden.parent.mkdir()
    hidden.write_text("HIDDEN = True\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    base = _git(repository, "rev-parse", "HEAD")

    # Cross-platform plumbing creates tree entries without requiring Windows
    # symlink privileges or an initialized submodule checkout.
    link_blob = _git(repository, "hash-object", "-w", "--stdin", stdin=b"service.py")
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"120000,{link_blob},linked.py",
    )
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{base},deps/submodule",
    )
    _git(repository, "commit", "-m", "tree-only entries")
    _git(repository, "reset", "--hard", "HEAD")
    return repository, _git(repository, "rev-parse", "HEAD")


def _stats(manifest):
    return {
        "source_manifest": manifest.as_dict(),
        "producer_coverage": {
            producer: {"authoritative": True}
            for producer in manifest.fingerprints
        },
    }


def test_content_change_invalidates_only_owning_analyzer(tmp_path):
    py = tmp_path / "service.py"
    ts = tmp_path / "ui.ts"
    py.write_text("def answer(): return 1\n", encoding="utf-8")
    ts.write_text("export const answer = 1\n", encoding="utf-8")
    before = build_source_manifest(tmp_path, ["python", "typescript"])

    py.write_text("def answer(): return 2\n", encoding="utf-8")
    after = build_source_manifest(tmp_path, ["python", "typescript"])

    changed, removed = changed_analyzers(_stats(before), after)
    assert changed == {"ggoss-py"}
    assert removed == set()


def test_mtime_only_change_is_a_noop(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    before = build_source_manifest(tmp_path, ["python"])

    stat = source.stat()
    os.utime(source, (stat.st_atime + 60, stat.st_mtime + 60))
    after = build_source_manifest(tmp_path, ["python"])

    assert changed_analyzers(_stats(before), after) == (set(), set())


def test_delete_and_language_removal_are_visible(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    before = build_source_manifest(tmp_path, ["python"])

    source.unlink()
    after_delete = build_source_manifest(tmp_path, ["python"])
    assert changed_analyzers(_stats(before), after_delete)[0] == {"ggoss-py"}

    after_language_removed = build_source_manifest(tmp_path, [])
    changed, removed = changed_analyzers(_stats(before), after_language_removed)
    assert changed == set()
    assert removed == {"ggoss-py"}


def test_excluded_dependency_tree_does_not_invalidate(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    dependency = tmp_path / "node_modules" / "generated.py"
    dependency.parent.mkdir()
    dependency.write_text("VALUE = 1\n", encoding="utf-8")
    before = build_source_manifest(tmp_path, ["python"])

    dependency.write_text("VALUE = 2\n", encoding="utf-8")
    after = build_source_manifest(tmp_path, ["python"])
    assert changed_analyzers(_stats(before), after) == (set(), set())


def test_hidden_tool_cache_does_not_enter_manifest(tmp_path):
    source = tmp_path / "service.py"
    cached = tmp_path / ".uv-cache" / "cached.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    cached.parent.mkdir()
    cached.write_text("VALUE = 1\n", encoding="utf-8")

    before = build_source_manifest(tmp_path, ["python"])
    cached.write_text("VALUE = 2\n", encoding="utf-8")
    after = build_source_manifest(tmp_path, ["python"])

    assert before.file_counts == {"ggoss-py": 1}
    assert changed_analyzers(_stats(before), after) == (set(), set())


def test_exact_git_manifest_uses_tree_objects_without_reading_source_bodies(
    tmp_path, monkeypatch
):
    repository, revision = _git_repository(tmp_path)
    service = (repository / "service.py").resolve()
    # Tree size is canonical Git-blob bytes.  It can intentionally differ from
    # a CRLF-smudged Windows worktree file by one byte per line ending.
    expected_bytes = int(_git(repository, "cat-file", "-s", f"{revision}:service.py"))
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    real_open = Path.open

    def _reject_source_body(self, *args, **kwargs):  # noqa: ANN001
        if self.resolve() == service:
            raise AssertionError("exact Git manifest read a source body")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _reject_source_body)
    manifest = build_source_manifest(
        repository,
        ["python", "typescript"],
        git_repository=repository,
        git_revision=revision,
    )

    # linked.py is mode 120000, deps/submodule is mode 160000, and the tracked
    # hidden cache is excluded by the same analyzer discovery contract.
    assert manifest.file_counts == {"ggoss-py": 1, "ggoss-ts": 1}
    assert manifest.total_bytes["ggoss-py"] == expected_bytes


def test_exact_git_blob_change_invalidates_only_owning_family(tmp_path, monkeypatch):
    repository, before_revision = _git_repository(tmp_path)
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    before = build_source_manifest(
        repository,
        ["python", "typescript"],
        git_repository=repository,
        git_revision=before_revision,
    )

    (repository / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repository, "add", "service.py")
    _git(repository, "commit", "-m", "change python")
    after_revision = _git(repository, "rev-parse", "HEAD")
    after = build_source_manifest(
        repository,
        ["python", "typescript"],
        git_repository=repository,
        git_revision=after_revision,
    )

    assert changed_analyzers(_stats(before), after) == ({"ggoss-py"}, set())


def test_exact_git_manifest_fails_closed_on_revision_or_subtree_mismatch(
    tmp_path, monkeypatch
):
    repository, old_revision = _git_repository(tmp_path)
    subtree = repository / "server"
    subtree.mkdir()
    (subtree / "service.py").write_text("VALUE = 3\n", encoding="utf-8")
    (subtree / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    _git(repository, "add", "server")
    _git(repository, "commit", "-m", "add subtree")
    revision = _git(repository, "rev-parse", "HEAD")
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )

    manifest = build_source_manifest(
        subtree,
        ["python", "typescript"],
        git_repository=repository,
        git_revision=revision,
        tree_prefix="server",
    )
    assert manifest.file_counts == {"ggoss-py": 1, "ggoss-ts": 1}

    with pytest.raises(ValueError, match="checkout_revision_mismatch"):
        build_source_manifest(
            repository,
            ["python"],
            git_repository=repository,
            git_revision=old_revision,
        )
    with pytest.raises(ValueError, match="tree_prefix_mismatch"):
        build_source_manifest(
            subtree,
            ["python"],
            git_repository=repository,
            git_revision=revision,
            tree_prefix="wrong",
        )
    with pytest.raises(ValueError, match="revision_not_exact"):
        build_source_manifest(
            repository,
            ["python"],
            git_repository=repository,
            git_revision="HEAD",
        )


def test_direct_git_manifest_rejects_dirty_untracked_checkout(tmp_path, monkeypatch):
    repository, revision = _git_repository(tmp_path)
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    (repository / "untracked.py").write_text("UNTRACKED = True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checkout_dirty"):
        build_source_manifest(
            repository,
            ["python"],
            git_repository=repository,
            git_revision=revision,
        )


def test_jobs_select_git_tree_manifest_only_for_exact_immutable_checkout(tmp_path):
    from app.orchestrator.jobs import _git_manifest_options
    from app.orchestrator.source_checkout import PreparedSource

    exact = PreparedSource(
        path=tmp_path / "checkout" / "server",
        mirror=tmp_path / "mirror.git",
        revision="a" * 40,
        source_kind="git_mirror",
        mutable=False,
        tree_prefix="server",
    )
    assert _git_manifest_options(exact) == {
        "git_repository": tmp_path / "mirror.git",
        "git_revision": "a" * 40,
        "tree_prefix": "server",
        "trusted_git_checkout": True,
    }

    mutable = PreparedSource(
        path=tmp_path / "directory",
        revision=None,
        source_kind="manual_directory",
        mutable=True,
    )
    assert _git_manifest_options(mutable) == {}


def test_manifest_does_not_follow_source_symlinks(tmp_path):
    source_root = tmp_path / "source"
    outside_root = tmp_path / "outside"
    source_root.mkdir()
    outside_root.mkdir()
    (source_root / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    outside = outside_root / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    linked_root = tmp_path / "linked-root"
    try:
        (source_root / "linked.py").symlink_to(outside)
        (source_root / "linked-directory").symlink_to(
            outside_root, target_is_directory=True
        )
        linked_root.symlink_to(source_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"this host cannot create test symlinks: {exc}")

    before = build_source_manifest(source_root, ["python"])
    outside.write_text("VALUE = 2\n", encoding="utf-8")
    after = build_source_manifest(source_root, ["python"])

    assert before.file_counts == {"ggoss-py": 1}
    assert changed_analyzers(_stats(before), after) == (set(), set())
    with pytest.raises(ValueError, match="source_path_must_not_be_link"):
        build_source_manifest(linked_root, ["python"])


def test_old_contract_cannot_authorize_skip(tmp_path):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    current = build_source_manifest(tmp_path, ["python"])
    old = _stats(current)
    old["source_manifest"]["contract_version"] = INDEX_CONTRACT_VERSION + ".old"
    assert changed_analyzers(old, current)[0] == {"ggoss-py"}


def test_shared_binary_fingerprints_its_full_language_contract(tmp_path):
    html = tmp_path / "index.html"
    css = tmp_path / "site.css"
    html.write_text("<main>hello</main>", encoding="utf-8")
    css.write_text("main { color: black; }", encoding="utf-8")
    before = build_source_manifest(tmp_path, ["html"])

    css.write_text("main { color: blue; }", encoding="utf-8")
    after = build_source_manifest(tmp_path, ["html"])
    assert changed_analyzers(_stats(before), after)[0] == {"ggoss-web"}


def test_opt_in_agent_language_gets_content_fingerprint(tmp_path):
    sql = tmp_path / "schema.sql"
    sql.write_text("CREATE TABLE users(id int);", encoding="utf-8")
    before = build_source_manifest(tmp_path, ["sql"])
    sql.write_text("CREATE TABLE users(id bigint);", encoding="utf-8")
    after = build_source_manifest(tmp_path, ["sql"])

    assert set(before.fingerprints) == {"agent:sql"}
    assert changed_analyzers(_stats(before), after)[0] == {"agent:sql"}


@pytest.mark.parametrize(
    ("language", "config_name", "producer"),
    [
        ("typescript", "package.json", "ggoss-ts"),
        ("typescript", "tsconfig.json", "ggoss-ts"),
        ("csharp", "Service.csproj", "ggoss-csharp"),
        ("csharp", "Workspace.sln", "ggoss-csharp"),
    ],
)
def test_analyzer_configuration_change_invalidates_manifest(
    tmp_path, language, config_name, producer
):
    """Analyzer inputs include build/config files, not source suffixes alone."""

    config = tmp_path / config_name
    config.write_text("version=1\n", encoding="utf-8")
    before = build_source_manifest(tmp_path, [language])

    config.write_text("version=2\n", encoding="utf-8")
    after = build_source_manifest(tmp_path, [language])

    assert changed_analyzers(_stats(before), after)[0] == {producer}


def test_tsconfig_extends_change_invalidates_mutable_manifest(tmp_path, monkeypatch):
    """Compiler options inherited from a base config are analyzer inputs."""

    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    (tmp_path / "ui.ts").write_text(
        "import { value } from '@app/value';\nconsole.log(value);\n",
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text(
        '{\n  // JSONC is the TypeScript config dialect.\n  "extends": "./tsconfig.base",\n}\n',
        encoding="utf-8",
    )
    base = tmp_path / "tsconfig.base.json"
    base.write_text(
        '{"compilerOptions":{"paths":{"@app/*":["src/*"]}}}\n',
        encoding="utf-8",
    )
    before = build_source_manifest(tmp_path, ["typescript"])

    base.write_text(
        '{"compilerOptions":{"paths":{"@app/*":["lib/*"]}}}\n',
        encoding="utf-8",
    )
    after = build_source_manifest(tmp_path, ["typescript"])

    assert changed_analyzers(_stats(before), after)[0] == {"ggoss-ts"}


def test_tsconfig_extends_change_invalidates_exact_git_manifest(tmp_path, monkeypatch):
    """The Git-tree fast path must include the same config dependency closure."""

    if _GIT is None:
        pytest.skip("git is unavailable")
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "manifest@example.invalid")
    _git(repository, "config", "user.name", "Manifest Test")
    (repository / "ui.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (repository / "tsconfig.json").write_text(
        '{"extends":"./tsconfig.base"}\n', encoding="utf-8"
    )
    base = repository / "tsconfig.base.json"
    base.write_text('{"compilerOptions":{"strict":true}}\n', encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base config")
    before_revision = _git(repository, "rev-parse", "HEAD")
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    before = build_source_manifest(
        repository,
        ["typescript"],
        git_repository=repository,
        git_revision=before_revision,
    )

    base.write_text('{"compilerOptions":{"strict":false}}\n', encoding="utf-8")
    _git(repository, "add", "tsconfig.base.json")
    _git(repository, "commit", "-m", "change base config")
    after_revision = _git(repository, "rev-parse", "HEAD")
    after = build_source_manifest(
        repository,
        ["typescript"],
        git_repository=repository,
        git_revision=after_revision,
    )

    assert changed_analyzers(_stats(before), after)[0] == {"ggoss-ts"}


def test_tsconfig_package_field_and_target_enter_mutable_manifest(
    tmp_path, monkeypatch
):
    """Bare package extends hashes package.json and its selected config."""

    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    (tmp_path / "ui.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"extends":"base-config"}\n', encoding="utf-8"
    )
    package = tmp_path / "node_modules" / "base-config"
    selected = package / "configs" / "base.json"
    selected.parent.mkdir(parents=True)
    (package / "package.json").write_text(
        '{"name":"base-config","tsconfig":"configs/base.json"}\n',
        encoding="utf-8",
    )
    selected.write_text('{"compilerOptions":{"strict":true}}\n', encoding="utf-8")
    (package / "tsconfig.json").write_text(
        '{"compilerOptions":{"strict":false}}\n', encoding="utf-8"
    )
    before = build_source_manifest(tmp_path, ["typescript"])

    selected.write_text(
        '{"compilerOptions":{"strict":false,"noImplicitAny":true}}\n',
        encoding="utf-8",
    )
    target_changed = build_source_manifest(tmp_path, ["typescript"])
    assert target_changed.fingerprints != before.fingerprints

    package_manifest = package / "package.json"
    package_manifest.write_text(
        '{"name":"base-config","version":"2","tsconfig":"configs/base.json"}\n',
        encoding="utf-8",
    )
    package_changed = build_source_manifest(tmp_path, ["typescript"])
    assert package_changed.fingerprints != target_changed.fingerprints


def test_tsconfig_package_field_and_target_enter_exact_git_manifest(
    tmp_path, monkeypatch
):
    """The exact-Git resolver follows the same bare-package config closure."""

    if _GIT is None:
        pytest.skip("git is unavailable")
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "manifest@example.invalid")
    _git(repository, "config", "user.name", "Manifest Test")
    (repository / "ui.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (repository / "tsconfig.json").write_text(
        '{"extends":"base-config"}\n', encoding="utf-8"
    )
    package = repository / "node_modules" / "base-config"
    selected = package / "configs" / "base.json"
    selected.parent.mkdir(parents=True)
    (package / "package.json").write_text(
        '{"name":"base-config","tsconfig":"configs/base.json"}\n',
        encoding="utf-8",
    )
    selected.write_text('{"compilerOptions":{"strict":true}}\n', encoding="utf-8")
    (package / "tsconfig.json").write_text(
        '{"compilerOptions":{"strict":false}}\n', encoding="utf-8"
    )
    _git(repository, "add", "-f", ".")
    _git(repository, "commit", "-m", "package config")
    before_revision = _git(repository, "rev-parse", "HEAD")
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    before = build_source_manifest(
        repository,
        ["typescript"],
        git_repository=repository,
        git_revision=before_revision,
    )

    selected.write_text(
        '{"compilerOptions":{"strict":false,"noImplicitAny":true}}\n',
        encoding="utf-8",
    )
    _git(repository, "add", "-f", selected.relative_to(repository).as_posix())
    _git(repository, "commit", "-m", "change package config")
    after_revision = _git(repository, "rev-parse", "HEAD")
    after = build_source_manifest(
        repository,
        ["typescript"],
        git_repository=repository,
        git_revision=after_revision,
    )

    assert after.fingerprints != before.fingerprints
    assert changed_analyzers(_stats(before), after)[0] == {"ggoss-ts"}


@pytest.mark.parametrize(
    ("extends", "error"),
    [
        ([f"./base-{index}" for index in range(65)], "extends_limit_exceeded"),
        ("x" * 4097, "specifier_too_long"),
    ],
)
def test_tsconfig_extends_inputs_are_bounded(tmp_path, extends, error, monkeypatch):
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    (tmp_path / "ui.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"extends": extends}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match=error):
        build_source_manifest(tmp_path, ["typescript"])


def test_tsconfig_package_probes_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    monkeypatch.setattr(source_manifest_mod, "_MAX_TSCONFIG_PROBES", 1)
    (tmp_path / "ui.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"extends":"missing-config-package"}\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="tsconfig_probe_limit_exceeded"):
        build_source_manifest(tmp_path, ["typescript"])


def test_visible_node_modules_disables_typescript_incremental_skip(
    tmp_path, monkeypatch
):
    """Unhashed visible @types packages make a mutable TS run non-cacheable."""

    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    (tmp_path / "ui.ts").write_text("export const value = 1;\n", encoding="utf-8")
    declaration = tmp_path / "node_modules" / "@types" / "fixture" / "index.d.ts"
    declaration.parent.mkdir(parents=True)
    declaration.write_text("declare const fixture: string;\n", encoding="utf-8")

    before = build_source_manifest(tmp_path, ["typescript"])
    after = build_source_manifest(tmp_path, ["typescript"])

    assert before.non_cacheable_analyzers == frozenset({"ggoss-ts"})
    assert before.as_dict()["non_cacheable_analyzers"] == ["ggoss-ts"]
    assert before.fingerprints == after.fingerprints
    assert changed_analyzers(_stats(before), after)[0] == {"ggoss-ts"}


def test_csharp_msbuild_environment_is_fail_closed_for_incremental_skip(
    tmp_path, monkeypatch
):
    """Uncaptured SDK/NuGet imports cannot authorize a C# cache hit."""

    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    (tmp_path / "App.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk" />\n', encoding="utf-8"
    )
    (tmp_path / "Directory.Build.props").write_text(
        '<Project><PropertyGroup /></Project>\n', encoding="utf-8"
    )
    manifest = build_source_manifest(tmp_path, ["csharp"])

    assert manifest.non_cacheable_analyzers == frozenset({"ggoss-csharp"})
    assert manifest.file_counts["ggoss-csharp"] == 2
    assert changed_analyzers(_stats(manifest), manifest) == (
        {"ggoss-csharp"},
        set(),
    )


def test_external_package_extends_is_not_read_and_disables_mutable_skip(
    tmp_path, monkeypatch
):
    """An enclosing package is analyzer-visible but outside manifest authority."""

    workspace = tmp_path / "workspace"
    source = workspace / "project"
    source.mkdir(parents=True)
    (source / "ui.ts").write_text("export const value = 1;\n", encoding="utf-8")
    (source / "tsconfig.json").write_text(
        '{"extends":"base-config"}\n', encoding="utf-8"
    )
    package = workspace / "node_modules" / "base-config"
    selected = package / "configs" / "base.json"
    selected.parent.mkdir(parents=True)
    package_json = package / "package.json"
    package_json.write_text(
        '{"name":"base-config","tsconfig":"configs/base.json"}\n',
        encoding="utf-8",
    )
    selected.write_text('{"compilerOptions":{"strict":true}}\n', encoding="utf-8")
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )
    real_read_bytes = Path.read_bytes

    def _reject_external_read(path):  # noqa: ANN001
        if path in {package_json, selected}:
            raise AssertionError("manifest read outside its source authority")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", _reject_external_read)
    before = build_source_manifest(source, ["typescript"])

    selected.write_text('{"compilerOptions":{"strict":false}}\n', encoding="utf-8")
    after = build_source_manifest(source, ["typescript"])

    assert before.non_cacheable_analyzers == frozenset({"ggoss-ts"})
    assert before.fingerprints == after.fingerprints
    assert changed_analyzers(_stats(before), after)[0] == {"ggoss-ts"}


def test_exact_git_subtree_checks_visible_parent_node_modules(tmp_path, monkeypatch):
    """Tracked @types above tree_prefix must prevent an authoritative skip."""

    if _GIT is None:
        pytest.skip("git is unavailable")
    repository = tmp_path / "repository"
    source = repository / "app"
    source.mkdir(parents=True)
    (source / "ui.ts").write_text("export const value = 1;\n", encoding="utf-8")
    declaration = repository / "node_modules" / "@types" / "fixture" / "index.d.ts"
    declaration.parent.mkdir(parents=True)
    declaration.write_text("declare const fixture: string;\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "config", "user.email", "manifest@example.invalid")
    _git(repository, "config", "user.name", "Manifest Test")
    _git(repository, "add", "-f", ".")
    _git(repository, "commit", "-m", "subtree with parent types")
    revision = _git(repository, "rev-parse", "HEAD")
    monkeypatch.setattr(
        source_manifest_mod, "_producer_runtime_marker", lambda _producer: "runtime"
    )

    # Exercise the exact-tree proof rather than relying on the live checkout's
    # ancestor directory stat, which may be absent in a sparse materialization.
    parent_node_modules = repository / "node_modules"
    real_is_dir = Path.is_dir

    def _hide_live_parent_dependency(path):  # noqa: ANN001
        if path == parent_node_modules:
            return False
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", _hide_live_parent_dependency)
    manifest = build_source_manifest(
        source,
        ["typescript"],
        git_repository=repository,
        git_revision=revision,
        tree_prefix="app",
    )

    assert manifest.non_cacheable_analyzers == frozenset({"ggoss-ts"})
    assert changed_analyzers(_stats(manifest), manifest)[0] == {"ggoss-ts"}


def test_analyzer_runtime_change_invalidates_unchanged_sources(tmp_path, monkeypatch):
    """A new analyzer implementation must rebuild its old graph facts."""

    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        source_manifest_mod,
        "_producer_runtime_marker",
        lambda _producer: "runtime-v1",
    )
    before = build_source_manifest(tmp_path, ["python"])
    monkeypatch.setattr(
        source_manifest_mod,
        "_producer_runtime_marker",
        lambda _producer: "runtime-v2",
    )
    after = build_source_manifest(tmp_path, ["python"])

    assert changed_analyzers(_stats(before), after)[0] == {"ggoss-py"}


@pytest.mark.parametrize(
    "coverage",
    [
        {},
        {"ggoss-py": {}},
        {"ggoss-py": {"authoritative": False}},
    ],
)
def test_incomplete_previous_coverage_cannot_authorize_skip(tmp_path, coverage):
    """Equal bytes are insufficient when the prior producer was partial."""

    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    current = build_source_manifest(tmp_path, ["python"])
    previous = _stats(current)
    previous["producer_coverage"] = coverage

    assert changed_analyzers(previous, current)[0] == {"ggoss-py"}


def test_agent_extraction_options_are_part_of_manifest_contract(tmp_path):
    """Changing an Agent extraction cap cannot reuse differently bounded output."""

    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE users(id int);", encoding="utf-8"
    )
    before = build_source_manifest(
        tmp_path, ["sql"], producer_options={"agent_extract_limit": 1}
    )
    after = build_source_manifest(
        tmp_path, ["sql"], producer_options={"agent_extract_limit": 2}
    )

    assert changed_analyzers(_stats(before), after)[0] == {"agent:sql"}
