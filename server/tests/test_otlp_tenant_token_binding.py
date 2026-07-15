"""Token-bound tenant attribution for the OTLP ingress."""

from __future__ import annotations

import importlib
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.organization import Organization
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

receiver = importlib.import_module("app.runtime_receiver.router")


_TOKEN_A = "a" * 48
_TOKEN_B = "b" * 48


@pytest_asyncio.fixture
async def org_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Organization.__table__.create)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clear_otlp_configuration(monkeypatch):
    for name in (
        "MNEMOS_OTLP_ORG_TOKENS",
        "MNEMOS_OTLP_TOKEN",
        "MNEMOS_OTLP_ORGANIZATION_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _set_map(monkeypatch, org_a: uuid.UUID, org_b: uuid.UUID | None = None) -> None:
    values = {str(org_a): _TOKEN_A}
    if org_b is not None:
        values[str(org_b)] = _TOKEN_B
    monkeypatch.setenv("MNEMOS_OTLP_ORG_TOKENS", json.dumps(values))


class _Request:
    def __init__(self) -> None:
        self.parsed = False

    async def json(self):
        self.parsed = True
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "orders"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "1" * 32,
                                    "spanId": "2" * 16,
                                    "name": "GET /orders",
                                    "kind": 2,
                                    "attributes": [],
                                }
                            ]
                        }
                    ],
                }
            ]
        }


@pytest.mark.parametrize(
    "configure",
    [
        lambda monkeypatch, org: None,
        lambda monkeypatch, org: monkeypatch.setenv("MNEMOS_OTLP_TOKEN", _TOKEN_A),
        lambda monkeypatch, org: monkeypatch.setenv(
            "MNEMOS_OTLP_ORGANIZATION_ID", str(org)
        ),
        lambda monkeypatch, org: monkeypatch.setenv(
            "MNEMOS_OTLP_ORG_TOKENS", "not-json"
        ),
        lambda monkeypatch, org: monkeypatch.setenv(
            "MNEMOS_OTLP_ORG_TOKENS",
            f'{{"{org}":"{_TOKEN_A}","{org}":"{_TOKEN_B}"}}',
        ),
        lambda monkeypatch, org: monkeypatch.setenv(
            "MNEMOS_OTLP_ORG_TOKENS",
            f'{{"{org}":"{_TOKEN_A}","{str(org).upper()}":"{_TOKEN_B}"}}',
        ),
    ],
)
def test_missing_partial_duplicate_and_malformed_configuration_rejects_all(
    monkeypatch, configure
) -> None:
    org_id = uuid.uuid4()
    configure(monkeypatch, org_id)
    with pytest.raises(HTTPException) as exc_info:
        receiver._check_otlp_bearer(f"Bearer {_TOKEN_A}")
    assert exc_info.value.status_code == 503


def test_duplicate_token_across_organizations_is_ambiguous(monkeypatch) -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    monkeypatch.setenv(
        "MNEMOS_OTLP_ORG_TOKENS",
        json.dumps({str(org_a): _TOKEN_A, str(org_b): _TOKEN_A}),
    )
    with pytest.raises(HTTPException) as exc_info:
        receiver._check_otlp_bearer(f"Bearer {_TOKEN_A}")
    assert (exc_info.value.status_code, exc_info.value.detail) == (
        503,
        "otlp_token_configuration_ambiguous",
    )


def test_short_token_and_map_plus_legacy_are_rejected(monkeypatch) -> None:
    org_id = uuid.uuid4()
    monkeypatch.setenv(
        "MNEMOS_OTLP_ORG_TOKENS",
        json.dumps({str(org_id): "too-short"}),
    )
    with pytest.raises(HTTPException, match="otlp_token_configuration_invalid"):
        receiver._check_otlp_bearer("Bearer too-short")

    monkeypatch.setenv(
        "MNEMOS_OTLP_ORG_TOKENS",
        json.dumps({str(org_id): _TOKEN_A}),
    )
    monkeypatch.setenv("MNEMOS_OTLP_TOKEN", _TOKEN_A)
    monkeypatch.setenv("MNEMOS_OTLP_ORGANIZATION_ID", str(org_id))
    with pytest.raises(HTTPException, match="otlp_token_configuration_invalid"):
        receiver._check_otlp_bearer(f"Bearer {_TOKEN_A}")


