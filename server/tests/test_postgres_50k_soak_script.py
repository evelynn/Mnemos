import shutil
import sys
import tempfile
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.benchmarks.postgres_50k_soak import (  # noqa: E402
    DEFAULT_FILE_COUNT,
    DEFAULT_STAGE_BATCH_SIZE,
    MAX_STAGE_BATCH_SIZE,
    _generate_source_tree,
    _normalize_postgres_url,
    _parallel_cleanup_temp_root,
    build_parser,
)


def test_soak_defaults_pin_real_50k_and_production_batch_bound() -> None:
    args = build_parser().parse_args(
        ["--database-url", "postgresql+asyncpg://localhost/mnemos"]
    )

    assert args.files == DEFAULT_FILE_COUNT == 50_000
    assert args.batch_size == DEFAULT_STAGE_BATCH_SIZE == MAX_STAGE_BATCH_SIZE == 250
    assert args.output_json is None


def test_source_generator_writes_exact_real_python_files(tmp_path: Path) -> None:
    target = tmp_path / "source"

    corpus = _generate_source_tree(target, 7)

    assert corpus.files == 7
    assert corpus.directories == 1
    assert sum(1 for _ in target.rglob("*.py")) == 7
    assert (target / "shard_0000" / "module_000006.py").read_text(
        encoding="utf-8"
    ) == "def symbol_000006():\n    return 6\n"
    assert len(corpus.content_fingerprint) == 64


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (
            "postgres://localhost/mnemos",
            "postgresql+asyncpg://localhost/mnemos",
        ),
        (
            "postgresql://localhost/mnemos",
            "postgresql+asyncpg://localhost/mnemos",
        ),
        (
            "postgresql+asyncpg://localhost/mnemos",
            "postgresql+asyncpg://localhost/mnemos",
        ),
    ],
)
def test_postgres_url_normalization(raw: str, normalized: str) -> None:
    assert _normalize_postgres_url(raw) == normalized


def test_postgres_url_rejects_non_postgres() -> None:
    with pytest.raises(ValueError, match="requires a PostgreSQL URL"):
        _normalize_postgres_url("sqlite+aiosqlite:///mnemos.db")


def test_parallel_cleanup_removes_only_verified_temp_shards() -> None:
    root = Path(tempfile.mkdtemp(prefix="mnemos-e4-50k-unit-"))
    try:
        source = root / "source"
        source.mkdir()
        for shard_index in range(4):
            shard = source / f"shard_{shard_index:04d}"
            shard.mkdir()
            for file_index in range(3):
                (shard / f"module_{file_index:06d}.py").write_text(
                    "def symbol():\n    return 1\n",
                    encoding="utf-8",
                )

        _parallel_cleanup_temp_root(root, max_workers=2)

        assert not root.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_parallel_cleanup_rejects_nested_or_unexpected_root(tmp_path: Path) -> None:
    root = tmp_path / "mnemos-e4-50k-unsafe"
    root.mkdir()

    with pytest.raises(ValueError, match="direct OS-temp child"):
        _parallel_cleanup_temp_root(root)

    assert root.exists()
