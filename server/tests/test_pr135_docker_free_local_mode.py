"""PR-135 — docker-free local mode end-to-end.

사용자 요청: Docker 없이도 동작하도록 구성.

이 테스트는 외부 서비스(Postgres/Redis/docker daemon) 없이
전체 플랫폼이 한 프로세스에서 동작함을 **실제로** 입증:
- SQLite (PR-118 polyglot) 로 스키마 생성
- 공유 fakeredis 로 세션/레이트리밋/진행률
- inline ARQ shim 으로 잡 실행
- httpx ASGITransport 로 실제 앱에 HTTP 요청 → login/세션/findings

모든 테스트는 in-process — harness 가 background 서버를 못 죽이고,
실제 ASGI 미들웨어/라우팅/DB/auth 를 전부 통과.
"""

from __future__ import annotations

import os
import secrets

import pytest


def _local_env(tmp_path) -> dict[str, str]:
    """docker-free env 한 벌. 각 테스트가 monkeypatch 로 적용."""
    from cryptography.fernet import Fernet

    db = tmp_path / "mnemos-local.db"
    return {
        "MNEMOS_LOCAL_MODE": "1",
        "MNEMOS_ENV": "local",
        "DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "SECRET_KEY": secrets.token_urlsafe(48),
        "FERNET_KEY": Fernet.generate_key().decode(),
        "SESSION_COOKIE_SECURE": "false",
        "MNEMOS_SKIP_STARTUP_VERIFY": "1",
    }


# ─── is_local_mode 감지 ──────────────────────────────────────────


def test_local_mode_detected_via_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_LOCAL_MODE", "1")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.local_mode import is_local_mode
    assert is_local_mode() is True


def test_local_mode_detected_via_sqlite_url(monkeypatch):
    monkeypatch.delenv("MNEMOS_LOCAL_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///x.db")
    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.setenv("SECRET_KEY", "x" * 48)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.local_mode import is_local_mode
    assert is_local_mode() is True
    get_settings.cache_clear()


# ─── fakeredis 공유 키스페이스 ──────────────────────────────────


@pytest.mark.asyncio
async def test_fakeredis_shared_keyspace():
    """get_fake_redis() 가 반환하는 별개 클라이언트들이 같은
    키스페이스를 공유 — 세션 written 이 health 에서 보임."""
    from app.local_mode import get_fake_redis

    a = get_fake_redis()
    b = get_fake_redis()
    await a.set("shared:key", "v1")
    assert await b.get("shared:key") == "v1", "fakeredis clients must share one server"


# ─── inline queue ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inline_queue_runs_job_in_process(monkeypatch):
    """enqueue_job 이 등록된 잡을 백그라운드 태스크로 실행."""
    import asyncio
    from app.local_mode import InlineQueue

    ran = []

    async def fake_job(ctx, *args):
        ran.append(args)

    q = InlineQueue()
    monkeypatch.setattr(q, "_registry", lambda: {"run_ingest": fake_job})
    job = await q.enqueue_job("run_ingest", "pid", "rid", "/path")
    assert job.job_id is None or isinstance(job.job_id, str)
    # 백그라운드 태스크가 완료되도록 양보.
    await asyncio.sleep(0.05)
    assert ran == [("pid", "rid", "/path")]


@pytest.mark.asyncio
async def test_inline_queue_dedups_job_id(monkeypatch):
    """같은 _job_id 두 번 → 한 번만 실행 (webhook 멱등성)."""
    import asyncio
    from app.local_mode import InlineQueue

    ran = []

    async def fake_job(ctx, *args):
        ran.append(args)

    q = InlineQueue()
    monkeypatch.setattr(q, "_registry", lambda: {"run_ingest": fake_job})
    first = await q.enqueue_job("run_ingest", "a", _job_id="dup")
    duplicate = await q.enqueue_job("run_ingest", "b", _job_id="dup")
    assert first is not None
    assert duplicate is None  # same contract as ARQ enqueue_job
    await asyncio.sleep(0.05)
    assert len(ran) == 1, "duplicate _job_id must run only once"


@pytest.mark.asyncio
async def test_inline_queue_unknown_job_is_safe(monkeypatch):
    from app.local_mode import InlineQueue
    q = InlineQueue()
    monkeypatch.setattr(q, "_registry", lambda: {})
    job = await q.enqueue_job("nonexistent", "x")  # must not raise
    assert job is None