def test_legacy_pair_binds_token_to_organization(monkeypatch) -> None:
    org_id = uuid.uuid4()
    monkeypatch.setenv("MNEMOS_OTLP_TOKEN", _TOKEN_A)
    monkeypatch.setenv("MNEMOS_OTLP_ORGANIZATION_ID", str(org_id))
    assert receiver._check_otlp_bearer(f"Bearer {_TOKEN_A}") == org_id
    with pytest.raises(HTTPException, match="otlp_organization_mismatch"):
        receiver._check_otlp_bearer(
            f"Bearer {_TOKEN_A}",
            str(uuid.uuid4()),
        )


@pytest.mark.asyncio
async def test_a_token_with_b_header_is_rejected_before_body_or_buffer(
    org_session: AsyncSession, monkeypatch
) -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    org_session.add_all(
        [
            Organization(id=org_a, slug=f"a-{org_a.hex}", display_name="A"),
            Organization(id=org_b, slug=f"b-{org_b.hex}", display_name="B"),
        ]
    )
    await org_session.commit()
    _set_map(monkeypatch, org_a, org_b)
    request = _Request()
    buffer = AsyncMock()
    with patch.object(receiver, "buffer_observations", new=buffer):
        with pytest.raises(HTTPException) as exc_info:
            await receiver.receive_traces(
                request=request,
                db=org_session,
                authorization=f"Bearer {_TOKEN_A}",
                x_mnemos_organization_id=str(org_b),
            )
    assert (exc_info.value.status_code, exc_info.value.detail) == (
        403,
        "otlp_organization_mismatch",
    )
    assert request.parsed is False
    buffer.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_bound_org_is_live_checked_buffered_and_audited(
    org_session: AsyncSession, monkeypatch
) -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    org_session.add_all(
        [
            Organization(id=org_a, slug=f"a-{org_a.hex}", display_name="A"),
            Organization(id=org_b, slug=f"b-{org_b.hex}", display_name="B"),
        ]
    )
    await org_session.commit()
    _set_map(monkeypatch, org_a, org_b)
    request = _Request()
    seen_orgs: list[uuid.UUID] = []

    async def _buffer(_db, organization_id, records):
        seen_orgs.append(organization_id)
        assert records
        return len(records)

    audit = AsyncMock()
    with (
        patch.object(receiver, "buffer_observations", new=_buffer),
        patch.object(receiver, "audit_record", new=audit),
    ):
        response = await receiver.receive_traces(
            request=request,
            db=org_session,
            authorization=f"Bearer {_TOKEN_A}",
            x_mnemos_organization_id=None,
        )

    assert json.loads(response.body) == {"accepted": 1, "buffered": 1}
    assert seen_orgs == [org_a]
    assert audit.await_args.kwargs["details"]["organization_id"] == str(org_a)
    assert _TOKEN_A not in str(audit.await_args)
    assert _TOKEN_B not in str(audit.await_args)


@pytest.mark.asyncio
async def test_deleted_or_unknown_bound_org_rejected_before_payload(
    org_session: AsyncSession, monkeypatch
) -> None:
    missing_org = uuid.uuid4()
    _set_map(monkeypatch, missing_org)
    request = _Request()
    with pytest.raises(HTTPException) as exc_info:
        await receiver.receive_traces(
            request=request,
            db=org_session,
            authorization=f"Bearer {_TOKEN_A}",
            x_mnemos_organization_id=None,
        )
    assert (exc_info.value.status_code, exc_info.value.detail) == (
        403,
        "otlp_organization_unavailable",
    )
    assert request.parsed is False
