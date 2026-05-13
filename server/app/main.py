from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import analysis as analysis_api
from app.api import artifacts as artifacts_api
from app.api import audit as audit_api
from app.api import auth as auth_api
from app.api import break_glass as break_glass_api
from app.api import data as data_api
from app.api import diffs as diffs_api
from app.api import findings as findings_api
from app.api import gdpr as gdpr_api
from app.api import health as health_api
from app.api import organizations as organizations_api
from app.api import plans as plans_api
from app.api import project_dbs as project_dbs_api
from app.api import projects as projects_api
from app.api import secrets as secrets_api
from app.api import users as users_api
from app.api import webhooks as webhooks_api
from app.audit.middleware import AuditMiddleware
from app.auth.oidc import router as oidc_router
from app.config import get_settings
from app.dashboard.router import router as dashboard_router
from app.obs import configure_logging
from app.obs.errors import install as install_error_handlers
from app.obs.metrics import PrometheusMiddleware, metrics_endpoint
from app.obs.middleware import RequestContextMiddleware
from app.orchestrator.redis_pool import close_redis
from app.runtime_receiver import router as otlp_router

_STATIC_DIR = Path(__file__).parent / "dashboard" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Boot-time guards. Both checks are cheap, fail-fast, and protect
    # invariants the rest of the code already assumes — better to refuse
    # to start than to serve traffic with a broken contract.
    from app.safety.dialects import assert_registry_aligned

    assert_registry_aligned()
    yield
    # Flush cached singletons so SIGTERM shutdown stays clean.
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Mnemos Platform",
        version="0.1.0",
        lifespan=lifespan,
    )
    install_error_handlers(app)

    # Order matters — RequestContextMiddleware runs first so the request_id
    # is available to AuditMiddleware and all handlers downstream.
    app.add_middleware(AuditMiddleware)
    app.add_middleware(PrometheusMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.add_route("/metrics", metrics_endpoint, methods=["GET"])
    app.include_router(health_api.router)
    app.include_router(auth_api.router)
    app.include_router(users_api.router)
    app.include_router(oidc_router)
    app.include_router(organizations_api.router)
    app.include_router(gdpr_api.router)
    app.include_router(secrets_api.router)
    app.include_router(projects_api.router)
    app.include_router(project_dbs_api.router)
    app.include_router(analysis_api.router)
    app.include_router(artifacts_api.router)
    app.include_router(data_api.router)
    app.include_router(data_api.query_router)
    app.include_router(findings_api.router)
    app.include_router(plans_api.router)
    app.include_router(diffs_api.router)
    app.include_router(break_glass_api.router)
    app.include_router(audit_api.router)
    app.include_router(webhooks_api.router)
    app.include_router(otlp_router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(dashboard_router)

    return app


app = create_app()
