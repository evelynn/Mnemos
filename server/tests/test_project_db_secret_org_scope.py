"""ProjectDB credential references must never cross tenant boundaries."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import project_dbs as api
from app.data_sampler.probe import ProbeResult


class _Result:
    def __init__(self, *, first=None, scalar=None):
        self._first = first
        self._scalar = scalar

    def first(self):
        return self._first

    def scalar_one_or_none(self):
        return self._scalar


class _Session:
    def __init__(self, *results: _Result):
        self._results = list(results)

    async def execute(self, _statement):
        return self._results.pop(0)


def _secret(org_id: uuid.UUID):
    return SimpleNamespace(
        organization_id=org_id,
        ciphertext=b"ciphertext",
        iv=b"iv",
    )


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="admin")


@pytest.mark.asyncio
async def test_same_org_secret_can_reach_probe(monkeypatch):
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    secret_id = uuid.uuid4()
    session = _Session(_Result(scalar=_secret(org_id)))
    observed: list[tuple[str, str]] = []

    monkeypatch.setattr(api, "decrypt", lambda _ct, _iv: "db://same-org")

    async def _probe(kind: str, conn: str) -> ProbeResult:
        observed.append((kind, conn))
        return ProbeResult(
            status="pass",
            connect_ok=True,
            read_ok=True,
            write_blocked=True,
            grants_summary={},
        )

    monkeypatch.setattr(api, "probe_via_analyzer", _probe)
    result = await api._probe_or_412(session, project_id, "mssql", secret_id)

    assert result.is_acceptable()
    assert observed == [("mssql", "db://same-org")]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver_row", [None, "cross_org"])
async def test_create_hides_missing_and_cross_org_secret_before_probe(
    monkeypatch,
    resolver_row,
):
    # The exact-org SQL predicate makes both a missing id and a foreign row
    # resolve to no scalar result.
    _ = resolver_row
    session = _Session(_Result(scalar=None))
    probe_called = False

    async def _unexpected_probe(_kind: str, _conn: str):
        nonlocal probe_called
        probe_called = True
        raise AssertionError("cross-org credential reached analyzer probe")

    monkeypatch.setattr(api, "probe_via_analyzer", _unexpected_probe)
    with pytest.raises(HTTPException) as raised:
        await api.create_project_db(
            uuid.uuid4(),
            api.ProjectDBCreate(
                kind="mssql",
                display_name="db",
                component_id="db:primary",
                secret_id=uuid.uuid4(),
            ),
            session,
            _user(),
        )

    assert (raised.value.status_code, raised.value.detail) == (
        404,
        "secret_not_found",
    )
    assert not probe_called


@pytest.mark.asyncio
async def test_update_rejects_cross_org_secret_before_persist():
    project_id = uuid.uuid4()
    old_secret_id = uuid.uuid4()
    binding = SimpleNamespace(secret_id=old_secret_id)
    session = _Session(
        _Result(scalar=binding),
        _Result(scalar=None),
    )

    with pytest.raises(HTTPException) as raised:
        await api.update_project_db(
            project_id,
            uuid.uuid4(),
            api.ProjectDBUpdate(secret_id=uuid.uuid4()),
            session,
            _user(),
        )

    assert (raised.value.status_code, raised.value.detail) == (
        404,
        "secret_not_found",
    )
    assert binding.secret_id == old_secret_id


@pytest.mark.asyncio
async def test_reprobe_blocks_preexisting_cross_org_binding(monkeypatch):
    project_id = uuid.uuid4()
    binding = SimpleNamespace(secret_id=uuid.uuid4())
    session = _Session(
        _Result(scalar=binding),
        _Result(scalar=None),
    )
    probe_called = False

    async def _unexpected_probe(_kind: str, _conn: str):
        nonlocal probe_called
        probe_called = True
        raise AssertionError("contaminated credential reached analyzer probe")

    monkeypatch.setattr(api, "probe_via_analyzer", _unexpected_probe)
    with pytest.raises(HTTPException) as raised:
        await api.reprobe_project_db(
            project_id,
            uuid.uuid4(),
            session,
            _user(),
        )

    assert (raised.value.status_code, raised.value.detail) == (
        404,
        "secret_not_found",
    )
    assert not probe_called