@pytest.mark.asyncio
async def test_inline_queue_seen_cache_is_ttl_and_size_bounded(monkeypatch):
    import asyncio

    from app.local_mode import InlineQueue

    async def fake_job(ctx, *args):
        return None

    q = InlineQueue(seen_ttl_sec=0.01, max_seen_job_ids=2)
    monkeypatch.setattr(q, "_registry", lambda: {"run_ingest": fake_job})
    for job_id in ("one", "two", "three"):
        assert await q.enqueue_job("run_ingest", _job_id=job_id) is not None
        await asyncio.sleep(0)
    assert len(q._seen_job_ids) <= 2

    await asyncio.sleep(0.02)
    # Expired ids can be used again and pruning remains bounded.
    assert await q.enqueue_job("run_ingest", _job_id="three") is not None
    assert len(q._seen_job_ids) <= 2


@pytest.mark.asyncio
async def test_inline_queue_deferred_job_can_be_cancelled(monkeypatch):
    import asyncio

    from app.local_mode import InlineQueue

    ran = []

    async def fake_job(ctx, *args):
        ran.append(args)

    q = InlineQueue()
    monkeypatch.setattr(q, "_registry", lambda: {"run_ingest": fake_job})
    job = await q.enqueue_job(
        "run_ingest", "late", _job_id="deferred", _defer_by=60
    )
    assert job is not None
    assert await q.abort_job("deferred") is True
    await asyncio.sleep(0)
    assert ran == []


# ─── redis_pool / sessions / queue 분기 ──────────────────────────


@pytest.mark.asyncio
async def test_redis_pool_returns_fakeredis_in_local_mode(monkeypatch, tmp_path):
    for k, v in _local_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()
    # reset singleton
    import app.orchestrator.redis_pool as rp
    rp._client = None
    client = await rp.get_redis()
    assert "fakeredis" in type(client).__module__
    rp._client = None
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_queue_returns_inline_in_local_mode(monkeypatch, tmp_path):
    for k, v in _local_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.orchestrator.queue import get_queue
    from app.local_mode import InlineQueue
    q = await get_queue()
    assert isinstance(q, InlineQueue)
    get_settings.cache_clear()


# ─── worker health check: local 모드 = inline ──────────────────


@pytest.mark.asyncio
async def test_worker_check_reports_inline_in_local_mode(monkeypatch, tmp_path):
    for k, v in _local_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.api.health import _check_worker
    ok, msg = await _check_worker()
    assert ok is True
    assert msg == "inline"
    get_settings.cache_clear()


# ─── 전체 HTTP E2E (in-process ASGI, no docker) ─────────────────


# app.db 는 import 시점에 DATABASE_URL 로 engine 을 굳히므로, 전체
# suite 안에서는 이미 다른 URL 로 import 됐을 수 있다. 진짜 docker-free
# 부팅을 순서 무관하게 입증하려면 **깨끗한 subprocess** 로 돌린다 —
# serve_local 의 실제 동작 방식(새 프로세스)과 정확히 일치한다.
_E2E_SUBPROCESS = '''
import os, asyncio, json, sys, secrets
os.environ["MNEMOS_LOCAL_MODE"] = "1"
os.environ["MNEMOS_ENV"] = "local"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + sys.argv[1]
os.environ["SECRET_KEY"] = secrets.token_urlsafe(48)
from cryptography.fernet import Fernet
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["MNEMOS_SKIP_STARTUP_VERIFY"] = "1"

async def main():
    from app.local_mode import ensure_sqlite_schema
    await ensure_sqlite_schema()
    from app.seed_demo import seed_demo
    s = await seed_demo(force=True)
    user, pw = s["user"], s["password"]
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    out = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local") as c:
        out["health"] = (await c.get("/api/v1/health")).status_code
        rr = await c.get("/api/v1/health/ready")
        out["ready"] = rr.status_code
        out["worker_msg"] = rr.json()["checks"]["worker"]["message"]
        lr = await c.post("/api/v1/auth/login", json={"username": user, "password": pw})
        out["login"] = lr.status_code
        out["login_user"] = lr.json().get("username")
        me = await c.get("/api/v1/auth/me")
        out["me_user"] = me.json().get("username")
        pj = await c.get("/api/v1/projects")
        projects = pj.json()
        out["project_count"] = len(projects)
        pid = projects[0]["id"]
        fd = await c.get("/api/v1/projects/" + pid + "/findings?status=open")
        out["finding_kinds"] = sorted({f["kind"] for f in fd.json()})
    print("RESULT_JSON:" + json.dumps(out))

asyncio.run(main())
'''


