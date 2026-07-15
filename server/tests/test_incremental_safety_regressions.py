from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "incremental-safety-regression-test-key")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.analyzers.runner import RunRecord  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import audit as _audit  # noqa: E402,F401
from app.models import auth as _auth  # noqa: E402,F401
from app.models import comments as _comments  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import onboarding as _onboarding  # noqa: E402,F401
from app.models import organization as _organization  # noqa: E402,F401
from app.models import plans as _plans  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models import runtime as _runtime  # noqa: E402,F401
from app.models import samples as _samples  # noqa: E402,F401
from app.models import stages as _stages  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun, GraphHead, Node  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.orchestrator.source_checkout import PreparedSource  # noqa: E402
from app.orchestrator.source_manifest import (  # noqa: E402
    INDEX_CONTRACT_VERSION,
    build_source_manifest,
)
from app.source_snapshot import (  # noqa: E402
    SOURCE_SNAPSHOT_CONTRACT,
    find_unpublished_refresh,
)
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[uuid.UUID, dict]] = []

    async def publish(self, run_id, event) -> None:
        self.events.append((run_id, event))


async def _install_database(
    monkeypatch,
    *,
    source_root,
    languages: list[str],
    previous_stats: dict | None,
    previous_status: str = "completed",
):
    from app.orchestrator import jobs, stages

    monkeypatch.setenv("MNEMOS_LOCAL_MODE", "1")
    monkeypatch.setenv("MNEMOS_INREPO_ANALYZERS", "1")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionLocal", Session)
    monkeypatch.setattr(stages, "SessionLocal", Session)

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(jobs, "audit_record", no_audit)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    settings = get_settings()
    monkeypatch.setattr(settings, "source_allowed_root", str(source_root))
    monkeypatch.setattr(
        settings,
        "source_project_roots",
        json.dumps({str(project_id): "."}),
    )
    baseline_at = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="incremental-safety",
                gitlab_project_id=project_id.int % 2_000_000_000,
                gitlab_url="https://example.invalid/incremental-safety",
                default_branch="main",
                languages=languages,
            )
        )
        baseline_id = uuid.uuid4() if previous_stats is not None else None
        if baseline_id is not None:
            baseline_stats = dict(previous_stats)
            if previous_status == "completed":
                baseline_stats.setdefault(
                    "graph_publication",
                    {
                        "contract_version": "mnemos.graph-publication.v1",
                        "generation": 1,
                        "base_generation": 0,
                        "published_at": baseline_at.isoformat(),
                        "authoritative_sources": ["ggoss-py"],
                        "coverage_sealed_at": baseline_at.isoformat(),
                        "counts": {
                            "nodes_inserted": 0,
                            "nodes_closed": 0,
                            "nodes_unchanged": 0,
                            "nodes_downgrade_ignored": 0,
                            "edges_inserted": 0,
                            "edges_closed": 0,
                            "edges_unchanged": 0,
                            "edges_downgrade_ignored": 0,
                            "stale_nodes_closed": 0,
                            "stale_edges_closed": 0,
                        },
                        "atomic": True,
                    },
                )
            session.add(
                AnalysisRun(
                    id=baseline_id,
                    project_id=project_id,
                    status=previous_status,
                    triggered_by="test:baseline",
                    git_sha="a" * 40,
                    scope="full",
                    started_at=baseline_at - timedelta(seconds=10),
                    completed_at=(
                        baseline_at if previous_status != "running" else None
                    ),
                    stats=baseline_stats,
                    graph_base_generation=(
                        0 if previous_status == "completed" else None
                    ),
                    graph_authoritative_sources=(
                        ["ggoss-py"]
                        if previous_status == "completed"
                        else None
                    ),
                    graph_coverage_sealed_at=(
                        baseline_at
                        if previous_status == "completed"
                        else None
                    ),
                    created_at=baseline_at - timedelta(seconds=20),
                )
            )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="queued",
                triggered_by="test:current",
                git_sha="HEAD",
                scope="incremental",
            )
        )
        await session.flush()
        ready_baseline = baseline_id is not None and previous_status == "completed"
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=baseline_id if ready_baseline else None,
                generation=1 if ready_baseline else 0,
                state="ready" if ready_baseline else "needs_rebuild",
                published_at=baseline_at if ready_baseline else None,
            )
        )
        await session.commit()
    return jobs, engine, Session, project_id, run_id, baseline_at


