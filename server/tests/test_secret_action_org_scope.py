"""Secret UUID actions are exact-org, fail-closed authorization boundaries."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import secrets as api
from app.models.auth import Secret
from app.models.base import Base
from app.models.organization import Organization
from app.models.projects import Project
from app.secret_scope import (
    SecretScopeNotFound,
    resolve_org_secret,
    resolve_project_secret,
)
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


class _Result:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _Session:
    def __init__(self, *results: _Result):
        self._results = list(results)
        self.commits = 0
        self.deleted: list[object] = []

    async def execute(self, _statement):
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def refresh(self, _row):
        pass

    async def delete(self, row):
        self.deleted.append(row)


def _user(org_id: uuid.UUID | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        role="admin",
        organization_id=org_id if org_id is not None else uuid.uuid4(),
    )


def _secret(org_id: uuid.UUID):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=org_id,
        label="original",
        kind="db_connection",
        ciphertext=b"old-ciphertext",
        iv=b"old-iv",
        created_at=datetime.now(tz=timezone.utc),
        last_tested_at=None,
        last_test_result=None,
    )


@pytest.mark.asyncio
async def test_same_org_update_succeeds(monkeypatch):
    org_id = uuid.uuid4()
    user = _user(org_id)
    secret = _secret(org_id)
    session = _Session(_Result(secret))

    monkeypatch.setattr(api, "encrypt", lambda _value: (b"new-ct", b"new-iv"))

    async def _audit(**_kwargs):
        return None

    monkeypatch.setattr(api, "audit_record", _audit)
    result = await api.update_secret(
        secret.id,
        api.SecretUpdate(label="renamed", value="replacement"),
        session,
        user,
    )

    assert result.label == "renamed"
    assert (secret.ciphertext, secret.iv) == (b"new-ct", b"new-iv")
    assert session.commits == 1


@pytest.mark.asyncio
async def test_orgless_user_cannot_create_orphan_secret(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        api,
        "encrypt",
        lambda _value: calls.append("encrypt"),
    )
    user = SimpleNamespace(
        id=uuid.uuid4(),
        role="admin",
        organization_id=None,
    )

    with pytest.raises(HTTPException) as raised:
        await api.create_secret(
            api.SecretCreate(label="orphan", kind="other", value="value"),
            _Session(),
            user,
        )

    assert (raised.value.status_code, raised.value.detail) == (
        403,
        "organization_required",
    )
    assert calls == []


@pytest.mark.asyncio
async def test_cross_org_update_is_404_and_leaves_secret_unchanged():
    user = _user(uuid.uuid4())
    foreign = _secret(uuid.uuid4())
    session = _Session(_Result(None))

    with pytest.raises(HTTPException) as raised:
        await api.update_secret(
            foreign.id,
            api.SecretUpdate(label="hijacked", value="replacement"),
            session,
            user,
        )

    assert (raised.value.status_code, raised.value.detail) == (404, "not_found")
    assert (foreign.label, foreign.ciphertext, foreign.iv) == (
        "original",
        b"old-ciphertext",
        b"old-iv",
    )
    assert session.commits == 0


@pytest.mark.asyncio
async def test_cross_org_delete_is_404_and_does_not_delete():
    user = _user(uuid.uuid4())
    foreign = _secret(uuid.uuid4())
    session = _Session(_Result(None))

    with pytest.raises(HTTPException) as raised:
        await api.delete_secret(foreign.id, session, user)

    assert (raised.value.status_code, raised.value.detail) == (404, "not_found")
    assert session.deleted == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_cross_org_test_never_decrypts_or_probes(monkeypatch):
    user = _user(uuid.uuid4())
    foreign = _secret(uuid.uuid4())
    session = _Session(_Result(None))
    calls: list[str] = []

    def _decrypt(_ciphertext, _iv):
        calls.append("decrypt")
        return "db://foreign"

    async def _probe(_kind, _plaintext):
        calls.append("probe")
        raise AssertionError("cross-org Secret reached network probe")

    monkeypatch.setattr(api, "decrypt", _decrypt)
    monkeypatch.setattr(api, "probe_tcp", _probe)
    with pytest.raises(HTTPException) as raised:
        await api.test_secret(foreign.id, session, user)

    assert (raised.value.status_code, raised.value.detail) == (404, "not_found")
    assert calls == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_missing_cross_org_and_null_owner_are_indistinguishable():
    secret_id = uuid.uuid4()
    errors: list[tuple[int, str]] = []
    for user, session in (
        (_user(uuid.uuid4()), _Session(_Result(None))),  # missing
        (_user(uuid.uuid4()), _Session(_Result(None))),  # cross-org
        (SimpleNamespace(id=uuid.uuid4(), role="admin", organization_id=None), _Session()),
    ):
        with pytest.raises(HTTPException) as raised:
            await api._secret_in_user_org(session, user, secret_id)
        errors.append((raised.value.status_code, raised.value.detail))

    assert errors == [(404, "not_found")] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver", [resolve_org_secret, resolve_project_secret])
async def test_secret_scope_resolvers_refresh_identity_map_on_lock(resolver):
    class _CaptureSession:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result(_secret(uuid.uuid4()))

    session = _CaptureSession()
    if resolver is resolve_org_secret:
        await resolver(
            session,
            organization_id=uuid.uuid4(),
            secret_id=uuid.uuid4(),
            for_update=True,
        )
    else:
        await resolver(
            session,
            project_id=uuid.uuid4(),
            secret_id=uuid.uuid4(),
            for_update=True,
        )

    assert session.statement is not None
    assert session.statement.get_execution_options()["populate_existing"] is True
    assert session.statement._for_update_arg is not None


@pytest.mark.asyncio
async def test_secret_scope_predicates_against_real_sqlite():
    """Real SQL proves foreign/NULL/missing rows cannot satisfy either resolver."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    Organization.__table__,
                    Project.__table__,
                    Secret.__table__,
                ],
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    org_a = Organization(
        id=uuid.uuid4(),
        slug=f"org-a-{uuid.uuid4().hex[:8]}",
        display_name="Org A",
    )
    org_b = Organization(
        id=uuid.uuid4(),
        slug=f"org-b-{uuid.uuid4().hex[:8]}",
        display_name="Org B",
    )
    project_a = Project(
        id=uuid.uuid4(),
        name="Project A",
        gitlab_project_id=1001,
        gitlab_url="https://gitlab.example/project-a",
        default_branch="main",
        languages=["python"],
        organization_id=org_a.id,
    )
    secret_a = Secret(
        id=uuid.uuid4(),
        label=f"secret-a-{uuid.uuid4().hex}",
        kind="db_connection",
        ciphertext=b"a",
        iv=b"a",
        organization_id=org_a.id,
    )
    secret_b = Secret(
        id=uuid.uuid4(),
        label=f"secret-b-{uuid.uuid4().hex}",
        kind="db_connection",
        ciphertext=b"b",
        iv=b"b",
        organization_id=org_b.id,
    )
    secret_null = Secret(
        id=uuid.uuid4(),
        label=f"secret-null-{uuid.uuid4().hex}",
        kind="db_connection",
        ciphertext=b"null",
        iv=b"null",
        organization_id=None,
    )

    try:
        async with session_factory() as session:
            session.add_all(
                [org_a, org_b, project_a, secret_a, secret_b, secret_null]
            )
            await session.commit()

            assert (
                await resolve_org_secret(
                    session,
                    organization_id=org_a.id,
                    secret_id=secret_a.id,
                )
            ).id == secret_a.id
            assert (
                await resolve_project_secret(
                    session,
                    project_id=project_a.id,
                    secret_id=secret_a.id,
                )
            ).id == secret_a.id

            for rejected_id in (secret_b.id, secret_null.id, uuid.uuid4()):
                with pytest.raises(SecretScopeNotFound):
                    await resolve_org_secret(
                        session,
                        organization_id=org_a.id,
                        secret_id=rejected_id,
                    )
                with pytest.raises(SecretScopeNotFound):
                    await resolve_project_secret(
                        session,
                        project_id=project_a.id,
                        secret_id=rejected_id,
                    )
    finally:
        await engine.dispose()
