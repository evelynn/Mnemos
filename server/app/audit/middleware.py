from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.audit.logger import record

_TRACKED_METHODS = {"POST", "PATCH", "DELETE", "PUT"}
_SKIP_PREFIXES = ("/static",)


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        try:
            if request.method in _TRACKED_METHODS and not request.url.path.startswith(
                _SKIP_PREFIXES
            ):
                await record(
                    actor=_resolve_actor(request),
                    action=f"http.{request.method}",
                    target=request.url.path,
                    details={
                        "status_code": response.status_code,
                        "client": request.client.host if request.client else None,
                    },
                )
        except Exception:
            # Never let audit failures leak into responses.
            pass
        return response


def _resolve_actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is not None:
        return f"user:{user.id}"
    return "anonymous"