def _manifest_stats(manifest) -> dict:
    return {
        "source_manifest": manifest.as_dict(),
        "producer_coverage": {
            producer: {"authoritative": True, "mode": "deterministic"}
            for producer in manifest.fingerprints
        },
    }


def _authoritative_stage(jobs):
    async def run(
        _bus,
        _project_id,
        _run_id,
        language,
        verb,
        _path,
        _position,
        _totals,
        **_kwargs,
    ):
        return jobs.AnalyzerStageOutcome(
            producer=jobs.binary_for(language),
            verb=verb,
            authoritative=True,
        )

    return run


@pytest.mark.asyncio
async def test_partial_multi_producer_refresh_defers_shared_contract_sweep(
    tmp_path, monkeypatch
):
    """A last-writer owner cannot prove that an unchanged producer withdrew.

    The current row models a normalized Contract first emitted by Java and
    later overwritten by Python, so ``created_by`` retains only Python.  A
    Python-only incremental refresh must preserve it until all deterministic
    producers reconcile authoritatively.
    """

    # Availability is part of the manifest contract; keep it stable across
    # the baseline/current fingerprints made in this test.
    monkeypatch.setenv("MNEMOS_INREPO_ANALYZERS", "1")
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "Service.java").write_text(
        "class Service { int value() { return 1; } }\n", encoding="utf-8"
    )
    before = build_source_manifest(tmp_path, ["python", "java"])
    jobs, engine, Session, project_id, run_id, _ = await _install_database(
        monkeypatch,
        source_root=tmp_path,
        languages=["python", "java"],
        previous_stats=_manifest_stats(before),
    )
    async with Session() as session:
        session.add(
            Node(
                id="http.GET./shared",
                project_id=project_id,
                kind="Contract",
                data={
                    "name": "GET /shared",
                    "producer_history": ["ggoss-java", "ggoss-py"],
                },
                certainty="asserted",
                created_by=["ggoss-py"],
            )
        )
        await session.commit()

    (tmp_path / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setattr(jobs, "_run_analyzer_stage", _authoritative_stage(jobs))
    await jobs.run_ingest(
        {"progress": RecordingBus()},
        str(project_id),
        str(run_id),
        str(tmp_path),
        {"scope": "incremental"},
    )

    async with Session() as session:
        run = await session.get(AnalysisRun, run_id)
        contract = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "http.GET./shared",
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one_or_none()
    await engine.dispose()

    assert run is not None and run.status == "completed"
    assert run.stats["refresh"]["changed_analyzers"] == ["ggoss-py"]
    assert run.stats["refresh"]["deletion_sweep_sources"] == []
    assert (
        run.stats["refresh"]["deletion_sweep_deferred_reason"]
        == "all_deterministic_producers_must_refresh_for_safe_ownership"
    )
    assert contract is not None


@pytest.mark.asyncio
async def test_subtree_refresh_cannot_sweep_removed_producer(tmp_path, monkeypatch):
    (tmp_path / "old.py").write_text("VALUE = 1\n", encoding="utf-8")
    before = build_source_manifest(tmp_path, ["python"])
    jobs, engine, Session, project_id, run_id, _ = await _install_database(
        monkeypatch,
        source_root=tmp_path,
        languages=["java"],
        previous_stats=_manifest_stats(before),
    )
    (tmp_path / "old.py").unlink()
    (tmp_path / "Service.java").write_text(
        "class Service { int value() { return 1; } }\n", encoding="utf-8"
    )
    async with Session() as session:
        session.add(
            Node(
                id="http.GET./outside-subtree",
                project_id=project_id,
                kind="Contract",
                data={"name": "GET /outside-subtree"},
                certainty="asserted",
                created_by=["ggoss-py"],
            )
        )
        await session.commit()

    async def subtree_source(*_args, **_kwargs):
        return PreparedSource(
            path=tmp_path,
            authoritative_root=False,
            root_id="git:test-root",
            source_kind="manual_git",
            mutable=False,
            tree_prefix="service",
        )

    monkeypatch.setattr(jobs, "prepare_source", subtree_source)
    monkeypatch.setattr(jobs, "_run_analyzer_stage", _authoritative_stage(jobs))
    await jobs.run_ingest(
        {"progress": RecordingBus()},
        str(project_id),
        str(run_id),
        str(tmp_path),
        {"scope": "incremental"},
    )

    async with Session() as session:
        run = await session.get(AnalysisRun, run_id)
        contract = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "http.GET./outside-subtree",
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one_or_none()
    await engine.dispose()

    assert run is not None and run.status == "completed"
    assert run.stats["refresh"]["removed_analyzers"] == ["ggoss-py"]
    assert run.stats["refresh"]["deletion_sweep_sources"] == []
    assert (
        run.stats["refresh"]["deletion_sweep_deferred_reason"]
        == "source_subtree_not_authoritative"
    )
    assert contract is not None


@pytest.mark.asyncio
async def test_source_name_mismatch_makes_analyzer_stage_non_authoritative(
    tmp_path, monkeypatch
):
    from app.orchestrator import jobs

    class WrongSourceRunner:
        async def run(self, *_args, **_kwargs):
            yield RunRecord(
                stream="stdout",
                payload={
                    "record_type": "symbol",
                    "source_name": "ggoss-py-typo",
                    "data": {"id": "py:poison", "kind": "function"},
                },
            )

    class FakeStage:
        def __init__(self, *_args, **_kwargs) -> None:
            self.stats = {}
            self.partial_reason = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def increment(self, _amount=1):
            return None

        def set_stats(self, stats):
            self.stats = stats

        def mark_partial(self, reason):
            self.partial_reason = reason

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def active(_run_id):
        return None

    monkeypatch.setattr(jobs, "SessionLocal", Session)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    monkeypatch.setattr(jobs, "_ensure_run_active", active)
    monkeypatch.setattr(jobs, "analyzer_available", lambda _language: True)
    monkeypatch.setattr(jobs, "runner_for", lambda _language: WrongSourceRunner())
    totals = {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0, "errors": 0}

    outcome = await jobs._run_analyzer_stage(
        RecordingBus(),
        uuid.uuid4(),
        uuid.uuid4(),
        "python",
        "symbols",
        str(tmp_path),
        1,
        totals,
    )
    await engine.dispose()

    assert outcome.producer == "ggoss-py"
    assert outcome.authoritative is False
    assert outcome.records == 0
    assert outcome.errors == ("unexpected_or_invalid_record",)
    assert totals["symbols"] == 0
    assert totals["errors"] == 1


@pytest.mark.parametrize(
    ("prior_provenance", "current_fields", "error"),
    [
        (
            {"ref": "refs/heads/main", "tree_prefix": ""},
            {"source_ref": "refs/heads/feature", "tree_prefix": ""},
            "source_ref_mismatch",
        ),
        (
            {"ref": "refs/heads/main", "tree_prefix": ""},
            {"source_ref": "refs/heads/main", "tree_prefix": "service"},
            "source_tree_prefix_mismatch",
        ),
    ],
)
@pytest.mark.asyncio
async def test_bound_project_rejects_ref_or_tree_prefix_drift(
    tmp_path, monkeypatch, prior_provenance, current_fields, error
):
    (tmp_path / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = build_source_manifest(tmp_path, ["python"])
    prior = _manifest_stats(manifest)
    prior["source_provenance"] = {
        "kind": "manual_git",
        "root_id": "git:stable-root",
        "authoritative_root": True,
        **prior_provenance,
    }
    jobs, engine, Session, project_id, run_id, _ = await _install_database(
        monkeypatch,
        source_root=tmp_path,
        languages=["python"],
        previous_stats=prior,
    )

    async def changed_source(*_args, **_kwargs):
        return PreparedSource(
            path=tmp_path,
            authoritative_root=True,
            root_id="git:stable-root",
            source_kind="manual_git",
            mutable=False,
            **current_fields,
        )

    monkeypatch.setattr(jobs, "prepare_source", changed_source)
    await jobs.run_ingest(
        {"progress": RecordingBus()},
        str(project_id),
        str(run_id),
        str(tmp_path),
        {"scope": "incremental"},
    )

    async with Session() as session:
        run = await session.get(AnalysisRun, run_id)
    await engine.dispose()
    assert run is not None and run.status == "failed"
    assert error in (run.error_log or "")


@pytest.mark.asyncio
async def test_continuation_inherits_complete_source_baseline(tmp_path, monkeypatch):
    snapshot = {
        "contract_version": SOURCE_SNAPSHOT_CONTRACT,
        "kind": "git_commit",
        "revision": "b" * 40,
        "repository_key": "filled-after-project-id-is-known",
        "tree_prefix": "service",
        "immutable": True,
    }
    prior = {
        "source_manifest": {
            "contract_version": INDEX_CONTRACT_VERSION,
            "fingerprints": {"ggoss-py": "digest"},
        },
        "source_provenance": {
            "kind": "git_mirror",
            "root_id": "git-mirror:project",
            "ref": "refs/heads/main",
            "revision": "b" * 40,
            "authoritative_root": True,
            "mutable": False,
            "tree_prefix": "service",
        },
        "producer_coverage": {
            "ggoss-py": {"authoritative": True, "mode": "deterministic"}
        },
        "coverage": {"status": "complete", "gaps": []},
        "source_snapshot": snapshot,
    }
    jobs, engine, Session, project_id, run_id, _ = await _install_database(
        monkeypatch,
        source_root=tmp_path,
        languages=["python"],
        previous_stats=prior,
    )
    snapshot["repository_key"] = str(project_id)
    async with Session() as session:
        baseline = (
            await session.execute(
                select(AnalysisRun).where(
                    AnalysisRun.project_id == project_id,
                    AnalysisRun.status == "completed",
                )
            )
        ).scalar_one()
        baseline.stats = {
            **prior,
            "graph_publication": baseline.stats["graph_publication"],
        }
        await session.commit()

    await jobs.run_ingest(
        {"progress": RecordingBus()},
        str(project_id),
        str(run_id),
        str(tmp_path),
        {"scope": "continuation", "summarize": False},
    )

    async with Session() as session:
        run = await session.get(AnalysisRun, run_id)
    await engine.dispose()

    assert run is not None and run.status == "completed"
    assert run.git_sha == "b" * 40
    for key in (
        "source_manifest",
        "source_provenance",
        "producer_coverage",
        "coverage",
        "source_snapshot",
    ):
        assert run.stats[key] == prior[key]
    assert run.stats["refresh"] == {
        "mode": "continuation",
        "requested_mode": "continuation",
        "content_noop": True,
        "source_baseline_inherited": True,
        "deletion_sweep_sources": [],
        "deletion_sweep_deferred_reason": "continuation_does_not_index",
    }


@pytest.mark.parametrize("status", ["running", "failed", "cancelled"])
@pytest.mark.asyncio
async def test_continuation_uses_ready_head_despite_terminal_or_staged_refresh(
    tmp_path, monkeypatch, status
):
    prior = {
        "source_snapshot": {
            "contract_version": SOURCE_SNAPSHOT_CONTRACT,
            "kind": "git_commit",
            "revision": "c" * 40,
            "repository_key": "placeholder",
            "tree_prefix": "",
            "immutable": True,
        }
    }
    jobs, engine, Session, project_id, run_id, baseline_at = (
        await _install_database(
            monkeypatch,
            source_root=tmp_path,
            languages=["python"],
            previous_stats=prior,
        )
    )
    async with Session() as session:
        session.add(
            AnalysisRun(
                id=uuid.uuid4(),
                project_id=project_id,
                status=status,
                triggered_by="test:unpublished",
                git_sha="d" * 40,
                scope="incremental",
                started_at=baseline_at + timedelta(minutes=1),
                completed_at=(
                    None if status == "running" else baseline_at + timedelta(minutes=2)
                ),
                stats={"partial": True},
                created_at=baseline_at + timedelta(seconds=30),
            )
        )
        await session.commit()

    await jobs.run_ingest(
        {"progress": RecordingBus()},
        str(project_id),
        str(run_id),
        str(tmp_path),
        {"scope": "continuation", "summarize": False},
    )

    async with Session() as session:
        run = await session.get(AnalysisRun, run_id)
    await engine.dispose()
    assert run is not None and run.status == "completed"
    assert run.stats["refresh"]["mode"] == "continuation"


@pytest.mark.parametrize("with_completed_baseline", [True, False])
@pytest.mark.asyncio
async def test_failed_staged_refresh_does_not_block_next_atomic_refresh(
    tmp_path, monkeypatch, with_completed_baseline
):
    jobs, engine, Session, project_id, run_id, baseline_at = (
        await _install_database(
            monkeypatch,
            source_root=tmp_path,
            languages=["python"],
            previous_stats=({"baseline": True} if with_completed_baseline else None),
        )
    )
    async with Session() as session:
        session.add(
            AnalysisRun(
                id=uuid.uuid4(),
                project_id=project_id,
                status="failed",
                triggered_by="test:unpublished",
                git_sha="e" * 40,
                scope="incremental",
                started_at=baseline_at + timedelta(minutes=1),
                completed_at=baseline_at + timedelta(minutes=2),
                stats={"partial": True},
                created_at=baseline_at + timedelta(seconds=30),
            )
        )
        await session.commit()

    await jobs.run_ingest(
        {"progress": RecordingBus()},
        str(project_id),
        str(run_id),
        str(tmp_path),
        {"scope": "incremental"},
    )

    async with Session() as session:
        run = await session.get(AnalysisRun, run_id)
    await engine.dispose()
    assert run is not None and run.status == "completed"
    publication = run.stats.get("graph_publication") or {}
    assert publication.get("atomic") is True
    if with_completed_baseline:
        assert run.stats["refresh"]["mode"] == "incremental"
    else:
        # No trusted head: the default incremental request is promoted only
        # after a complete effective-full rebuild.
        assert run.stats["refresh"]["mode"] == "full"
        assert run.stats["refresh"]["requested_mode"] == "incremental"


@pytest.mark.asyncio
async def test_full_repair_is_allowed_and_publishes_new_clean_baseline(
    tmp_path, monkeypatch
):
    jobs, engine, Session, project_id, run_id, baseline_at = (
        await _install_database(
            monkeypatch,
            source_root=tmp_path,
            languages=[],
            previous_stats=None,
        )
    )
    async with Session() as session:
        session.add(
            AnalysisRun(
                id=uuid.uuid4(),
                project_id=project_id,
                status="failed",
                triggered_by="test:unpublished",
                git_sha="f" * 40,
                scope="incremental",
                started_at=baseline_at,
                completed_at=baseline_at + timedelta(minutes=1),
                stats={"partial": True},
                created_at=baseline_at - timedelta(seconds=1),
            )
        )
        await session.commit()

    async def prepared_source(*_args, **_kwargs):
        return PreparedSource(
            path=tmp_path,
            authoritative_root=True,
            root_id="manual:repair-root",
            source_kind="manual_directory",
            mutable=True,
        )

    monkeypatch.setattr(jobs, "prepare_source", prepared_source)
    await jobs.run_ingest(
        {"progress": RecordingBus()},
        str(project_id),
        str(run_id),
        str(tmp_path),
        {"scope": "full"},
    )

    async with Session() as session:
        run = await session.get(AnalysisRun, run_id)
        blocked = await find_unpublished_refresh(
            session,
            project_id=project_id,
            exclude_run_id=run_id,
        )
    await engine.dispose()

    assert run is not None and run.status == "completed"
    # Once the full run is the latest completed baseline, the older failed
    # mutation no longer blocks current-graph consumers.
    assert blocked is None
