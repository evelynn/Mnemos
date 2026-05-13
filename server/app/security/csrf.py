"""CSRF protection — double-submit cookie pattern (PR-44, audit E1).

The dashboard's JSON endpoints used ``content-type:
application/json``, which traditionally protected them from cross-
origin form-encoded CSRF. But the ``/logout`` POST and the form-
encoded ``/login`` are still classic CSRF targets, and a future
HTML-form endpoint would be too.

Mechanism — double-submit cookie:

1. On every dashboard page-render the server sets a
   ``mnemos_csrf`` cookie. The value is a random 32-byte hex
   string; ``SameSite=Strict`` + ``HttpOnly=false`` because the
   front-end JS needs to read it.
2. State-changing requests (POST/PATCH/PUT/DELETE) must echo the
   cookie back in an ``X-CSRF-Token`` header.
3. A cross-origin attacker can't read the cookie (Same-Origin
   Policy) so they can't construct a matching header.

Carve-outs:

* GET / HEAD / OPTIONS — never need a token.
* The login endpoint itself — the user has no session cookie yet.
* OIDC callback + webhook — server-to-server traffic, not
  browser-driven; they have their own HMAC / state guards.
* OTLP receiver — same reasoning.

The middleware is the *outermost* gate (runs early in the request
chain) so an unauthorised mutation is refused before it touches
any handler logic.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

CSRF_COOKIE = "mnemos_csrf"
CSRF_HEADER = "x-csrf-token"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Path prefixes that bypass the CSRF check. Each is documented
# inline so a future reader can audit the exemptions.
_EXEMPT_PREFIXES = (
    "/api/v1/auth/login",        # no session yet
    "/api/v1/auth/oidc/",         # OIDC redirect / callback (state-guarded)
    "/api/v1/webhooks/",          # GitLab HMAC-signed
    "/otlp/",                     # OTLP receiver
    "/metrics",                   # Prometheus scrape
    "/api/v1/health",             # liveness probe
)


def _generate_token() -> str:
    return secrets.token_hex(32)


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Issue a token on every response if the cookie is missing,
        # so the next state-changing request from the same page can
        # echo it back. Done before the handler runs so a fresh
        # login response carries the cookie.
        existing = request.cookies.get(CSRF_COOKIE)
        token = existing or _generate_token()

        method = request.method.upper()
        path = request.url.path

        # The browser-driven mutation path needs the token check.
        # Everything else falls through.
        if method not in _SAFE_METHODS and not any(
            path.startswith(p) for p in _EXEMPT_PREFIXES
        ):
            if not existing:
                # No cookie yet → no way to compare. Refuse.
                return JSONResponse(
                    status_code=403,
                    content={"detail": "csrf_token_missing"},
                )
            sent = request.headers.get(CSRF_HEADER, "")
            if not secrets.compare_digest(sent, existing):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "csrf_token_mismatch"},
                )

        response = await call_next(request)
        if not existing:
            # Set on every dashboard render so the front-end JS can
            # pick the cookie up via document.cookie. ``HttpOnly=false``
            # is *intentional* — the cookie is the public half of the
            # double-submit pattern, not a credential.
            response.set_cookie(
                key=CSRF_COOKIE,
                value=token,
                httponly=False,
                samesite="strict",
                path="/",
                max_age=60 * 60 * 24 * 7,  # 1 week
            )
        return response