def test_full_http_stack_without_docker(tmp_path):
    """SQLite + fakeredis + seed-demo + login + 세션 + findings 를
    실제 ASGI 앱으로 구동 — 외부 서비스 0. serve_local 과 동일하게
    깨끗한 subprocess 에서 (suite 순서 무관)."""
    import json
    import subprocess
    import sys

    db = tmp_path / "e2e.db"
    script = tmp_path / "e2e.py"
    script.write_text(_E2E_SUBPROCESS, encoding="utf-8")
    cp = subprocess.run(
        [sys.executable, str(script), str(db)],
        capture_output=True, text=True, timeout=120,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
    )
    assert cp.returncode == 0, f"docker-free boot failed:\n{cp.stderr[-2000:]}"
    line = next(
        ln for ln in cp.stdout.splitlines() if ln.startswith("RESULT_JSON:")
    )
    out = json.loads(line[len("RESULT_JSON:"):])
    assert out["health"] == 200
    assert out["ready"] == 200, f"ready not OK (worker={out.get('worker_msg')})"
    assert out["worker_msg"] == "inline"
    assert out["login"] == 200
    assert out["login_user"] == "demo-admin"
    assert out["me_user"] == "demo-admin"           # session via fakeredis
    assert out["project_count"] >= 1
    assert "duplicate_endpoint" in out["finding_kinds"]  # seed-demo P1


# ─── bcrypt 직접 해싱 (passlib 제거) ────────────────────────────


def test_password_hash_uses_bcrypt_directly():
    """PR-135 — passlib 의 깨진 bcrypt self-test 우회. hash/verify
    가 실제 동작하고 $2b$ 포맷 유지."""
    from app.auth.passwords import hash_password, verify_password

    h = hash_password("Mnemos-local-123")
    assert h.startswith("$2b$")
    assert verify_password("Mnemos-local-123", h) is True
    assert verify_password("wrong-pw-9999", h) is False


def test_password_verify_tolerates_malformed_hash():
    from app.auth.passwords import verify_password
    assert verify_password("x", "not-a-bcrypt-hash") is False


def test_passwords_module_does_not_import_passlib():
    """passlib 의존 제거 확인 — 깨진 self-test 가 다시 들어오지
    않도록."""
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[1] / "app" / "auth" / "passwords.py"
    ).read_text(encoding="utf-8")
    # docstring 은 passlib 을 언급해도 OK — import 만 없으면 됨.
    assert "from passlib" not in src
    assert "import passlib" not in src
    assert "import bcrypt" in src
    # 모듈이 실제로 동작하는지 (이미 import 된 함수로 런타임 증명 —
    # reload 는 다른 테스트의 함수 참조를 오염시키므로 쓰지 않음).
    from app.auth.passwords import hash_password, verify_password
    h = hash_password("Runtime-proof-1")
    assert verify_password("Runtime-proof-1", h)


# ─── serve_local 부트스트랩 env ─────────────────────────────────


def test_serve_local_bootstrap_sets_docker_free_env(monkeypatch):
    """_bootstrap_env 가 모든 docker-free 기본값을 설정."""
    # 깨끗한 슬레이트
    for k in ("MNEMOS_LOCAL_MODE", "DATABASE_URL", "SECRET_KEY",
              "FERNET_KEY", "SESSION_COOKIE_SECURE",
              "MNEMOS_DISABLE_AGENT_SDK"):
        monkeypatch.delenv(k, raising=False)
    from app.serve_local import _bootstrap_env
    _bootstrap_env("/tmp/x.db")
    assert os.environ["MNEMOS_LOCAL_MODE"] == "1"
    assert os.environ["DATABASE_URL"].startswith("sqlite+aiosqlite:///")
    assert len(os.environ["SECRET_KEY"]) >= 40
    assert os.environ["FERNET_KEY"]
    assert os.environ["SESSION_COOKIE_SECURE"] == "false"
    # PR-158 — local mode defaults the Claude Agent SDK off so analysis
    # completes fast on the zero-dependency trial path instead of stalling
    # at l1_summaries on a 60s-per-summary Claude-CLI timeout.
    assert os.environ["MNEMOS_DISABLE_AGENT_SDK"] == "1"


def test_serve_local_bootstrap_respects_agent_sdk_override(monkeypatch):
    """An operator who explicitly wants the Agent SDK in local mode can
    re-enable it — _bootstrap_env uses setdefault, so a pre-set value wins."""
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "0")
    from app.serve_local import _bootstrap_env
    _bootstrap_env("/tmp/x.db")
    assert os.environ["MNEMOS_DISABLE_AGENT_SDK"] == "0"

