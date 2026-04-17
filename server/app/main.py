from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import analysis as analysis_api
from app.api import artifacts as artifacts_api
from app.api import audit as audit_api
from app.api import auth as auth_api
from app.api import data as data_api
from app.api import projects as projects_api
from app.api import secrets as secrets_api
from app.api import webhooks as webhooks_api
from app.audit.middleware import AuditMiddleware
from app.config import get_settings
from app.dashboard.router import router as dashboard_router

_STATIC_DIR = Path(__file__).parent / "dashboard" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    get_settings()
    app = FastAPI(
        title="Mnemos Platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/api/v1/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app.add_middleware(AuditMiddleware)

    app.include_router(auth_api.router)
    app.include_router(secrets_api.router)
    app.include_router(projects_api.router)
    app.include_router(analysis_api.router)
    app.include_router(artifacts_api.router)
    app.include_router(data_api.router)
    app.include_router(audit_api.router)
    app.include_router(webhooks_api.router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(dashboard_router)

    return app


app = create_app()
