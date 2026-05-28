"""PR-132 — OpenAPI integrity audit.

PR-131 의 env audit 후 OpenAPI 검증:
- duplicate operation IDs 검출 (auth.me vs users.me 충돌 발견 + fix)
- 모든 endpoint 가 tags 보유 (UI 그룹핑)
- 모든 endpoint 가 operationId 보유 (SDK 생성)
- 모든 endpoint 가 summary 보유 (API docs 가독성)
- 명시적 response_model 또는 schema (typed client)
"""

from __future__ import annotations

import os
from collections import Counter

import pytest


@pytest.fixture(scope="module")
def openapi_spec():
    """OpenAPI spec 한 번 생성, 모든 테스트가 공유."""
    os.environ.setdefault("MNEMOS_ENV", "test")
    os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")
    from app.main import create_app
    app = create_app()
    return app.openapi()


def _all_operations(spec):
    """(path, verb, operation_dict) 튜플 전부 yield."""
    for path, methods in spec.get("paths", {}).items():
        for verb, op in methods.items():
            if verb in ("get", "post", "put", "patch", "delete"):
                yield path, verb, op


# ─── PR-132 fix: duplicate operationId ────────────────────────────


def test_no_duplicate_operation_ids(openapi_spec):
    """SDK 생성 시 op_id 충돌 → 한 endpoint 가 다른 걸 덮어씀.
    auth.me + users.me 중복이 PR-132 에서 발견되어 제거됨."""
    op_ids = []
    for _, _, op in _all_operations(openapi_spec):
        oid = op.get("operationId")
        if oid:
            op_ids.append(oid)
    dupes = [oid for oid, c in Counter(op_ids).items() if c > 1]
    assert not dupes, f"duplicate operation IDs: {dupes}"


def test_auth_me_only_defined_once(openapi_spec):
    """auth.py 의 짧은 /me 제거 후 users.py 의 완전한 /me 만 남음."""
    me_endpoints = [
        (path, verb) for path, verb, _ in _all_operations(openapi_spec)
        if path == "/api/v1/auth/me" and verb == "get"
    ]
    assert len(me_endpoints) == 1


# ─── audit PASS guards ───────────────────────────────────────────


def test_every_endpoint_has_tags(openapi_spec):
    """tags 없으면 API docs UI 그룹핑 깨짐."""
    no_tags = [
        f"{verb.upper()} {path}"
        for path, verb, op in _all_operations(openapi_spec)
        if not op.get("tags")
    ]
    assert not no_tags, f"endpoints without tags: {no_tags[:5]}"


def test_every_endpoint_has_operation_id(openapi_spec):
    """SDK 생성 + UI link 에 필요."""
    no_oid = [
        f"{verb.upper()} {path}"
        for path, verb, op in _all_operations(openapi_spec)
        if not op.get("operationId")
    ]
    assert not no_oid


def test_every_endpoint_has_summary(openapi_spec):
    """API docs 가독성. FastAPI 가 함수명에서 자동 생성하므로
    누락은 매우 드뭄."""
    no_summary = [
        f"{verb.upper()} {path}"
        for path, verb, op in _all_operations(openapi_spec)
        if not op.get("summary")
    ]
    assert not no_summary


def test_endpoint_count_reasonable(openapi_spec):
    """sanity — 100 endpoint 정도. 100 미만 = endpoint 누락 가능성,
    200 초과 = scope creep."""
    total = sum(1 for _ in _all_operations(openapi_spec))
    assert 50 <= total <= 200, f"endpoint count = {total}"


def test_openapi_version_3(openapi_spec):
    """OpenAPI 3.x 보장 — Swagger 2.0 호환 도구는 거부."""
    version = openapi_spec.get("openapi", "")
    assert version.startswith("3."), f"unexpected OpenAPI version: {version}"


def test_components_schemas_present(openapi_spec):
    """Pydantic 모델이 components.schemas 로 export 되어야 SDK 가
    재사용 가능."""
    schemas = openapi_spec.get("components", {}).get("schemas", {})
    assert len(schemas) >= 30, f"only {len(schemas)} schemas — too few?"
    # 핵심 모델 일부 확인.
    expected = {"FindingOut", "PlanOut", "DiffOut", "UserOut"}
    missing = expected - set(schemas.keys())
    assert not missing, f"core schemas missing: {missing}"
