"""Security regressions for the fail-closed password-reset contract."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request, Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.passwords import hash_password, verify_password
from app.models.auth import ApiKey, User
from app.models.onboarding import PasswordResetToken
from app.models.organization import Organization
from app.safety.tokens import hash_token
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.api import auth as api_auth  # noqa: E402
from app.api import onboarding  # noqa: E402
from app.auth import sessions  # noqa: E402
from app.dashboard import router as dashboard  # noqa: E402
from app.models.base import Base  # noqa: E402


_OLD_PASSWORD = "Initial-Password-123"
_NEW_PASSWORD = "Changed-Password-456"
_NEXT_PASSWORD = "Another-Password-789"


async def _require_live_postgres():
    """Return the app DB module or skip when no PostgreSQL service is live."""

    from app import db as app_db

    if app_db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL-only concurrency contract")
    try:
        async with app_db.engine.connect() as connection:
            await connection.execute(select(1))
    except OSError as exc:
        pytest.skip(f"PostgreSQL service unavailable: {exc}")
    return app_db


@pytest_asyncio.fixture
async def sqlite_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Organization.__table__,
        User.__table__,
        ApiKey.__table__,
        PasswordResetToken.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=tables,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _user(*, disabled: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        username=f"reset-{uuid.uuid4().hex}",
        password_hash=hash_password(_OLD_PASSWORD),
        role="operator",
        disabled_at=datetime.now(tz=timezone.utc) if disabled else None,
    )


def _reset_token(
    user: User,
    raw: str,
    *,
    expires_at: datetime | None = None,
    consumed_at: datetime | None = None,
) -> PasswordResetToken:
    return PasswordResetToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_token(raw),
        expires_at=expires_at or datetime.now(tz=timezone.utc) + timedelta(hours=1),
        consumed_at=consumed_at,
    )


def _dashboard_login_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [],
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_request_is_fixed_202_and_never_mints_or_discloses_token(
    sqlite_session: AsyncSession,
) -> None:
    """Known, unknown, and disabled usernames take the identical HTTP path."""

    active = _user()
    disabled = _user(disabled=True)
    sqlite_session.add_all([active, disabled])
    await sqlite_session.commit()

    app = FastAPI()
    app.include_router(onboarding.router)
    audit = AsyncMock()
    usernames = [active.username, f"missing-{uuid.uuid4().hex}", disabled.username]

    with patch.object(onboarding, "audit_record", new=audit):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://reset.test",
        ) as client:
            responses = [
                await client.post(
                    "/api/v1/auth/reset/request",
                    json={"username": username},
                )
                for username in usernames
            ]

    assert [(response.status_code, response.json()) for response in responses] == [
        (202, {"status": "accepted"}),
        (202, {"status": "accepted"}),
        (202, {"status": "accepted"}),
    ]
    assert all("token" not in response.text.lower() for response in responses)
    assert await sqlite_session.scalar(select(func.count()).select_from(PasswordResetToken)) == 0
    await sqlite_session.refresh(active)
    assert verify_password(_OLD_PASSWORD, active.password_hash)

    assert audit.await_count == 3
    for call in audit.await_args_list:
        assert call.kwargs == {
            "actor": "anonymous",
            "action": "auth.password_reset_requested",
            "target": None,
            "details": {"delivery": "unavailable"},
        }
        assert all(username not in str(call) for username in usernames)


@pytest.mark.asyncio
async def test_missing_used_expired_and_disabled_all_return_same_invalid_response(
    sqlite_session: AsyncSession,
) -> None:
    active = _user()
    disabled = _user(disabled=True)
    used_raw = "used-" + uuid.uuid4().hex
    expired_raw = "expired-" + uuid.uuid4().hex
    disabled_raw = "disabled-" + uuid.uuid4().hex
    sqlite_session.add_all(
        [
            active,
            disabled,
            _reset_token(
                active,
                used_raw,
                consumed_at=datetime.now(tz=timezone.utc),
            ),
            _reset_token(
                active,
                expired_raw,
                expires_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1),
            ),
            _reset_token(disabled, disabled_raw),
        ]
    )
    await sqlite_session.commit()

    revoke = AsyncMock()
    invalids: list[tuple[int, str]] = []
    with patch.object(onboarding, "revoke_all_for_user", new=revoke):
        for raw in (
            "missing-" + uuid.uuid4().hex,
            used_raw,
            expired_raw,
            disabled_raw,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await onboarding.consume_password_reset(
                    body=onboarding.PasswordResetConsume(
                        token=raw,
                        new_password=_NEW_PASSWORD,
                    ),
                    db=sqlite_session,
                )
            invalids.append((exc_info.value.status_code, exc_info.value.detail))

    assert invalids == [(400, "reset_invalid_or_expired")] * 4
    revoke.assert_not_awaited()
    await sqlite_session.refresh(active)
    assert verify_password(_OLD_PASSWORD, active.password_hash)


@pytest.mark.asyncio
async def test_success_is_single_use_and_revokes_sessions_and_all_user_tokens(
    sqlite_session: AsyncSession,
) -> None:
    user = _user()
    other_user = _user()
    other_user_id = other_user.id
    raw = "primary-" + uuid.uuid4().hex
    other_raw = "other-" + uuid.uuid4().hex
    active_key = ApiKey(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_token("api-" + uuid.uuid4().hex),
        label="active",
    )
    old_revoked_at = datetime.now(tz=timezone.utc) - timedelta(days=1)
    already_revoked_key = ApiKey(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_token("api-" + uuid.uuid4().hex),
        label="already-revoked",
        revoked_at=old_revoked_at,
    )
    other_key = ApiKey(
        id=uuid.uuid4(),
        user_id=other_user.id,
        token_hash=hash_token("api-" + uuid.uuid4().hex),
        label="other-user",
    )
    sqlite_session.add_all(
        [
            user,
            other_user,
            active_key,
            already_revoked_key,
            other_key,
            _reset_token(user, raw),
            _reset_token(user, other_raw),
        ]
    )
    await sqlite_session.commit()

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    sessions._redis = fake_redis
    session_tokens = [
        await sessions.create_session(user.id),
        await sessions.create_session(user.id),
    ]
    other_session_token = await sessions.create_session(other_user.id)

    audit = AsyncMock()
    with patch.object(onboarding, "audit_record", new=audit):
        await onboarding.consume_password_reset(
            body=onboarding.PasswordResetConsume(
                token=raw,
                new_password=_NEW_PASSWORD,
            ),
            db=sqlite_session,
        )

        with pytest.raises(HTTPException) as replay:
            await onboarding.consume_password_reset(
                body=onboarding.PasswordResetConsume(
                    token=raw,
                    new_password=_NEXT_PASSWORD,
                ),
                db=sqlite_session,
            )

    assert (replay.value.status_code, replay.value.detail) == (
        400,
        "reset_invalid_or_expired",
    )
    await sqlite_session.refresh(user)
    assert verify_password(_NEW_PASSWORD, user.password_hash)
    assert not verify_password(_OLD_PASSWORD, user.password_hash)
    assert not verify_password(_NEXT_PASSWORD, user.password_hash)

    reset_rows = (
        (
            await sqlite_session.execute(
                select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(reset_rows) == 2
    assert all(row.consumed_at is not None for row in reset_rows)

    await sqlite_session.refresh(active_key)
    await sqlite_session.refresh(already_revoked_key)
    await sqlite_session.refresh(other_key)
    assert active_key.revoked_at is not None
    assert already_revoked_key.revoked_at == old_revoked_at.replace(tzinfo=None)
    assert other_key.revoked_at is None
    assert [await sessions.read_session(token) for token in session_tokens] == [
        None,
        None,
    ]
    assert await sessions.read_session(other_session_token) == other_user_id
    audit.assert_awaited_once()
    assert "token" not in str(audit.await_args).lower()
    await fake_redis.aclose()


def test_source_contract_uses_user_lock_then_conditional_update_returning() -> None:
    source = inspect.getsource(onboarding.consume_password_reset)
    user_lock = source.index(".with_for_update(of=User)")
    conditional_claim = source.index("update(PasswordResetToken)")
    assert user_lock < conditional_claim
    assert ".execution_options(populate_existing=True)" in source
    assert ".returning(PasswordResetToken.user_id)" in source
    assert "PasswordResetToken.consumed_at.is_(None)" in source
    assert "PasswordResetToken.expires_at > now" in source


def test_request_source_and_deployment_migration_are_fail_closed() -> None:
    request_source = inspect.getsource(onboarding.request_password_reset)
    for forbidden in (
        "body.username",
        "secrets.token_urlsafe",
        "PasswordResetToken(",
        "raw_token",
    ):
        assert forbidden not in request_source

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0035_invalidate_exposed_password_resets.py"
    ).read_text(encoding="utf-8")
    assert "UPDATE password_reset_tokens" in migration
    assert "WHERE consumed_at IS NULL" in migration
    assert "never make a once-invalidated bearer credential usable" in migration


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_parallel_consume_has_exactly_one_winner(monkeypatch) -> None:
    """Real PostgreSQL proves the lock/conditional-UPDATE linearization."""

    app_db = await _require_live_postgres()

    user = _user()
    raw = "parallel-" + uuid.uuid4().hex
    async with app_db.SessionLocal() as setup:
        setup.add_all([user, _reset_token(user, raw)])
        await setup.commit()

    monkeypatch.setattr(onboarding, "audit_record", AsyncMock())
    monkeypatch.setattr(onboarding, "revoke_all_for_user", AsyncMock(return_value=0))

    gate = asyncio.Event()

    async def attempt(password: str) -> str:
        await gate.wait()
        async with app_db.SessionLocal() as session:
            try:
                await onboarding.consume_password_reset(
                    body=onboarding.PasswordResetConsume(
                        token=raw,
                        new_password=password,
                    ),
                    db=session,
                )
            except HTTPException as exc:
                assert (exc.status_code, exc.detail) == (
                    400,
                    "reset_invalid_or_expired",
                )
                return "rejected"
            return "consumed"

    try:
        tasks = [
            asyncio.create_task(attempt(_NEW_PASSWORD)),
            asyncio.create_task(attempt(_NEXT_PASSWORD)),
        ]
        gate.set()
        assert sorted(await asyncio.gather(*tasks)) == ["consumed", "rejected"]
    finally:
        async with app_db.SessionLocal() as cleanup:
            await cleanup.execute(delete(User).where(User.id == user.id))
            await cleanup.commit()


@pytest.mark.parametrize(
    "login_handler",
    [api_auth.login, dashboard.login_submit],
    ids=["api", "dashboard"],
)
def test_local_password_login_holds_user_lock_through_session_issuance(
    login_handler,
) -> None:
    """The ordering contract closes reset-vs-old-password session races."""

    source = inspect.getsource(login_handler)
    lock = source.index(".with_for_update(of=User)")
    refresh = source.index(".execution_options(populate_existing=True)")
    verify = source.index("verify_password")
    create = source.index("await create_session")
    commit = source.index("await db.commit()")

    assert lock < refresh < verify < create < commit
    assert "await db.rollback()" in source
    assert "await delete_session(token)" in source


@pytest.mark.parametrize("surface", ["api", "dashboard"])
@pytest.mark.asyncio
async def test_normal_login_still_issues_cookie_and_releases_transaction(
    surface: str,
    sqlite_session: AsyncSession,
    monkeypatch,
) -> None:
    user = _user()
    sqlite_session.add(user)
    await sqlite_session.commit()

    module = api_auth if surface == "api" else dashboard
    monkeypatch.setattr(module.brute_force, "is_locked", AsyncMock(return_value=False))
    monkeypatch.setattr(module.brute_force, "clear", AsyncMock())
    monkeypatch.setattr(module, "create_session", AsyncMock(return_value="normal-token"))
    monkeypatch.setattr(module, "delete_session", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(module, "audit_record", audit)

    response = Response()
    if surface == "api":
        result = await api_auth.login(
            api_auth.LoginRequest(
                username=user.username,
                password=_OLD_PASSWORD,
            ),
            response,
            sqlite_session,
        )
        assert result.id == str(user.id)
        audit.assert_awaited_once_with(
            actor=f"user:{user.id}",
            action="auth.login",
            session=sqlite_session,
        )
    else:
        result = await dashboard.login_submit(
            _dashboard_login_request(),
            response,
            username=user.username,
            password=_OLD_PASSWORD,
            db=sqlite_session,
        )
        assert result.status_code == 303
        audit.assert_not_awaited()

    module.create_session.assert_awaited_once_with(user.id)
    module.delete_session.assert_not_awaited()
    assert (
        "normal-token" in result.headers.get("set-cookie", "")
        if surface == "dashboard"
        else "normal-token" in response.headers.get("set-cookie", "")
    )
    assert not sqlite_session.in_transaction()


@pytest.mark.parametrize("surface", ["api", "dashboard"])
@pytest.mark.asyncio
async def test_login_lockout_still_rejects_before_database_or_session(
    surface: str,
    sqlite_session: AsyncSession,
    monkeypatch,
) -> None:
    module = api_auth if surface == "api" else dashboard
    monkeypatch.setattr(module.brute_force, "is_locked", AsyncMock(return_value=True))
    create = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(module, "create_session", create)
    monkeypatch.setattr(module, "audit_record", audit)

    if surface == "api":
        with pytest.raises(HTTPException) as exc_info:
            await api_auth.login(
                api_auth.LoginRequest(
                    username="locked-user",
                    password="irrelevant",
                ),
                Response(),
                sqlite_session,
            )
        assert exc_info.value.status_code == 429
    else:
        result = await dashboard.login_submit(
            _dashboard_login_request(),
            Response(),
            username="locked-user",
            password="irrelevant",
            db=sqlite_session,
        )
        assert result.status_code == 429

    create.assert_not_awaited()
    audit.assert_awaited_once()
    assert not sqlite_session.in_transaction()


@pytest.mark.parametrize("surface", ["api", "dashboard"])
@pytest.mark.asyncio
async def test_login_commit_failure_deletes_issued_session(
    surface: str,
    sqlite_session: AsyncSession,
    monkeypatch,
) -> None:
    user = _user()
    sqlite_session.add(user)
    await sqlite_session.commit()

    module = api_auth if surface == "api" else dashboard
    monkeypatch.setattr(module.brute_force, "is_locked", AsyncMock(return_value=False))
    monkeypatch.setattr(module.brute_force, "clear", AsyncMock())
    monkeypatch.setattr(module, "create_session", AsyncMock(return_value="orphan-token"))
    cleanup = AsyncMock()
    monkeypatch.setattr(module, "delete_session", cleanup)
    monkeypatch.setattr(module, "audit_record", AsyncMock())

    async def _commit_failure() -> None:
        raise RuntimeError("forced commit failure")

    monkeypatch.setattr(sqlite_session, "commit", _commit_failure)

    with pytest.raises(RuntimeError, match="forced commit failure"):
        if surface == "api":
            await api_auth.login(
                api_auth.LoginRequest(
                    username=user.username,
                    password=_OLD_PASSWORD,
                ),
                Response(),
                sqlite_session,
            )
        else:
            await dashboard.login_submit(
                _dashboard_login_request(),
                Response(),
                username=user.username,
                password=_OLD_PASSWORD,
                db=sqlite_session,
            )

    cleanup.assert_awaited_once_with("orphan-token")
    assert not sqlite_session.in_transaction()


@pytest.mark.parametrize(
    ("surface", "failure_stage"),
    [("api", "audit"), ("api", "redis"), ("dashboard", "redis")],
)
@pytest.mark.asyncio
async def test_login_precommit_failure_rolls_back_without_live_session(
    surface: str,
    failure_stage: str,
    sqlite_session: AsyncSession,
    monkeypatch,
) -> None:
    user = _user()
    user_id = user.id
    username = user.username
    sqlite_session.add(user)
    await sqlite_session.commit()

    module = api_auth if surface == "api" else dashboard
    monkeypatch.setattr(module.brute_force, "is_locked", AsyncMock(return_value=False))
    monkeypatch.setattr(module.brute_force, "clear", AsyncMock())
    create = AsyncMock(return_value="must-not-survive")
    audit = AsyncMock()
    if failure_stage == "audit":
        audit.side_effect = RuntimeError("forced audit failure")
    else:
        create.side_effect = RuntimeError("forced Redis failure")
    monkeypatch.setattr(module, "create_session", create)
    cleanup = AsyncMock()
    monkeypatch.setattr(module, "delete_session", cleanup)
    monkeypatch.setattr(module, "audit_record", audit)

    with pytest.raises(RuntimeError, match="forced"):
        if surface == "api":
            await api_auth.login(
                api_auth.LoginRequest(
                    username=username,
                    password=_OLD_PASSWORD,
                ),
                Response(),
                sqlite_session,
            )
        else:
            await dashboard.login_submit(
                _dashboard_login_request(),
                Response(),
                username=username,
                password=_OLD_PASSWORD,
                db=sqlite_session,
            )

    if failure_stage == "audit":
        create.assert_not_awaited()
    else:
        create.assert_awaited_once_with(user_id)
    cleanup.assert_not_awaited()
    assert not sqlite_session.in_transaction()


@pytest.mark.integration
@pytest.mark.parametrize("surface", ["api", "dashboard"])
@pytest.mark.asyncio
async def test_postgres_reset_completion_blocks_old_password_session_issuance(
    surface: str,
    monkeypatch,
) -> None:
    """A reset holding User FOR UPDATE wins before either login surface.

    The login session intentionally preloads the old User identity before the
    reset.  Its observed SELECT then blocks on the reset row lock; after reset
    commit, ``populate_existing`` must refresh the identity-map object and the
    old password must fail without calling ``create_session``.
    """

    app_db = await _require_live_postgres()

    user = _user()
    user_id = user.id
    username = user.username
    raw = "login-race-" + uuid.uuid4().hex
    async with app_db.SessionLocal() as setup:
        setup.add_all([user, _reset_token(user, raw)])
        await setup.commit()

    module = api_auth if surface == "api" else dashboard
    create = AsyncMock(return_value="must-not-be-issued")
    monkeypatch.setattr(module, "create_session", create)
    monkeypatch.setattr(module, "delete_session", AsyncMock())
    monkeypatch.setattr(module, "audit_record", AsyncMock())
    monkeypatch.setattr(module.brute_force, "is_locked", AsyncMock(return_value=False))
    monkeypatch.setattr(module.brute_force, "record_failure", AsyncMock(return_value=1))
    monkeypatch.setattr(module.brute_force, "clear", AsyncMock())
    monkeypatch.setattr(onboarding, "audit_record", AsyncMock())

    reset_holds_user_lock = asyncio.Event()
    allow_reset_commit = asyncio.Event()

    async def _blocking_revoke(_user_id: uuid.UUID) -> int:
        assert _user_id == user_id
        reset_holds_user_lock.set()
        await allow_reset_commit.wait()
        return 0

    monkeypatch.setattr(onboarding, "revoke_all_for_user", _blocking_revoke)

    reset_task: asyncio.Task | None = None
    login_task: asyncio.Task | None = None
    try:
        async with (
            app_db.SessionLocal() as reset_session,
            app_db.SessionLocal() as login_session,
        ):
            stale_user = await login_session.get(User, user_id)
            assert stale_user is not None
            assert verify_password(_OLD_PASSWORD, stale_user.password_hash)

            reset_task = asyncio.create_task(
                onboarding.consume_password_reset(
                    body=onboarding.PasswordResetConsume(
                        token=raw,
                        new_password=_NEW_PASSWORD,
                    ),
                    db=reset_session,
                )
            )
            await asyncio.wait_for(reset_holds_user_lock.wait(), timeout=10)

            login_query_entered = asyncio.Event()
            original_execute = login_session.execute

            async def _observed_execute(statement, *args, **kwargs):
                login_query_entered.set()
                return await original_execute(statement, *args, **kwargs)

            monkeypatch.setattr(login_session, "execute", _observed_execute)

            async def _attempt_old_password_login() -> int:
                if surface == "api":
                    try:
                        await api_auth.login(
                            api_auth.LoginRequest(
                                username=username,
                                password=_OLD_PASSWORD,
                            ),
                            Response(),
                            login_session,
                        )
                    except HTTPException as exc:
                        return exc.status_code
                    return 200
                result = await dashboard.login_submit(
                    _dashboard_login_request(),
                    Response(),
                    username=username,
                    password=_OLD_PASSWORD,
                    db=login_session,
                )
                return result.status_code

            login_task = asyncio.create_task(_attempt_old_password_login())
            await asyncio.wait_for(login_query_entered.wait(), timeout=10)
            await asyncio.sleep(0)
            assert not login_task.done(), "login did not wait for reset's User lock"

            allow_reset_commit.set()
            await asyncio.wait_for(reset_task, timeout=10)
            assert await asyncio.wait_for(login_task, timeout=10) == 401
            create.assert_not_awaited()

        async with app_db.SessionLocal() as verify:
            changed = await verify.get(User, user_id)
            assert changed is not None
            assert verify_password(_NEW_PASSWORD, changed.password_hash)
            assert not verify_password(_OLD_PASSWORD, changed.password_hash)
    finally:
        allow_reset_commit.set()
        for task in (reset_task, login_task):
            if task is not None and not task.done():
                task.cancel()
        if reset_task is not None or login_task is not None:
            await asyncio.gather(
                *(task for task in (reset_task, login_task) if task is not None),
                return_exceptions=True,
            )
        async with app_db.SessionLocal() as cleanup_session:
            await cleanup_session.execute(delete(User).where(User.id == user_id))
            await cleanup_session.commit()
