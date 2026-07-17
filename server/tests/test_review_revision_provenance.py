"""Gate-B side effects are authorized by one canonical graph revision."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api import break_glass as break_glass_api
from app.api import diffs as diffs_api
from app.gitlab_client.mr import MRResult
from app.graph_publication import GraphPublicationInvariantError
from app.models import all_models as _all_models  # noqa: F401
from app.models.auth import User
from app.models.base import Base
from app.models.graph import AnalysisRun, GraphHead
from app.models.organization import Organization
from app.models.plans import DiffBreakGlassGrant, DiffSubmission, Plan
from app.models.projects import Project
from app.safety.review.pipeline import PassResult, ReviewReport
from app.safety.tokens import hash_token
from app.review_revision import lock_review_graph_revision
from app.testing.graph_publication import published_run_fields
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


@pytest_asyncio.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _postgres_url() -> str | None:
    raw = os.environ.get("MNEMOS_TEST_POSTGRES_URL") or os.environ.get(
        "DATABASE_URL", ""
    )
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgres://")
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgresql://")
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql+"):
        return "postgresql+asyncpg://" + raw.split("://", 1)[1]
    return None


@pytest_asyncio.fixture
async def postgres_database():
    url = _postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    root_engine = create_async_engine(url)
    schema = f"mnemos_review_revision_{uuid.uuid4().hex}"
    schema_engine: AsyncEngine | None = None
    try:
        try:
            async with root_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        except (OSError, ConnectionError):
            pytest.skip("PostgreSQL service is unavailable")
        schema_engine = root_engine.execution_options(
            schema_translate_map={None: schema}
        )
        async with schema_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(schema_engine, expire_on_commit=False)
    finally:
        if schema_engine is not None:
            await schema_engine.dispose()
        try:
            async with root_engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        except (OSError, ConnectionError):
            pass
        await root_engine.dispose()


def _clean_report() -> ReviewReport:
    return ReviewReport(verdict="clean", passes=[], findings=[])


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    submission_status: str | None = None,
    bind_submission: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, User, Plan, DiffSubmission | None]:
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    published_at = datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc)
    async with factory() as session:
        organization = Organization(
            id=organization_id,
            slug=f"review-revision-{organization_id.hex}",
            display_name="Review revision tests",
        )
        session.add(organization)
        await session.flush()
        user = User(
            id=uuid.uuid4(),
            username=f"reviewer-{organization_id.hex}",
            password_hash="test",
            role="admin",
            organization_id=organization_id,
        )
        session.add(user)
        session.add(
            Project(
                id=project_id,
                name=f"review-revision-{project_id.hex}",
                gitlab_project_id=project_id.int % 2_000_000_000,
                gitlab_url="https://example.invalid/review-revision",
                default_branch="main",
                languages=["python"],
                organization_id=organization_id,
            )
        )
        await session.flush()
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test:review-revision",
                git_sha="a" * 40,
                scope="full",
                started_at=published_at - timedelta(seconds=2),
                completed_at=published_at,
                **published_run_fields(
                    generation=1,
                    published_at=published_at,
                    coverage_sealed_at=published_at - timedelta(seconds=1),
                ),
            )
        )
        await session.flush()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=1,
                overlay_generation=0,
                state="ready",
                published_at=published_at,
            )
        )
        plan = Plan(
            id=uuid.uuid4(),
            project_id=project_id,
            status="approved",
            spec={"title": "revision-bound change"},
            tasks=[],
            requester="user:test",
            worktree_path=str(Path("/tmp/review-revision")),
            source_run_id=run_id,
            source_git_sha="a" * 40,
            source_graph_generation=1,
            source_overlay_generation=0,
        )
        session.add(plan)
        await session.flush()
        submission = None
        if submission_status is not None:
            submission = DiffSubmission(
                id=uuid.uuid4(),
                plan_id=plan.id,
                task_id="task-1",
                status=submission_status,
                diff="--- a/x.py\n+++ b/x.py\n",
                auto_review_findings=[],
            )
            if bind_submission:
                submission.review_run_id = run_id
                submission.review_source_generation = 1
                submission.review_overlay_generation = 0
                submission.review_git_sha = "a" * 40
            session.add(submission)
        await session.commit()
        return project_id, run_id, organization_id, user, plan, submission


async def _noop_audit(**_kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_break_glass_refuses_clean_but_incomplete_rerun(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project_id, _run_id, _, user, _plan, submission = await _seed(
        database,
        submission_status="blocked",
    )
    assert submission is not None

    async def incomplete_pipeline(*_args, **_kwargs) -> ReviewReport:
        return ReviewReport(
            verdict="clean",
            passes=[
                PassResult(
                    name="second_opinion",
                    position=1,
                    coverage="incomplete",
                    error="second_opinion_terminal_without_product",
                )
            ],
            findings=[],
        )

    monkeypatch.setattr(
        break_glass_api,
        "run_pipeline",
        incomplete_pipeline,
    )
    async with database() as session:
        with pytest.raises(HTTPException) as exc_info:
            await break_glass_api.issue_break_glass_grant(
                submission.id,
                break_glass_api.BreakGlassRequest(rationale="r" * 200),
                session,
                user,
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "rerun_review_incomplete"


async def _fake_mr(*_args, **_kwargs) -> MRResult:
    return MRResult(ok=True, iid=7, url="https://example.invalid/mr/7", message="ok")


@pytest.mark.asyncio
async def test_submit_diff_persists_exact_canonical_review_revision(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, run_id, _, user, plan, _ = await _seed(database)

    async def clean_pipeline(*_args, **_kwargs) -> ReviewReport:
        return _clean_report()

    async def valid_worktree(*_args, **_kwargs) -> Path:
        return Path(plan.worktree_path or "")

    async def reviewed_diff(_plan_id: uuid.UUID) -> str:
        return "--- a/x.py\n+++ b/x.py\n"

    monkeypatch.setattr(diffs_api, "run_pipeline", clean_pipeline)
    monkeypatch.setattr(diffs_api, "audit_record", _noop_audit)
    monkeypatch.setattr(diffs_api, "verify_worktree_revision", valid_worktree)
    monkeypatch.setattr(diffs_api, "compute_diff", reviewed_diff)
    monkeypatch.setattr(
        diffs_api,
        "worktree_path",
        lambda _plan_id: Path(plan.worktree_path or ""),
    )
    async with database() as session:
        result = await diffs_api.submit_diff(
            diffs_api.DiffSubmit(
                plan_id=plan.id,
                task_id="task-1",
                diff="--- a/x.py\n+++ b/x.py\n",
            ),
            db=session,
            user=user,
        )

    assert result.review_run_id == run_id
    assert result.review_source_generation == 1
    assert result.review_overlay_generation == 0
    assert result.review_git_sha == "a" * 40
    async with database() as session:
        stored = await session.get(DiffSubmission, result.id)
        assert stored is not None
        assert stored.review_run_id == run_id
        assert stored.review_source_generation == 1
        assert stored.review_overlay_generation == 0
        assert stored.review_git_sha == "a" * 40
        head = await session.get(GraphHead, project_id)
        assert head is not None
        assert head.generation == 1


@pytest.mark.asyncio
async def test_submit_diff_rejects_graph_change_during_review_without_row(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, _, _, user, plan, _ = await _seed(database)

    async def moving_pipeline(session: AsyncSession, **_kwargs) -> ReviewReport:
        head = await session.get(GraphHead, project_id)
        assert head is not None
        head.overlay_generation = 1
        await session.flush()
        return _clean_report()

    monkeypatch.setattr(diffs_api, "run_pipeline", moving_pipeline)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await diffs_api.submit_diff(
                diffs_api.DiffSubmit(
                    plan_id=plan.id,
                    task_id="task-1",
                    diff="--- a/x.py\n+++ b/x.py\n",
                ),
                db=session,
                user=user,
            )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "graph_revision_changed_during_review"
    async with database() as session:
        count = await session.scalar(select(func.count()).select_from(DiffSubmission))
        assert count == 0
        head = await session.get(GraphHead, project_id)
        assert head is not None
        assert head.overlay_generation == 0


@pytest.mark.asyncio
async def test_review_revision_rejects_noncanonical_git_sha(database) -> None:
    project_id, run_id, _, _, _, _ = await _seed(database)
    async with database() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        run.git_sha = "A" * 40
        await session.commit()

    async with database() as session:
        with pytest.raises(
            GraphPublicationInvariantError,
            match="full lowercase hexadecimal git_sha",
        ):
            await lock_review_graph_revision(session, project_id=project_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_approve_rejects_legacy_or_stale_verdict_before_mr(
    database,
    monkeypatch: pytest.MonkeyPatch,
    bound: bool,
) -> None:
    project_id, _, _, user, _, submission = await _seed(
        database,
        submission_status="pending_approval",
        bind_submission=bound,
    )
    assert submission is not None
    async with database() as session:
        if bound:
            head = await session.get(GraphHead, project_id)
            stored_plan = await session.get(Plan, submission.plan_id)
            assert head is not None and stored_plan is not None
            head.overlay_generation = 1
            stored_plan.source_overlay_generation = 1
            await session.commit()

    mr_calls = 0

    async def count_mr(*_args, **_kwargs) -> MRResult:
        nonlocal mr_calls
        mr_calls += 1
        return await _fake_mr()

    monkeypatch.setattr(diffs_api, "create_mr_from_worktree", count_mr)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await diffs_api.approve_submission(
                submission.id,
                diffs_api.ApproveBody(),
                db=session,
                user=user,
            )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "review_graph_revision_stale"
    assert mr_calls == 0


@pytest.mark.asyncio
async def test_approve_current_verdict_checks_worktree_then_creates_mr(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, user, plan, submission = await _seed(
        database,
        submission_status="pending_approval",
    )
    assert submission is not None and plan.worktree_path is not None
    verified: list[tuple[uuid.UUID, str]] = []

    async def valid_worktree(plan_id: uuid.UUID, git_sha: str) -> Path:
        verified.append((plan_id, git_sha))
        return Path(plan.worktree_path)

    async def reviewed_diff(_plan_id: uuid.UUID) -> str:
        return submission.diff

    monkeypatch.setattr(diffs_api, "create_mr_from_worktree", _fake_mr)
    monkeypatch.setattr(diffs_api, "compute_diff", reviewed_diff)
    monkeypatch.setattr(diffs_api, "verify_worktree_revision", valid_worktree)
    monkeypatch.setattr(
        diffs_api,
        "worktree_path",
        lambda _plan_id: Path(plan.worktree_path),
    )
    monkeypatch.setattr(diffs_api, "audit_record", _noop_audit)
    async with database() as session:
        result = await diffs_api.approve_submission(
            submission.id,
            diffs_api.ApproveBody(),
            db=session,
            user=user,
        )

    assert result.status == "approved"
    assert result.gitlab_mr_iid == 7
    assert verified == [(plan.id, "a" * 40)]


@pytest.mark.asyncio
async def test_approve_rejects_worktree_diff_changed_after_review_before_mr(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, user, _, submission = await _seed(
        database,
        submission_status="pending_approval",
    )
    assert submission is not None
    mr_calls = 0

    async def changed_diff(_plan_id: uuid.UUID) -> str:
        return submission.diff + "+unreviewed mutation\n"

    async def count_mr(*_args, **_kwargs) -> MRResult:
        nonlocal mr_calls
        mr_calls += 1
        return await _fake_mr()

    monkeypatch.setattr(diffs_api, "compute_diff", changed_diff)
    monkeypatch.setattr(diffs_api, "create_mr_from_worktree", count_mr)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await diffs_api.approve_submission(
                submission.id,
                diffs_api.ApproveBody(),
                db=session,
                user=user,
            )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "worktree_diff_changed_since_review"
    assert mr_calls == 0
    async with database() as session:
        stored = await session.get(DiffSubmission, submission.id)
        assert stored is not None and stored.status == "pending_approval"


@pytest.mark.asyncio
async def test_approve_late_mr_payload_change_is_409_and_rolls_back_grant(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_id, _, user, plan, submission = await _seed(
        database,
        submission_status="blocked",
    )
    assert submission is not None and plan.worktree_path is not None
    token = "late-payload-break-glass-token"
    async with database() as session:
        grant = DiffBreakGlassGrant(
            submission_id=submission.id,
            token_hash=hash_token(token),
            issued_by="user:different-admin",
            rationale="r" * 200,
            rerun_review_payload={"verdict": "clean"},
            review_run_id=run_id,
            review_source_generation=1,
            review_overlay_generation=0,
            review_git_sha="a" * 40,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=15),
        )
        session.add(grant)
        await session.commit()
        grant_id = grant.id

    async def valid_worktree(*_args, **_kwargs) -> Path:
        return Path(plan.worktree_path or "")

    async def reviewed_diff(_plan_id: uuid.UUID) -> str:
        return submission.diff

    async def late_payload_change(*_args, **_kwargs) -> MRResult:
        raise diffs_api.MRPayloadChanged("late worktree mutation")

    monkeypatch.setattr(diffs_api, "create_mr_from_worktree", late_payload_change)
    monkeypatch.setattr(diffs_api, "compute_diff", reviewed_diff)
    monkeypatch.setattr(diffs_api, "verify_worktree_revision", valid_worktree)
    monkeypatch.setattr(
        diffs_api,
        "worktree_path",
        lambda _plan_id: Path(plan.worktree_path or ""),
    )
    monkeypatch.setattr(diffs_api, "audit_record", _noop_audit)

    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await diffs_api.approve_submission(
                submission.id,
                diffs_api.ApproveBody(break_glass_token=token),
                db=session,
                user=user,
            )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "worktree_diff_changed_since_review"

    async with database() as session:
        stored_submission = await session.get(DiffSubmission, submission.id)
        stored_grant = await session.get(DiffBreakGlassGrant, grant_id)
        assert stored_submission is not None
        assert stored_submission.status == "blocked"
        assert stored_grant is not None
        assert stored_grant.consumed_at is None
        assert stored_grant.consumed_by is None


@pytest.mark.asyncio
async def test_break_glass_rerun_binds_submission_and_grant_to_same_revision(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_id, _, user, _, submission = await _seed(
        database,
        submission_status="blocked",
    )
    assert submission is not None

    async def clean_pipeline(*_args, **_kwargs) -> ReviewReport:
        return _clean_report()

    monkeypatch.setattr(break_glass_api, "run_pipeline", clean_pipeline)
    monkeypatch.setattr(break_glass_api, "audit_record", _noop_audit)
    async with database() as session:
        response = await break_glass_api.issue_break_glass_grant(
            submission.id,
            break_glass_api.BreakGlassRequest(rationale="r" * 200),
            db=session,
            user=user,
        )

    async with database() as session:
        stored_submission = await session.get(DiffSubmission, submission.id)
        grant = await session.get(DiffBreakGlassGrant, response.grant_id)
        assert stored_submission is not None
        assert grant is not None
        assert stored_submission.review_run_id == run_id == grant.review_run_id
        assert stored_submission.review_source_generation == 1
        assert grant.review_source_generation == 1
        assert stored_submission.review_overlay_generation == 0
        assert grant.review_overlay_generation == 0
        assert stored_submission.review_git_sha == grant.review_git_sha == "a" * 40
        assert isinstance(stored_submission.auto_review_findings, list)


@pytest.mark.asyncio
async def test_approve_rejects_stale_break_glass_grant_before_mr(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, run_id, _, user, _, submission = await _seed(
        database,
        submission_status="blocked",
    )
    assert submission is not None
    token = "stale-break-glass-token"
    async with database() as session:
        head = await session.get(GraphHead, project_id)
        stored_submission = await session.get(DiffSubmission, submission.id)
        stored_plan = await session.get(Plan, submission.plan_id)
        assert (
            head is not None
            and stored_submission is not None
            and stored_plan is not None
        )
        head.overlay_generation = 1
        stored_plan.source_overlay_generation = 1
        stored_submission.review_overlay_generation = 1
        session.add(
            DiffBreakGlassGrant(
                submission_id=submission.id,
                token_hash=hash_token(token),
                issued_by="user:different-admin",
                rationale="r" * 200,
                rerun_review_payload={"verdict": "clean"},
                review_run_id=run_id,
                review_source_generation=1,
                review_overlay_generation=0,
                review_git_sha="a" * 40,
                expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=15),
            )
        )
        await session.commit()

    mr_calls = 0

    async def count_mr(*_args, **_kwargs) -> MRResult:
        nonlocal mr_calls
        mr_calls += 1
        return await _fake_mr()

    monkeypatch.setattr(diffs_api, "create_mr_from_worktree", count_mr)
    async with database() as session:
        with pytest.raises(HTTPException) as error:
            await diffs_api.approve_submission(
                submission.id,
                diffs_api.ApproveBody(break_glass_token=token),
                db=session,
                user=user,
            )
    assert error.value.status_code == 409
    assert "stale" in str(error.value.detail)
    assert mr_calls == 0
    async with database() as session:
        grant = (
            await session.execute(
                select(DiffBreakGlassGrant).where(
                    DiffBreakGlassGrant.submission_id == submission.id
                )
            )
        ).scalar_one()
        assert grant.consumed_at is None


@pytest.mark.asyncio
async def test_approve_consumes_only_current_break_glass_grant(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_id, _, user, plan, submission = await _seed(
        database,
        submission_status="blocked",
    )
    assert submission is not None and plan.worktree_path is not None
    token = "current-break-glass-token"
    async with database() as session:
        grant = DiffBreakGlassGrant(
            submission_id=submission.id,
            token_hash=hash_token(token),
            issued_by="user:different-admin",
            rationale="r" * 200,
            rerun_review_payload={"verdict": "clean"},
            review_run_id=run_id,
            review_source_generation=1,
            review_overlay_generation=0,
            review_git_sha="a" * 40,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=15),
        )
        session.add(grant)
        await session.commit()
        grant_id = grant.id

    async def valid_worktree(*_args, **_kwargs) -> Path:
        return Path(plan.worktree_path)

    async def reviewed_diff(_plan_id: uuid.UUID) -> str:
        return submission.diff

    monkeypatch.setattr(diffs_api, "create_mr_from_worktree", _fake_mr)
    monkeypatch.setattr(diffs_api, "compute_diff", reviewed_diff)
    monkeypatch.setattr(diffs_api, "verify_worktree_revision", valid_worktree)
    monkeypatch.setattr(
        diffs_api,
        "worktree_path",
        lambda _plan_id: Path(plan.worktree_path),
    )
    monkeypatch.setattr(diffs_api, "audit_record", _noop_audit)
    async with database() as session:
        result = await diffs_api.approve_submission(
            submission.id,
            diffs_api.ApproveBody(break_glass_token=token),
            db=session,
            user=user,
        )

    assert result.status == "approved"
    async with database() as session:
        stored_grant = await session.get(DiffBreakGlassGrant, grant_id)
        assert stored_grant is not None
        assert stored_grant.consumed_at is not None
        assert stored_grant.consumed_by == f"user:{user.id}"


@pytest.mark.asyncio
async def test_postgres_approval_holds_graph_head_until_mr_decision_commit(
    postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publisher/overlay writer cannot cross the approval side effect."""

    factory = postgres_database
    project_id, _, _, user, plan, submission = await _seed(
        factory,
        submission_status="pending_approval",
    )
    assert submission is not None and plan.worktree_path is not None
    mr_started = asyncio.Event()
    release_mr = asyncio.Event()
    head_advanced = asyncio.Event()

    async def blocking_mr(*_args, **_kwargs) -> MRResult:
        mr_started.set()
        await release_mr.wait()
        return await _fake_mr()

    async def valid_worktree(*_args, **_kwargs) -> Path:
        return Path(plan.worktree_path)

    async def reviewed_diff(_plan_id: uuid.UUID) -> str:
        return submission.diff

    monkeypatch.setattr(diffs_api, "create_mr_from_worktree", blocking_mr)
    monkeypatch.setattr(diffs_api, "compute_diff", reviewed_diff)
    monkeypatch.setattr(diffs_api, "verify_worktree_revision", valid_worktree)
    monkeypatch.setattr(
        diffs_api,
        "worktree_path",
        lambda _plan_id: Path(plan.worktree_path),
    )
    monkeypatch.setattr(diffs_api, "audit_record", _noop_audit)

    async def approve() -> None:
        async with factory() as session:
            await diffs_api.approve_submission(
                submission.id,
                diffs_api.ApproveBody(),
                db=session,
                user=user,
            )

    async def advance_head() -> None:
        await mr_started.wait()
        async with factory() as session:
            head = (
                await session.execute(
                    select(GraphHead)
                    .where(GraphHead.project_id == project_id)
                    .with_for_update()
                )
            ).scalar_one()
            head.overlay_generation += 1
            await session.commit()
        head_advanced.set()

    approval_task = asyncio.create_task(approve())
    await asyncio.wait_for(mr_started.wait(), timeout=10)
    advance_task = asyncio.create_task(advance_head())
    await asyncio.sleep(0.1)
    assert not head_advanced.is_set(), "head update crossed Gate-B's lock"

    release_mr.set()
    await asyncio.wait_for(
        asyncio.gather(approval_task, advance_task),
        timeout=10,
    )

    async with factory() as session:
        stored_submission = await session.get(DiffSubmission, submission.id)
        head = await session.get(GraphHead, project_id)
        assert stored_submission is not None
        assert stored_submission.status == "approved"
        assert stored_submission.review_overlay_generation == 0
        assert head is not None and head.overlay_generation == 1


def test_migration_0033_is_chained_and_downgrade_is_fail_closed() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0033_review_revision_provenance.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "0033_review_revision_provenance"' in migration
    assert 'down_revision = "0032_plan_revision_provenance"' in migration
    assert "fk_diff_submissions_review_run" in migration
    assert "fk_diff_break_glass_review_run" in migration
    assert "cannot downgrade 0033: Gate-B review provenance exists" in migration
