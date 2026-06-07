"""Security response headers (PR-39, closes E6).

A starlette ``Middleware`` that adds the headers any production
HTTPS deployment expects:

* ``Strict-Transport-Security`` — forces the browser to use HTTPS
  for the lifetime of the policy (1 year by default). Only emitted
  when the operator has set ``MNEMOS_HSTS_ENABLE=true`` so a
  local-dev HTTP setup doesn't get a sticky HTTPS-only flag.
* ``X-Content-Type-Options: nosniff`` — turns off MIME sniffing.
* ``X-Frame-Options: DENY`` — clickjacking defence.
* ``Referrer-Policy: same-origin`` — keep finding-ids and share
  URLs out of third-party access logs.
* ``Permissions-Policy: …=()`` — explicit deny for browser APIs
  the dashboard doesn't need (camera, geolocation, payment, USB).
  ``microphone=(self)`` is the one allow: the Ask tab's voice-command
  feature (PR-155) needs ``getUserMedia({audio})`` on this origin.
  Recognition still runs server-side/local — only the *capture* happens
  in the browser, so this grants nothing to a third party.
* ``Content-Security-Policy`` — tight default. Lets the dashboard
  load its own CSS / JS and the htmx CDN; blocks everything else.
  Operators can override via ``MNEMOS_CSP`` if they self-host
  htmx.

The middleware is intentionally permissive on dev (no HSTS, looser
CSP if ``MNEMOS_CSP_DEV=true``) so an operator running the stack
on localhost doesn't have to debug a Strict-Transport-Security
flag stuck in their browser.
"""

from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    # media-src blob: lets the Ask tab play synthesized TTS audio (PR-156),
    # which arrives as a Blob and is played via a blob: URL.
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self';"
)

# microphone is self-allowed for the Ask tab voice-command capture
# (PR-155); everything else stays hard-denied.
_PERMISSIONS_POLICY = (
    "camera=(), microphone=(self), geolocation=(), "
    "payment=(), usb=(), interest-cohort=()"
)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Always-on hardening — these are safe even in dev.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)

        # CSP — overridable via env so an operator on an air-gapped
        # network without https://unpkg.com can self-host htmx.
        csp = os.environ.get("MNEMOS_CSP", _DEFAULT_CSP)
        response.headers.setdefault("Content-Security-Policy", csp)

        # HSTS is opt-in: a local-dev HTTP deployment doesn't want a
        # sticky HSTS policy in the browser. Production hosts set
        # MNEMOS_HSTS_ENABLE=true behind their TLS terminator.
        if _env_flag("MNEMOS_HSTS_ENABLE", default=False):
            max_age = os.environ.get("MNEMOS_HSTS_MAX_AGE", "31536000")
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={max_age}; includeSubDomains",
            )
        return response
